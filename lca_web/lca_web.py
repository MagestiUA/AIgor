"""local-code-agent — веб-інтерфейс на Reflex (стиль Claude).

G3: керування сесіями через бекенд (agent/session.py): створення/список/вибір,
тека, дозволи, план-наперед, джерела — зберігаються в сесію.
Прив'язки runner (answer/shell/plan/edit) — G4.
"""
import asyncio
import functools
import json
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import reflex as rx
from reflex_base.style import set_color_mode

from agent import attachments as A
from agent import backend as B
from agent import config
from agent import convo
from agent import activity as act
from agent import context_view as cv
from agent import orchestration as orch
from agent import projectmap
from agent.loopguard import (CONTEXT_FULL_PREFIX, WARN_TEXT, LoopGuard, context_full)
from agent import limits
from agent import logs
from agent import chat_projects as CP
from agent import session as sess
from agent import topics
from agent.agent_loop import (ITER_LIMIT_MSG, is_incomplete, _estimate_iters, NUDGE_TEXT, pending_continuation,
                              recover_tool_calls, should_nudge,
                              _looks_like_leaked_reasoning,
                              extract_intended_attachments)
from agent.answerer import build_context
from agent.hierarchical import check_consistency
from agent.intent import classify_intent
from agent.llm import OllamaClient
from agent.memory import render_for_planner, render_for_user
from agent.planner import deliberate
from agent.project import load_project_doc, scan_structure
from agent.project_info import git_info
from agent.toolkit import ATTACHMENT_CHUNK_SIZE, ToolContext, default_registry

CHAT_SYSTEM = (
    "You are a helpful thinking assistant. You can search the web with the web_search "
    "tool when you need current or external information. If the user attached files, "
    "their names are listed in context — read any you need with the read_attachment(name) "
    "tool (NEVER ask the user to paste or resend them; they are already available). "
    "Answer in the user's language."
)

_CLIENT = None
logs.setup("igor")     # файл із ротацією ~/.local-code-agent/logs/igor.log
_log = logs.get("app")

# Очікувані confirm-и shell=ask, поза State (asyncio.Event не серіалізується). Ключ —
# id сесії. {sid: {"event": asyncio.Event, "ok": bool}}. Резолвить confirm_yes/no.
_CONFIRMS: dict = {}

# Stop-події для переривання стріму/виконання. Ключ — id сесії.
_STOP_EVENTS: dict = {}

# Вкладення зберігаються на диску (agent/attachments.py), не в пам'яті.


@functools.lru_cache(maxsize=2)
def _tools_tokens(chat: bool) -> int:
    reg = default_registry()
    schema = reg.schema()
    if chat:
        schema = [t for t in schema if t["function"]["name"] in State._READONLY_TOOLS]
    return cv.tools_tokens(schema)


def get_client():
    """Єдиний клієнт на процес; бекенд/адреса/ключ — з налаштувань (agent.backend).
    reset_client() скидає кеш після зміни налаштувань."""
    global _CLIENT
    if _CLIENT is None:
        from agent import backend
        _CLIENT = backend.make_client()
    return _CLIENT


def reset_client() -> None:
    global _CLIENT
    _CLIENT = None


AGO_JS = """
if (!window.__igorAgo) { window.__igorAgo = setInterval(() => {
  document.querySelectorAll('.igor-ago').forEach(e => {
    const t = Date.parse(e.dataset.ts || ''); if (!t) { e.textContent = ''; return; }
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    e.textContent = s < 60 ? s + ' с тому' : Math.floor(s / 60) + ' хв ' + (s % 60) + ' с тому';
    e.style.color = s > 60 ? '#e5a04b' : '';
  });
}, 1000); }
"""

SCROLL_SETUP_JS = """
(() => {
  // Виміряти ширину скролбара -> CSS-змінна (для вирівнювання поля вводу зі
  // скрольованим чатом: чат втрачає ширину на скролбар, поле вводу — ні).
  const sd = document.createElement('div');
  sd.style.cssText = 'overflow:scroll;width:50px;height:50px;position:absolute;top:-9999px';
  document.body.appendChild(sd);
  document.documentElement.style.setProperty('--sbw', (sd.offsetWidth - sd.clientWidth) + 'px');
  document.body.removeChild(sd);

  const c = document.getElementById('chat-scroll');
  if (!c || c._lcaObs) return;
  let stick = true;
  const nearBottom = () => c.scrollHeight - c.scrollTop - c.clientHeight < 120;
  const btn = document.getElementById('scroll-down-btn');
  const updBtn = () => { if (btn) btn.style.display = nearBottom() ? 'none' : 'inline-flex'; };
  c.addEventListener('scroll', () => { stick = nearBottom(); updBtn(); });
  const obs = new MutationObserver(() => { if (stick) c.scrollTop = c.scrollHeight; updBtn(); });
  obs.observe(c, {childList: true, subtree: true, characterData: true});
  c._lcaObs = obs;
  c.scrollTop = c.scrollHeight;
  updBtn();
})();
"""
SCROLL_BOTTOM_JS = "var c=document.getElementById('chat-scroll'); if(c){c.scrollTop=c.scrollHeight;}"

# Вставка файлів через Ctrl+V: перенаправляємо файли з буфера обміну у прихований
# input компонента rx.upload → спрацьовує наявний handle_upload. Якщо у буфері лише
# текст (files порожній) — нічого не робимо, звичайна вставка в textarea працює як є.
PASTE_SETUP_JS = """
(() => {
  if (window._lcaPasteBound) return;
  window._lcaPasteBound = true;
  document.addEventListener('paste', (e) => {
    const cd = e.clipboardData;
    if (!cd) return;
    const files = Array.from(cd.files || []);
    if (!files.length) return;
    const input = document.querySelector('.rx-Upload input[type="file"]');
    if (!input) return;
    e.preventDefault();
    const dt = new DataTransfer();
    files.forEach(f => dt.items.add(f));
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
})();
"""


BG = "#262624"
PANEL = "#171615"
INPUT = "#30302e"
BORDER = "border-white/10"
SERIF = {"fontFamily": "Newsreader, Georgia, serif"}


class State(rx.State):
    sessions: list[dict] = []
    mode: str = "code"                     # активний режим: "code" (агент) | "chat" (легкий чат)
    current_id: str = ""
    title: str = ""
    project_root: str = ""
    edits: str = "ask"
    shell: str = "smart"
    plan_first: bool = False
    references: list[str] = []
    extra_dirs: list[str] = []             # додаткові теки проєкту (читання+запис, абс. шляхи)
    extra_input: str = ""
    extra_error: str = ""
    extra_open: bool = False
    folder_input: str = ""
    ref_input: str = ""
    task: str = ""
    messages: list[dict] = []
    has_pending: bool = False
    queued_text: str = ""                  # повідомлення, що чекає, поки модель зайнята
    stopping: bool = False                 # стоп запитано, чекаємо переривання
    context_summary: str = ""              # контекст-памʼять розмови (підсумок)
    compress_log: list[dict] = []          # історія стискань контексту цієї сесії
    activity: list[dict[str, str]] = []    # жива стрічка: запити до моделі й виклики тулів (під катом)
    activity_last: str = ""                # ISO-час останньої події (годинник «N с тому» на клієнті)
    _act_seq: int = 0                      # лічильник id подій (backend-only)
    _step_touched: list = []               # файли, які змінив останній виконаний крок (backend-only)
    git_is_repo: bool = False              # робоча тека — git-репозиторій
    git_branch: str = ""
    git_add: int = 0
    git_del: int = 0
    git_files: list[dict[str, str]] = []   # зміни: [{path, delta}]
    git_more: int = 0
    session_topics: list[str] = []         # теми (chat), що траплялись у цій сесії
    # Проекти chat-режиму (аналог Claude/ChatGPT Projects) — лише для mode="chat".
    projects: list[dict] = []
    current_project_id: str = ""           # фільтр сайдбара: "" = чати без проекту
    chat_project_id: str = ""              # проект ВІДКРИТОГО чату (джерело правди для промпту)
    project_dialog_open: bool = False
    project_edit_id: str = ""              # "" = створення нового проекту
    project_name_input: str = ""
    project_prompt_input: str = ""
    move_dialog_open: bool = False
    move_chat_id: str = ""                 # чат, який зараз переносимо між проектами
    # Вкладення (#10) — лише метадані для chips; вміст файлів лежить на диску
    # (agent/attachments.py, тека сесії). Модель читає їх через read_file.
    attachments: list[dict] = []
    # Налаштування (#9)
    settings_open: bool = False
    theme: str = "dark"
    font_chat: int = 14
    font_ui: int = 13
    # Підключення до LLM (agent/backend.py): бекенд/адреса/порт/модель. Сам API-ключ у
    # State НЕ тримаємо (State їде у фронтенд) — лише поле вводу нового ключу й прапор.
    llm_backend: str = "ollama"
    llm_host: str = ""
    llm_port: str = ""
    llm_model: str = ""
    llm_key_input: str = ""
    llm_key_saved: bool = False
    llm_test_msg: str = ""
    llm_test_ok: bool = False
    llm_testing: bool = False
    # планувальник-діалог (#2+#5): питання/вибір, що очікує відповіді
    has_question: bool = False
    q_text: str = ""
    q_reasoning: str = ""
    q_options: list[dict] = []
    busy: bool = False
    status: str = ""
    rename_open: bool = False
    rename_id: str = ""
    rename_input: str = ""
    confirm_open: bool = False
    confirm_text: str = ""
    # живий стрім (#1): оновлюється по ходу роздумів/відповіді
    streaming: bool = False
    stream_content: str = ""
    stream_thinking: str = ""
    # лічильник токенів поточного запиту (над полем вводу; скидається на новий запит)
    tok_prompt: int = 0
    tok_out: int = 0
    tok_eval_ns: int = 0
    tok_tps: float = 0.0
    tok_model: str = ""

    @rx.event
    def set_task(self, v: str):
        self.task = v

    def _resolve_confirm(self, ok: bool):
        c = _CONFIRMS.get(self.current_id)
        if c is not None:
            c["ok"] = ok
            c["event"].set()
        self.confirm_open = False

    @rx.event
    def confirm_yes(self):
        self._resolve_confirm(True)

    @rx.event
    def confirm_no(self):
        self._resolve_confirm(False)

    # ── Лічильник токенів (викликати лише всередині `async with self`) ─────────
    def _act_start(self, kind: str, title: str, detail: str = "") -> int:
        """Додати подію «виконується» у живу стрічку (викликати всередині `async with self`)."""
        self._act_seq += 1
        e = {"id": str(self._act_seq), "status": "run", "kind": kind, "title": title,
             "detail": detail[:900], "t": time.strftime("%H:%M:%S")}
        self.activity = [*self.activity, e][-80:]
        self.activity_last = datetime.now(timezone.utc).isoformat()
        return self._act_seq

    def _act_done(self, seq: int, detail: str, ok: bool = True) -> None:
        self.activity = [({**e, "status": "ok" if ok else "err", "detail": detail[:900]}
                          if e["id"] == str(seq) else e) for e in self.activity]
        self.activity_last = datetime.now(timezone.utc).isoformat()

    def _reset_tokens(self):
        self.tok_prompt = self.tok_out = self.tok_eval_ns = 0
        self.tok_tps = 0.0
        self.tok_model = ""

    def _recompute_tps(self):
        self.tok_tps = round(self.tok_out / (self.tok_eval_ns / 1e9), 1) if self.tok_eval_ns else 0.0

    def _add_stats(self, stats: list[dict]):
        self.tok_prompt += sum(s.get("prompt", 0) for s in stats)
        self.tok_out += sum(s.get("out", 0) for s in stats)
        self.tok_eval_ns += sum(s.get("eval_ns", 0) for s in stats)
        for s in stats:
            if s.get("model"):
                self.tok_model = s["model"]      # остання модель, що відповідала
        self._recompute_tps()

    @rx.var
    def tokens_label(self) -> str:
        # ctx (prompt_eval_count) Ollama віддає лише у фінальному чанку — поки невідомо
        # й модель ще працює, показуємо «…», а не оманливий 0.
        ctx = "…" if (self.busy and self.tok_prompt == 0) else str(self.tok_prompt)
        model = f"{self.tok_model} · " if self.tok_model else ""
        return f"{model}↑{ctx} ctx · ↓{self.tok_out} out · {self.tok_tps} tok/s"

    def _apply_git(self, info: dict) -> None:
        self.git_is_repo = bool(info["is_repo"])
        self.git_branch = info["branch"]
        self.git_add, self.git_del, self.git_more = info["add"], info["del"], info["more"]
        self.git_files = [{"path": f["path"],
                           "delta": "новий" if f.get("new") else f"+{f['add']} −{f['del']}"}
                          for f in info["files"]]

    async def _refresh_git_async(self) -> None:
        """Оновити гілку/зміни git після завершення запиту (subprocess поза lock-ом стану)."""
        async with self:
            root, code = self.project_root, self.mode == "code"
        if not (code and root):
            return
        info = await asyncio.to_thread(git_info, root)
        async with self:
            self._apply_git(info)

    @rx.var
    def env_rows(self) -> list[dict[str, str]]:
        """Секція «Середовище»: де працює модель, режим, дозволи, гілка."""
        from urllib.parse import urlparse
        _ = (self.llm_backend, self.llm_host, self.llm_port, self.llm_model)   # перерахунок після збереження
        eff = B.resolve()
        rows: list[dict[str, str]] = []
        if eff["backend"] == "openai":
            u = urlparse(eff["base_url"])
            host = f"{u.hostname}:{u.port}" if u.port else str(u.hostname or "")
            rows.append({"label": "Сервер", "value": host or "не налаштовано (Налаштування)"})
            rows.append({"label": "Модель", "value": eff["model"] or "авто"})
        else:
            rows.append({"label": "Бекенд", "value": "локально (Ollama)"})
            rows.append({"label": "Модель", "value": config.MODEL})
        rows.append({"label": "Режим", "value": "Чат" if self.mode == "chat" else "Код"})
        rows.append({"label": "Версія", "value": logs.app_version()})
        if self.mode == "code":
            rows.append({"label": "Правки / консоль", "value": f"{self.edits} / {self.shell}"})
            if self.git_branch:
                rows.append({"label": "Гілка git", "value": self.git_branch})
        return rows

    @rx.var
    def folder_rows(self) -> list[dict[str, str]]:
        """Секція «Теки та файли»: з чим працює відкритий чат."""
        rows: list[dict[str, str]] = []
        if self.mode == "code" and self.project_root:
            rows.append({"name": Path(self.project_root).name, "path": self.project_root, "tag": "проєкт"})
        if self.mode == "chat" and self.chat_project_id:
            pr = CP.load_project(self.chat_project_id)
            if pr:
                rows.append({"name": pr.name, "path": "проєкт чату (інструкції)", "tag": "проєкт"})
        if self.mode == "code":
            for d in self.extra_dirs:
                rows.append({"name": Path(d).name or d, "path": d, "tag": "додаткова тека"})
        for r in self.references:
            rows.append({"name": Path(r).name, "path": r, "tag": "джерело"})
        _ = self.attachments                                  # перерахунок при зміні вкладень
        if self.current_id:
            n = len(A.list_saved(self.current_id))
            if n:
                rows.append({"name": f"Вкладення ({n})", "path": str(A.session_dir(self.current_id)),
                             "tag": "файли чату"})
        return rows

    @rx.var
    def git_summary(self) -> str:
        return f"+{self.git_add} −{self.git_del}"

    @rx.var
    def ctx_report(self) -> str:
        """Розбивка контексту поточного чату (JSON-рядок; решта ctx_* розбирають його).
        Токени — оцінка за символами (agent/context_view.py). Залежності відстежуються
        прямими зверненнями до self.* (messages, summary, compress_log, вкладення…)."""
        lim = limits.get()
        chat = self.mode == "chat"
        msgs = [dict(m) for m in self.messages]
        _ = self.attachments                                  # перерахунок при зміні чипів
        cid = self.current_id
        names = A.list_saved(cid) if cid else []
        last = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
        cur = set(last.get("attachments") or [])
        cur_size = sum(a["size"] for a in names if a["name"] in cur)
        inline_ok = bool(cur) and cur_size <= lim.inline
        atts = [{"name": a["name"], "size": a["size"], "inline": inline_ok and a["name"] in cur}
                for a in names]
        project = ""
        if chat and self.chat_project_id:
            pr = CP.load_project(self.chat_project_id)
            project = CP.as_system_block(pr.prompt) if pr else ""
        elif not chat and self.project_root:
            project = load_project_doc(self.project_root) or ""
        topics_note = topics.available_topics_note(topics.list_topics()) if chat else ""
        pmap_text = projectmap.context_block(self.project_root) if (not chat and self.project_root) else ""
        from agent.agent_loop import SYSTEM as EXEC_SYSTEM
        rep = cv.build(
            window=lim.context_tokens, system=CHAT_SYSTEM if chat else EXEC_SYSTEM,
            tools_tok=_tools_tokens(chat), project=project, project_map=pmap_text, topics_note=topics_note,
            summary=self.context_summary,
            summary_budget=lim.summary // 3 if chat else lim.summary,
            topic_budget=lim.topic, digest_budget=lim.digest, messages=msgs,
            history_window=lim.history_msgs, attachments=atts, inline_budget=lim.inline,
            compressions=[dict(c) for c in self.compress_log])
        return json.dumps(rep, ensure_ascii=False)

    @rx.var
    def ctx_rows(self) -> list[dict[str, str]]:
        rep = json.loads(self.ctx_report)
        return [{"label": r["label"], "tokens_label": r["tokens_label"], "pct_label": f"{r['pct']}%",
                 "color": r["color"], "note": r["note"], "compress": r["compress"]}
                for r in rep["rows"]]

    @rx.var
    def ctx_segments(self) -> list[dict[str, str]]:
        rep = json.loads(self.ctx_report)
        segs = [{"width": f"{r['pct']}%", "color": r["color"]} for r in rep["rows"] if r["pct"] > 0]
        return segs

    @rx.var
    def ctx_pct(self) -> int:
        return int(json.loads(self.ctx_report)["used_pct"])

    @rx.var
    def ctx_total(self) -> str:
        rep = json.loads(self.ctx_report)
        return f"≈{rep['used_label']} / {rep['window_label']}"

    @rx.var
    def ctx_free(self) -> str:
        rep = json.loads(self.ctx_report)
        return f"{rep['free_label']}  {rep['free_pct']}%"

    @rx.var
    def ui_font_js(self) -> str:
        """JS, що масштабує кореневий шрифт <html> — усі rem-based Tailwind-розміри
        (text-xs/sm/4xl) інтерфейсу скейляться пропорційно. Чат має власний px-розмір."""
        return f"document.documentElement.style.fontSize='{self.font_ui}px'"

    @rx.var
    def chat_font_px(self) -> str:
        """Розмір шрифту повідомлень чату в px (абсолютний, не залежить від font_ui)."""
        return f"{self.font_chat}px"

    @rx.var
    def folder_name(self) -> str:
        return Path(self.project_root).name if self.project_root else "тека"

    @rx.var
    def has_current(self) -> bool:
        return self.current_id != ""

    @rx.var
    def is_chat(self) -> bool:
        return self.mode == "chat"

    @rx.var
    def visible_sessions(self) -> list[dict]:
        """Чати поточного режиму (Чат/Код роздільні списки). У chat-режимі додатково
        фільтруємо за поточним проектом сайдбара (current_project_id="" — чати без
        проекту, як і код-режим, де проектів нема взагалі)."""
        base = [s for s in self.sessions if s.get("kind", "code") == self.mode]
        if self.mode != "chat":
            return base
        return [s for s in base if (s.get("project_id") or "") == self.current_project_id]

    def _attachments_note(self) -> str:
        """Контекстний блок для моделі: імена файлів сесії з ДИСКА (не зі стейджинг-чіпів,
        бо ті зникають після відправки). Модель читає їх через read_attachment."""
        return A.attachments_note(self.current_id, A.list_saved(self.current_id))

    def _clear_view(self):
        # Файли вкладень лишаються на диску (ручна чистка) — скидаємо лише State-chips.
        self.current_id = ""
        self.title = ""
        self.project_root = ""
        self.messages = []
        self.references = []
        self.extra_dirs = []
        self.has_pending = False
        self.queued_text = ""
        self.context_summary = ""
        self.compress_log = []
        self.activity = []
        self.session_topics = []
        self.chat_project_id = ""
        self.attachments = []
        self._load_question(None)

    @rx.event
    def set_mode(self, m: str):
        if m == self.mode:
            return
        self.mode = m
        cur = next((s for s in self.sessions if s["id"] == self.current_id), None)
        if not cur or cur.get("kind", "code") != m:   # відкритий чат іншого типу -> згорнути
            self._clear_view()

    @rx.event
    def select_project(self, pid: str):
        """Фільтр сайдбара (chat-режим): показати чати лише цього проекту
        ("" — без проекту). Якщо відкритий чат не належить новому фільтру —
        згортаємо вид (той самий патерн, що й set_mode)."""
        if pid == self.current_project_id:
            return
        self.current_project_id = pid
        cur = next((s for s in self.sessions if s["id"] == self.current_id), None)
        if not cur or (cur.get("project_id") or "") != pid:
            self._clear_view()

    @rx.event
    def set_folder_input(self, v: str):
        self.folder_input = v

    @rx.event
    def set_ref_input(self, v: str):
        self.ref_input = v

    @rx.event
    def load_sessions(self):
        self.sessions = sess.list_sessions()
        self.projects = CP.list_projects()
        # Самозцілення: чат може лишитись з посиланням на проект, якого вже
        # нема на диску (живий кейс: чат "зник" із сайдбара, бо ховався за
        # фільтром неіснуючого проекту — і "чатів", і "проектів" виглядало
        # порожньо одночасно). Звіряємо й чистимо осиротілі project_id.
        valid_ids = {p["id"] for p in self.projects}
        for s_meta in self.sessions:
            pid = s_meta.get("project_id") or ""
            if pid and pid not in valid_ids:
                s = sess.load_session(s_meta["id"])
                s.project_id = ""
                sess.save_session(s)
                s_meta["project_id"] = ""
        # Відновлюємо останній відкритий чат (якщо він і досі існує — захист від
        # того самого класу "осиротілих" посилань, що й вище).
        from agent import settings as S
        last_id = S.load().get("last_session_id", "")
        if last_id and any(s["id"] == last_id for s in self.sessions):
            self._select(last_id)

    def _select(self, sid: str):
        s = sess.load_session(sid)
        self.current_id = s.id
        self.mode = s.kind
        self.title = s.title
        self.project_root = s.project_root
        self._apply_git(git_info(s.project_root) if s.kind == "code" else
                        {"is_repo": False, "branch": "", "add": 0, "del": 0, "files": [], "more": 0})
        self.chat_project_id = s.project_id
        self.current_project_id = s.project_id    # сайдбар-фільтр узгоджений із чатом
        self.edits = s.permissions.get("edits", "ask")
        self.shell = s.permissions.get("shell", "smart")
        self.plan_first = s.plan_first
        self.references = list(s.reference_files)
        self.extra_dirs = list(s.extra_dirs)
        self.messages = [{"thinking": "", "attachments": [], **m} for m in s.messages]
        self.has_pending = s.pending_plan is not None
        self.context_summary = s.context_summary
        self.compress_log = list(s.compressions)
        self.activity = []
        self.session_topics = list(s.topics)
        self.attachments = []     # чіпи — стейджинг до відправки; вкладення сесії на диску
        self._load_question(s.pending_question)
        from agent import settings as S
        S.save({**S.load(), "last_session_id": sid})

    def _load_question(self, q: dict | None):
        """Відновити стан питання планувальника зі збереженого pending_question."""
        if q:
            self.has_question = True
            self.q_text = q.get("question", "")
            self.q_reasoning = q.get("reasoning", "")
            self.q_options = list(q.get("options", []))
        else:
            self.has_question = False
            self.q_text = ""
            self.q_reasoning = ""
            self.q_options = []

    def _append(self, role: str, content: str, kind: str = "text", thinking: str = "",
                attachments: list | None = None):
        self.messages = self.messages + [
            {"role": role, "content": content, "kind": kind, "thinking": thinking,
             "attachments": attachments or []}]
        if self.current_id:
            s = sess.load_session(self.current_id)
            # Розгорнути Reflex-проксі, ВКЛЮЧНО з вкладеним списком attachments
            # (інакше asdict() при збереженні падає на MutableProxy).
            s.messages = [
                {"role": m["role"], "content": m["content"],
                 "kind": m.get("kind", "text"), "thinking": m.get("thinking", ""),
                 "attachments": list(m.get("attachments", []) or [])}
                for m in self.messages]
            sess.save_session(s)

    def _save_current(self):
        if not self.current_id:
            return
        s = sess.load_session(self.current_id)
        s.title = self.title or s.title
        s.project_root = self.project_root
        s.permissions = {"edits": self.edits, "shell": self.shell}
        s.plan_first = self.plan_first
        s.reference_files = [str(x) for x in self.references]   # розгорнути проксі
        s.extra_dirs = [str(x) for x in self.extra_dirs]
        sess.save_session(s)
        self.sessions = sess.list_sessions()

    @rx.event
    def new_chat(self):
        s = sess.new_session("Новий чат", "", kind=self.mode)
        if self.mode == "chat" and self.current_project_id:
            s.project_id = self.current_project_id
        sess.save_session(s)
        self.sessions = sess.list_sessions()
        self._select(s.id)

    @rx.event
    def select_chat(self, sid: str):
        self._select(sid)

    @rx.event
    def set_rename_input(self, v: str):
        self.rename_input = v

    @rx.event
    def set_rename_open(self, v: bool):
        self.rename_open = v

    @rx.event
    def open_rename(self, sid: str, title: str):
        self.rename_id = sid
        self.rename_input = title
        self.rename_open = True

    @rx.event
    def save_rename(self):
        if self.rename_id and self.rename_input.strip():
            s = sess.load_session(self.rename_id)
            s.title = self.rename_input.strip()
            sess.save_session(s)
            if self.current_id == self.rename_id:
                self.title = s.title
            self.sessions = sess.list_sessions()
        self.rename_open = False

    @rx.event
    def delete_chat(self, sid: str):
        sess.delete_session(sid)
        if self.current_id == sid:
            self._clear_view()
        self.sessions = sess.list_sessions()
        from agent import settings as S
        cur = S.load()
        if cur.get("last_session_id") == sid:
            S.save({**cur, "last_session_id": ""})

    # ── Проекти chat-режиму ──────────────────────────────────────────────────
    @rx.event
    def open_move_dialog(self, sid: str):
        self.move_chat_id = sid
        self.move_dialog_open = True

    @rx.event
    def set_move_dialog_open(self, v: bool):
        self.move_dialog_open = v

    @rx.event
    def move_chat_to_project(self, pid: str):
        """Перенести чат у проект pid ("" — винести з проекту/без проекту)."""
        if self.move_chat_id:
            s = sess.load_session(self.move_chat_id)
            s.project_id = pid
            sess.save_session(s)
            if self.current_id == self.move_chat_id:
                self.chat_project_id = pid
            self.sessions = sess.list_sessions()
        self.move_dialog_open = False
        self.move_chat_id = ""

    @rx.event
    def open_new_project(self):
        self.project_edit_id = ""
        self.project_name_input = ""
        self.project_prompt_input = ""
        self.project_dialog_open = True

    @rx.event
    def open_edit_project(self, pid: str):
        p = CP.load_project(pid)
        if not p:
            return
        self.project_edit_id = p.id
        self.project_name_input = p.name
        self.project_prompt_input = p.prompt
        self.project_dialog_open = True

    @rx.event
    def set_project_dialog_open(self, v: bool):
        self.project_dialog_open = v

    @rx.event
    def set_project_name_input(self, v: str):
        self.project_name_input = v

    @rx.event
    def set_project_prompt_input(self, v: str):
        self.project_prompt_input = v[:CP.PROMPT_MAX_CHARS]

    @rx.event
    def save_project_dialog(self):
        name = self.project_name_input.strip()
        if not name:
            return
        if self.project_edit_id:
            p = CP.load_project(self.project_edit_id)
            if p:
                p.name = name
                p.prompt = self.project_prompt_input
                CP.save_project(p)
        else:
            CP.save_project(CP.new_project(name, self.project_prompt_input))
        self.projects = CP.list_projects()
        self.project_dialog_open = False

    @rx.event
    def delete_project_event(self, pid: str):
        """Видалити проект. Чати, що належали йому, лишаються — просто без
        проекту (НЕ видаляємо їхній вміст)."""
        for s_meta in self.sessions:
            if (s_meta.get("project_id") or "") == pid:
                s = sess.load_session(s_meta["id"])
                s.project_id = ""
                sess.save_session(s)
        CP.delete_project(pid)
        if self.current_project_id == pid:
            self.current_project_id = ""
        if self.chat_project_id == pid:
            self.chat_project_id = ""
        self.projects = CP.list_projects()
        self.sessions = sess.list_sessions()

    @rx.event
    def on_enter(self, key: str):
        if key == "Enter":
            return State.send_task

    @rx.event
    def set_edits(self, v: str):
        self.edits = v
        self._save_current()

    @rx.event
    def set_shell(self, v: str):
        self.shell = v
        self._save_current()

    @rx.event
    def set_plan_first(self, v: bool):
        self.plan_first = v
        self._save_current()

    # ── Налаштування (#9) ──────────────────────────────────────────────────────
    @rx.event
    def toggle_settings(self):
        self.settings_open = not self.settings_open

    @rx.event
    def load_settings(self):
        from agent import settings as S
        d = S.load()
        self.theme = d.get("theme", "auto")
        self.font_chat = d.get("font_chat", 14)
        self.font_ui = d.get("font_ui", 13)
        from agent import backend
        eff = backend.resolve(d)
        self.llm_backend = eff["backend"]
        self.llm_model = d.get("llm_model", "")
        self.llm_key_saved = bool(d.get("llm_api_key"))
        self.llm_host, self.llm_port = d.get("llm_host", ""), str(d.get("llm_port", ""))
        if not self.llm_host and eff["backend"] == "openai":      # дефолт із .env/config
            from urllib.parse import urlparse
            u = urlparse(eff["base_url"])
            self.llm_host, self.llm_port = u.hostname or "", str(u.port or "")
        # Тему застосовує next-themes сам (зберігає в localStorage між перезавантаженнями);
        # set_color_mode НЕ можна викликати з бекенд-хендлера — лише шрифт через DOM.
        # PASTE_SETUP_JS — одноразово вішає document-listener вставки файлів (Ctrl+V).
        return [rx.call_script(self.ui_font_js), rx.call_script(PASTE_SETUP_JS), rx.call_script(AGO_JS)]

    def _persist_settings(self):
        from agent import settings as S
        # Мерджимо з тим, що вже на диску — інакше затираємо інші ключі (напр.
        # last_session_id), які записує не лише ця функція.
        S.save({**S.load(), "theme": self.theme, "font_chat": self.font_chat,
               "font_ui": self.font_ui})

    @rx.event
    def save_settings(self):
        self._persist_settings()

    @rx.event
    def set_theme(self, v: str | list[str]):
        # Лише зберігаємо вибір; реальне перемикання робить set_color_mode,
        # підвʼязаний до фронтенд-тригера сегмент-контролу (див. settings_dialog).
        if isinstance(v, list):
            v = v[0] if v else self.theme
        self.theme = v
        self._persist_settings()

    @rx.event
    def set_font_chat(self, v: str | list[str] | list[int]):
        val = v[0] if isinstance(v, list) else v
        # Кламп МУСИТЬ збігатися з min/max слайдера (12..32), інакше повзунок застрягає
        # посеред шкали (хендлер ріже значення раніше за кінець доріжки).
        self.font_chat = max(12, min(32, int(val)))
        self._persist_settings()

    @rx.event
    def set_font_ui(self, v: str | list[str] | list[int]):
        val = v[0] if isinstance(v, list) else v
        self.font_ui = max(11, min(24, int(val)))     # збігається зі слайдером 11..24
        self._persist_settings()
        return rx.call_script(self.ui_font_js)

    # ── Підключення до LLM ─────────────────────────────────────────────────────
    @rx.event
    def set_llm_backend(self, v: str | list[str]):
        self.llm_backend = v[0] if isinstance(v, list) else v

    @rx.event
    def set_llm_host(self, v: str):
        self.llm_host = v

    @rx.event
    def set_llm_port(self, v: str):
        self.llm_port = "".join(ch for ch in v if ch.isdigit())[:5]

    @rx.event
    def set_llm_model(self, v: str):
        self.llm_model = v

    @rx.event
    def set_llm_key_input(self, v: str):
        self.llm_key_input = v

    @rx.event
    def save_llm_settings(self):
        """Зберегти підключення й скинути закешований клієнт — наступний запит піде
        вже на нову адресу/ключ. Порожнє поле ключа = лишити збережений."""
        from agent import settings as S
        cur = S.load()
        new = {**cur, "llm_backend": self.llm_backend, "llm_host": self.llm_host.strip(),
               "llm_port": self.llm_port.strip(), "llm_model": self.llm_model.strip()}
        if self.llm_key_input.strip():
            new["llm_api_key"] = self.llm_key_input.strip()
        S.save(new)
        _log.info("налаштування підключення: backend=%s host=%s port=%s model=%s ключ_змінено=%s",
                  new["llm_backend"], new["llm_host"], new["llm_port"], new["llm_model"],
                  bool(self.llm_key_input.strip()))
        reset_client()
        self.llm_key_input = ""
        self.llm_key_saved = bool(new.get("llm_api_key"))
        self.llm_test_msg, self.llm_test_ok = "Збережено.", True

    @rx.event
    def export_diagnostics(self):
        """Зібрати архів з логами (без ключа й без вмісту чатів) і відкрити його теку."""
        try:
            path = logs.make_diagnostics()
            _log.info("діагностику збережено: %s", path)
            self.llm_test_msg, self.llm_test_ok = f"Діагностику збережено: {path}", True
            import os as _os
            _os.startfile(str(path.parent))        # відкрити теку в Провіднику
        except Exception as e:
            _log.exception("не вдалось зібрати діагностику")
            self.llm_test_msg, self.llm_test_ok = f"Не вдалось зібрати діагностику: {e}", False

    @rx.event(background=True)
    async def test_llm_connection(self):
        """Перевірити адресу/порт/ключ (GET /models) до збереження; підставляє модель."""
        from agent import backend, settings as S
        async with self:
            if self.llm_testing:
                return
            self.llm_testing, self.llm_test_msg = True, "Перевіряю…"
            host, port, typed = self.llm_host, self.llm_port, self.llm_key_input.strip()
        key = typed or S.load().get("llm_api_key", "")
        ok, msg, models = await asyncio.to_thread(
            lambda: backend.test_connection(backend.build_base_url(host, port), key))
        async with self:
            self.llm_testing, self.llm_test_ok, self.llm_test_msg = False, ok, msg
            if ok and models and not self.llm_model.strip():
                self.llm_model = models[0]

    # ── Вкладення (#10) ────────────────────────────────────────────────────────
    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        cid = self.current_id or "_tmp"     # до створення сесії — у тимчасову теку _tmp
        for file in files:
            if len(self.attachments) >= A.MAX_FILES:
                break
            if any(a["name"] == file.filename for a in self.attachments):
                continue                           # вже є — пропустити дублікат
            data = await file.read()
            result = A.process(file.filename, data)
            content = result.pop("_content", "")  # вміст не тримаємо в State
            if not result["error"] and content:
                A.save(cid, file.filename, content)   # валідний → на диск
            self.attachments = self.attachments + [result]

    @rx.event
    def remove_attachment(self, name: str):
        # Видалення чіпа = ручна чистка файлу з диска.
        A.remove(self.current_id or "_tmp", name)
        self.attachments = [a for a in self.attachments if a["name"] != name]

    @rx.event
    def save_folder(self):
        self.project_root = self.folder_input.strip()
        if self.project_root and self.title in ("", "Новий чат"):
            self.title = Path(self.project_root).name
        self._apply_git(git_info(self.project_root))
        self._save_current()

    @rx.event
    def add_ref(self):
        r = self.ref_input.strip()
        if r and r not in self.references:
            self.references = self.references + [r]
        self.ref_input = ""
        self._save_current()

    @rx.event
    def remove_ref(self, r: str):
        self.references = [x for x in self.references if x != r]
        self._save_current()

    @rx.event
    def set_extra_input(self, v: str):
        self.extra_input = v
        self.extra_error = ""

    @rx.event
    def set_extra_open(self, v: bool):
        self.extra_open = v
        if v:
            self.extra_error = ""

    @rx.event
    def add_extra_dir(self):
        """Додати ще одну теку до проєкту: агент читає й пише в ній за абсолютним шляхом
        (сценарій «скопіювати проєкт у нову теку й далі працювати там»). Якщо теки ще нема — створюємо."""
        raw = self.extra_input.strip().strip('"')
        self.extra_error = ""
        if not raw:
            return
        p = Path(raw).expanduser()
        if not p.is_absolute():
            self.extra_error = "потрібен абсолютний шлях"
            return
        if p.exists() and not p.is_dir():
            self.extra_error = "це файл, а не тека (файли — у «джерелах»)"
            return
        if not p.exists():
            try:
                p.mkdir(parents=True)                  # нова тека для копії проєкту
            except OSError as e:
                self.extra_error = f"не вдалося створити теку: {e}"
                return
        s = str(p.resolve())
        same = self.project_root and Path(self.project_root).resolve() == Path(s)
        if same or s in self.extra_dirs:
            self.extra_error = "ця тека вже додана"
            return
        self.extra_dirs = self.extra_dirs + [s]
        self.extra_input = ""
        self.extra_open = False
        self._save_current()

    @rx.event
    def remove_extra_dir(self, d: str):
        self.extra_dirs = [x for x in self.extra_dirs if x != d]
        self._save_current()

    def _roots_note(self) -> str:
        """Рядок для контексту моделі: які ще теки доступні (порожній, якщо їх нема)."""
        if not self.extra_dirs:
            return ""
        return ("Додаткові теки проєкту (читай і пиши в них за АБСОЛЮТНИМ шляхом; відносні шляхи рахуються від "
                "кореня проєкту): " + "; ".join(str(x) for x in self.extra_dirs) + "\n")

    async def _confirm_async(self, text: str) -> bool:
        """Показати діалог підтвердження З КОНТЕКСТУ background-події (через `async with
        self`, тож дельта confirm_open реально доходить до фронта — на відміну від
        старого _show через run_coroutine_threadsafe, що не мав EventContext) і чекати
        клік через asyncio.Event БЕЗ блокування воркер-потоку. Резолвиться
        confirm_yes/confirm_no; таймаут 300с -> відмова."""
        ev = asyncio.Event()
        _CONFIRMS[self.current_id] = {"event": ev, "ok": False}
        async with self:
            self.confirm_text = text
            self.confirm_open = True
        try:
            await asyncio.wait_for(ev.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass
        async with self:
            self.confirm_open = False
        return _CONFIRMS.pop(self.current_id, {}).get("ok", False)

    def _step_context(self, plan, step, root: str, att_note: str) -> str:
        """Контекст кроку: загальна задача + ПОВНИЙ план із результатами вже виконаних кроків
        і змінених файлів (render_for_planner) — кроки бачать, що зробили попередні, а не
        лише галочки. Підсумок розмови та вкладення — як і раніше."""
        ctx = (f"Project root: {root}\n{self._roots_note()}\nЗагальна задача: {plan.task}\n"
               f"План і результати вже виконаних кроків (ти виконуєш ЛИШЕ крок #{step.id}):\n"
               f"{render_for_planner(plan)}")
        pmap = projectmap.context_block(root)             # свіжа: попередній крок міг змінити файли
        if pmap:
            ctx += "\n\n" + pmap
        if self.context_summary:
            ctx += f"\n{convo.as_context(self.context_summary)}"
        if att_note:
            ctx += "\n" + att_note
        return ctx

    def _save_pending(self, plan) -> None:
        """Зберегти план (зі статусами) як очікуваний: «Виконати» продовжить з першого
        невиконаного кроку. Викликати всередині `async with self`."""
        s = sess.load_session(self.current_id)
        s.set_pending_plan(plan)
        sess.save_session(s)
        self.has_pending = True

    async def _execute_steps(self, plan, root: str, client):
        """Виконати кроки плану через per-step tool-loop: модель сама обирає тули
        (read/write/edit/run_shell), ми лише показуємо її дії та diff у чаті.

        Керування (фідбек розробників):
          * кроки бачать результати попередніх (render_for_planner), а не лише галочки;
          * виконані кроки пропускаємо — так план ВІДНОВЛЮЄТЬСЯ з місця зупинки;
          * невдалий крок (зациклився / ліміт / контекст) ЗУПИНЯЄ план і лишає його як
            очікуваний («Виконати» повторить з цього кроку), а не будує далі на зламаному;
          * після останнього кроку — звірка (_verify_plan): синтаксис змінених файлів +
            перевірка узгодженості, з обмеженим повтором проблемного кроку.

        ToolContext: edits=auto (план уже схвалено — через plan_first або edits=auto).
        shell передаємо як є; для shell=ask confirm показує модальний попап."""
        ctx = self._exec_ctx(root, client)
        async with self:
            cid = self.current_id
            att_note = self._attachments_note()    # шляхи файлів; виконавець читає read_file
        stop_ev = _STOP_EVENTS.get(cid)
        stopped = False
        failed = None
        final = ""
        touched_by_step: dict[int, list[str]] = {}
        total = len(plan.steps)
        done_before = sum(1 for s in plan.steps if s.status == "done")
        if 0 < done_before < total:
            nxt = next(s for s in plan.steps if s.status != "done")
            async with self:
                self._append("assistant", f"▶ Продовжую план з кроку #{nxt.id} (виконано {done_before} з {total}).", "note")
        for step in plan.steps:
            if step.status == "done":
                continue
            if stop_ev is not None and stop_ev.is_set():       # перервано між кроками
                stopped = True
                break
            async with self:
                self.status = f"Крок #{step.id}/{total}: {step.description[:50]}"
            if step.is_noop:
                plan.set_result(step.id, "done", "inline")
                continue
            final, log = await self._run_tool_step(step.description, ctx,
                                                   title=step.description[:60],
                                                   context=self._step_context(plan, step, root, att_note),
                                                   stop_event=stop_ev)
            ok = bool(log) and not is_incomplete(final)
            plan.set_result(step.id, "done" if ok else "failed", final or "—")
            async with self:
                files = list(self._step_touched)
            if files:
                touched_by_step[step.id] = files
                plan.facts["змінені файли"] = ", ".join(dict.fromkeys(
                    f for fl in touched_by_step.values() for f in fl))[:500]
            if not ok:
                failed = step
                break

        if failed is not None:
            async with self:
                self._append("assistant",
                             f"⛔ Крок #{failed.id} не завершено: {(final or '—')[:240]}\n\nРешту кроків призупинено, "
                             "щоб не будувати на зламаному. Натисніть «Виконати» — продовжу з цього кроку, "
                             "або «Відхилити».", "note")
                self._save_pending(plan)
            return render_for_planner(plan)

        verify_note = ""
        if stopped:
            if any(s.status != "done" for s in plan.steps):
                async with self:
                    self._save_pending(plan)              # можна відновити кнопкою «Виконати»
        elif touched_by_step:
            verify_note = await self._verify_plan(plan, root, client, ctx, touched_by_step, stop_ev, att_note)
        async with self:
            self._append("assistant", "⛔ Зупинено — решту можна продовжити кнопкою «Виконати»" if stopped
                         else "Готово ✓", "note")
        return render_for_planner(plan) + (f"\n{verify_note}" if verify_note else "")

    async def _verify_plan(self, plan, root: str, client, ctx, touched_by_step: dict, stop_ev, att_note: str) -> str:
        """Звірка після всіх кроків: (1) змінені .py компілюються; (2) LLM-перевірка узгодженості
        кроків (hierarchical.check_consistency). Проблему виправляємо ПОВТОРОМ одного кроку з
        уточненням — не більше 2 раундів, щоб не крутитись. Повертає рядок для підсумку."""
        def all_files() -> list[str]:
            return list(dict.fromkeys(f for fl in touched_by_step.values() for f in fl))

        async with self:
            self.status = "Перевіряю результат…"
            self._append("assistant", "🔍 Перевіряю результат: синтаксис змінених файлів і узгодженість кроків…", "note")
        for _ in range(2):
            bad = await asyncio.to_thread(lambda: orch.syntax_report(root, all_files()))
            if bad:
                path, msg = bad[0]
                note = f"У файлі {path} синтаксична помилка: {msg}. Виправ її, не ламаючи решту."
                step_id = orch.pick_step(touched_by_step, [p for p, _ in bad])
            else:
                plan.facts["синтаксис"] = "ок (змінені .py компілюються)"
                verdict = await asyncio.to_thread(lambda: check_consistency(plan, client))
                step_id = verdict.get("step_id")
                if verdict["ok"] or not any(s.id == step_id for s in plan.steps):
                    async with self:
                        self._append("assistant", "✓ Звірка пройшла: кроки узгоджені, змінені файли без синтаксичних помилок.", "note")
                    return "Звірка: ок."
                note = verdict.get("note") or "узгодь результат кроків"
            step = next((s for s in plan.steps if s.id == step_id), plan.steps[-1])
            async with self:
                self._append("assistant", f"⚠ Звірка: {note[:300]}\n\nПовторюю крок #{step.id}.", "note")
            final, log = await self._run_tool_step(
                step.description + f"\nУточнення після перевірки: {note}", ctx,
                title=f"повтор: {step.description[:50]}",
                context=self._step_context(plan, step, root, att_note), stop_event=stop_ev)
            plan.set_result(step.id, "done" if (log and not is_incomplete(final)) else "failed", final or "—")
            async with self:
                touched_by_step.setdefault(step.id, []).extend(self._step_touched)
        bad = await asyncio.to_thread(lambda: orch.syntax_report(root, all_files()))
        if bad:
            async with self:
                self._append("assistant", f"⚠ Після повторів лишилась синтаксична помилка у {bad[0][0]}: {bad[0][1]}. "
                                          "Перегляньте вручну.", "note")
            return f"Звірка: лишилась помилка у {bad[0][0]}."
        return "Звірка: проблеми виправлено повтором."

    def _exec_ctx(self, root: str, client) -> ToolContext:
        """ToolContext для виконання: edits=auto (план/запит уже схвалено). Підтвердження
        shell тепер робить async-шар (_dispatch_tool/_confirm_async), тож confirm=None —
        самі тули блокуючого попапу більше не викликають."""
        return ToolContext(root=Path(root),
                           permissions={"edits": "auto", "shell": self.shell},
                           client=client, confirm=None,
                           attachments_dir=A.session_dir(self.current_id), track_project=True,
                           extra_roots=[Path(x) for x in self.extra_dirs])

    async def _dispatch_tool(self, reg, name: str, args: dict, ctx: ToolContext) -> str:
        """Виконати тул + показати подію в живій стрічці («що виконує і який результат»)."""
        async with self:
            aid = self._act_start("tool", name, act.summarize_args(name, args))
        t0 = time.monotonic()
        result = await self._dispatch_tool_inner(reg, name, args, ctx)
        async with self:
            self._act_done(aid, f"{time.monotonic() - t0:.1f}с · " + act.clip_result(result),
                           ok=act.result_ok(result))
        return result

    async def _dispatch_tool_inner(self, reg, name: str, args: dict, ctx: ToolContext) -> str:
        """Виконати тул. run_shell, що потребує підтвердження (shell=ask, або smart+
        небезпечна), ставимо на ASYNC-паузу з діалогом (без блокування воркера); решта —
        у потоці. Схвалену команду виконуємо з shell=auto, щоб тул не питав удруге."""
        if name == "run_shell":
            from agent.shell_guard import classify
            cmd = (args.get("command") or "").strip()
            mode = ctx.permissions.get("shell", "allowlist")
            if mode == "off":
                return "консоль вимкнена (дозвіл shell=off)"
            danger = mode == "smart" and classify(cmd) == "danger"
            if mode == "ask" or danger:
                prompt = f"⚠ Небезпечна команда:\n{cmd}" if danger else f"Виконати: {cmd}"
                if not await self._confirm_async(prompt):
                    return "команду відхилено користувачем"
                ctx = replace(ctx, permissions={**ctx.permissions, "shell": "auto"})
        return await asyncio.to_thread(lambda: reg.dispatch(name, args, ctx))

    async def _run_tool_step(self, step_text: str, ctx: ToolContext, title: str = "",
                             context: str = "", stop_event=None):
        """Async per-step tool-loop: модель кличе тули, ми диспетчеримо їх (виклик моделі
        — per-iteration через to_thread). run_shell, що потребує підтвердження, ставимо
        на async-паузу — діалог показується з контексту ЦІЄЇ події (дельта доходить), а
        не з відірваної корутини. Додає ОДНЕ повідомлення-крок із катом «хід виконання».
        Логіку дублюємо з agent_loop.run_step (той лишається для headless/тестів)."""
        from agent.agent_loop import SYSTEM as EXEC_SYSTEM, _args
        reg = default_registry()
        client = ctx.client
        user = (f"Контекст:\n{context}\n\n" if context else "") + f"Крок:\n{step_text}"
        messages = [{"role": "system", "content": EXEC_SYSTEM},
                    {"role": "user", "content": user}]
        # сервер: довгу роботу не обмежуємо (стежимо за прогресом, LoopGuard); локальна модель — оцінка
        max_iters = limits.get().hard_iters or await asyncio.to_thread(
            lambda: _estimate_iters(step_text, context, client))
        guard = LoopGuard()
        actions: list[tuple[str, str]] = []   # (tool, result)
        touched: list[str] = []               # файли, змінені цим кроком (для звірки)
        stats: list[dict] = []                # client.last_stats кожного виклику моделі
        final = ITER_LIMIT_MSG
        nudged = False
        for _ in range(max_iters):
            if stop_event is not None and stop_event.is_set():
                final = "зупинено користувачем"; break
            async with self:
                aid = self._act_start("llm", "Запит до моделі", f"повідомлень у контексті: {len(messages)}")
            t_llm = time.monotonic()
            msg = await asyncio.to_thread(
                lambda: client.chat(messages, tools=reg.schema(), profile=config.EXECUTOR))
            if client.last_stats:
                stats.append(dict(client.last_stats))
            n_calls = len(msg.get("tool_calls") or [])
            async with self:
                ls = client.last_stats or {}
                self._act_done(aid, f"{time.monotonic() - t_llm:.1f}с · ↑{ls.get('prompt', 0)} ↓{ls.get('out', 0)} токенів"
                                    + (f" · викликів тулів: {n_calls}" if n_calls else " · текстова відповідь"))
            calls, recovered = recover_tool_calls(msg)
            if not calls:
                content = (msg.get("content") or "").strip()
                # Великий файл дочитується частинами — якщо модель не продовжила
                # пагінацію сама, дочитуємо БЕЗ її участі (текстовий nudge тут
                # ненадійний, бачили живцем 15+ ідентичних повторів). Див. pending_continuation.
                cont = pending_continuation(messages)
                if cont is not None:
                    calls, recovered = [cont], True
                # Підштовхуємо РІВНО ОДИН раз, незалежно від того, чи вже були дії
                # раніше у цьому кроці (живий кейс на account_test_14.py: модель
                # почитала файл, потім видала порожню відповідь). НЕ ехо-имо
                # зламаний/порожній текст назад як assistant. Див. agent_loop.should_nudge.
                elif should_nudge(content, nudged):
                    nudged = True
                    messages.append({"role": "user", "content": NUDGE_TEXT})
                    continue
                else:
                    final = content; break
            messages.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""),
                             "tool_calls": calls})
            for call in calls:
                name = call.get("function", {}).get("name", "")
                args = _args(call)
                result = await self._dispatch_tool(reg, name, args, ctx)
                actions.append((name, result))
                if act.result_ok(result):
                    touched.extend(orch.touched_paths(name, args))
                messages.append({"role": "tool", "content": result})
                guard.observe(name, args, result)
            verdict = guard.check()
            if verdict:
                kind, reason = verdict
                _log.warning("зациклення (%s): %s | останній результат: %s", kind, reason,
                             guard.last_result[:120].replace(chr(10), " "))
                if kind == "stop":
                    final = guard.stuck_message(reason)
                    break
                messages.append({"role": "user", "content": WARN_TEXT.format(reason=reason, last=guard.last_result[:300])})
                async with self:
                    self._append("assistant", f"⚠ Помітив повтор без прогресу ({reason}) — прошу змінити підхід.", "note")
            ls = getattr(client, "last_stats", None) or {}
            if ls and context_full(ls.get("prompt", 0), limits.get().context_tokens):
                final = (f"{CONTEXT_FULL_PREFIX} ({ls.get('prompt')} токенів). "
                         "Розбийте задачу на менші кроки.")
                break
        log = [f"{n} -> {r[:80]}" for n, r in actions]

        parts: list[str] = []
        for name, result in actions:
            if name in ("edit_file", "create_from_source"):
                parts.append(f"**{name}**\n```diff\n{result}\n```")
            elif name == "run_shell":
                parts.append(f"**run_shell**\n```\n{result}\n```")
            else:   # list_dir / read_file тощо
                parts.append(f"**{name}**\n```\n{result[:4000]}\n```")
        log_md = "\n\n".join(parts)

        head = title or step_text[:80]
        content = f"**{head}**"
        if final:
            content += f"\n\n{final}"
        async with self:
            self._step_touched = list(dict.fromkeys(touched))
            self._add_stats(stats)                       # лічильник над полем вводу
            self._append("assistant", content, "step", thinking=log_md)
        return final, log

    async def _run_stream(self, gen):
        """Прокрутити стрім із живим лічильником токенів. Повідомлення НЕ додає —
        повертає (body, thinking, tool_calls). Спільне для answer і chat-режиму.
        Підтримує переривання через _STOP_EVENTS[current_id]."""
        async with self:
            self.streaming = True
            self.stream_content = ""
            self.stream_thinking = ""
            self.status = ""
            base_out = self.tok_out
            cid = self.current_id
        t0 = time.time()
        stats = None
        tool_calls = None
        counted = 0
        while True:
            if _STOP_EVENTS.get(cid, None) and _STOP_EVENTS[cid].is_set():
                break
            ev = await asyncio.to_thread(lambda g=gen: next(g, None))
            if ev is None:
                break
            if ev["done"]:
                stats = ev.get("stats")
                tool_calls = ev.get("tool_calls")
                break
            async with self:
                self.stream_content += ev["content"]
                self.stream_thinking += ev["thinking"]
                if ev["content"]:
                    counted += 1
                    self.tok_out = base_out + counted
                    el = time.time() - t0
                    if el > 0:
                        self.tok_tps = round(self.tok_out / el, 1)
        async with self:
            if stats:
                self.tok_prompt += stats.get("prompt", 0)
                self.tok_out = base_out + stats.get("out", counted)
                self.tok_eval_ns += stats.get("eval_ns", 0)
                if stats.get("model"):
                    self.tok_model = stats["model"]
                self._recompute_tps()
            body = (self.stream_content or "").strip()
            thinking = self.stream_thinking
            self.streaming = False
            self.stopping = False
            _STOP_EVENTS.pop(cid, None)
        return body, thinking, tool_calls

    # Тули, доступні read-only режимам (чат / answer): пошук + читання файлів/вкладень.
    _READONLY_TOOLS = ("web_search", "read_file", "list_dir", "read_attachment", "read_topic")

    # Заголовок read_attachment, коли файл читається частинами (toolkit.h_read_attachment):
    # "=== name (OFFSET-END з TOTAL) ===" — з'являється лише коли total > CHUNK_SIZE.
    _PAGINATED_HEADER_RE = re.compile(r"^=== (.+?) \(\d+-\d+ з \d+\) ===\n", re.MULTILINE)
    # Хвіст-підказка "лишилось N символів... offset=X)]" — лишаємо як є, щоб
    # agent_loop.pending_continuation продовжував працювати й після заміни на дайджест.
    _CONTINUATION_SUFFIX_RE = re.compile(r"\n\n…\[лишилось .*?\]$", re.DOTALL)

    async def _digest_attachment_chunk(self, name: str, raw_result: str,
                                       memory: dict[str, str], client, profile: dict) -> str:
        """Якщо raw_result — шматок ВЕЛИКОГО вкладення (пагінація), зливає його в
        накопичувальний дайджест (agent.convo.update_digest) і повертає дайджест +
        той самий continuation-хвіст замість сирого шматка — щоб контекст не
        роздувався накопиченням сирих шматків, а pending_continuation і далі бачив
        offset-підказку. Для звичайних (непагінованих) результатів — повертає
        raw_result без змін. profile: та сама модель, що й gather-фаза (chat -> gemma)."""
        header_match = self._PAGINATED_HEADER_RE.match(raw_result)
        if not header_match:
            return raw_result
        body = raw_result[header_match.end():]
        suffix_match = self._CONTINUATION_SUFFIX_RE.search(body)
        suffix = suffix_match.group(0) if suffix_match else ""
        chunk_text = body[:suffix_match.start()] if suffix_match else body
        prev = memory.get(name, "")
        events: list[tuple] = []
        digest = await asyncio.to_thread(
            lambda: convo.update_digest(prev, chunk_text, client, profile=profile,
                                        on_compress=lambda w, b, a: events.append((w, b, a, name))))
        if events:
            async with self:
                self._apply_compress_events(self.current_id, events)
        memory[name] = digest
        return f"=== {name} (дайджест прочитаного дотепер) ===\n{digest}{suffix}"

    async def _dispatch_tool_calls(self, tool_calls: list, msgs: list, reg, tctx,
                                   digest_memory: dict[str, str] | None = None, client=None,
                                   digest_profile: dict = config.EXECUTOR):
        """Виконати tool_calls: дописати результати в msgs + показати кроки в чаті.
        digest_memory (опційно): якщо передано — шматки великих вкладень (read_attachment
        пагінація) зливаються в дайджест (convo.update_digest) замість накопичення сирими
        шматками в контексті. Без цього параметра — поведінка як була."""
        import json as _json
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            args = call.get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except Exception:
                    args = {}
            label = args.get("name") or args.get("query") or args.get("path") or name
            async with self:
                self.status = f"{name}: {label}"[:60]
            async with self:
                aid = self._act_start("tool", name, act.summarize_args(name, args))
            t_tool = time.monotonic()
            result = await asyncio.to_thread(lambda n=name, a=args: reg.dispatch(n, a, tctx))
            async with self:
                self._act_done(aid, f"{time.monotonic() - t_tool:.1f}с · " + act.clip_result(result),
                               ok=act.result_ok(result))
            shown = result
            if digest_memory is not None and name == "read_attachment" and client is not None:
                async with self:
                    self.status = f"Узагальнюю прочитане ({label})…"
                shown = await self._digest_attachment_chunk(label, result, digest_memory, client,
                                                             digest_profile)
            msgs.append({"role": "tool", "content": shown})
            async with self:
                self._append("assistant", f"🔧 {name} · {label}", "step", thinking=shown)

    async def _synthesize_with_retry(self, msgs: list, client,
                                     profile: dict = config.PLANNER,
                                     max_attempts: int = 3) -> tuple[str, str]:
        """Стрім фінальної відповіді (think=on). Живий кейс: модель «застрягла» в
        роздумах (видно довгий thinking-монолог) і так і не дійшла до фінального
        контенту — body порожній, хоч thinking — ні. Той самий клас бага, що
        порожні tool_calls у фазі збору (agent_loop.should_nudge), але тут це
        ОКРЕМА точка (синтез, не збір), тож власний, простіший retry: повторні
        проходи з прямим нагадуванням відповісти, перш ніж здаватись.

        Другий живий кейс: gemma4 (think=on) інколи НЕ розділяє content/thinking
        сама (Ollama довіряє розділенню моделі, _stream_event лише читає готові
        поля) — увесь монолог роздумів англійською ("Wait", "Self-Correction"
        тощо, часто у фенс-блоці) потрапляє прямо в body. Це НЕ порожній body,
        тож стара перевірка цей випадок не ловила — користувач бачив сирий хаос
        замість відповіді. Тому додатково перевіряємо просочення евристикою
        _looks_like_leaked_reasoning.

        max_attempts=3 (не 2): живцем бачили, що на ВАЖКІЙ задачі (синтез 6
        файлів зі складними правилами джерел) ОДИН retry просочується знову й
        зрештою повертає (порожня відповідь) — модель просто не тягне задачу за
        один прохід синтезу. Третя спроба з тим самим нагадуванням іноді
        витягує чисту відповідь; якщо й вона не вийде — приймаємо що є (краще
        ніж нескінченний цикл)."""
        body, thinking, _ = await self._run_stream(client.chat_stream(msgs, profile=profile))
        for _ in range(max_attempts - 1):
            if (body or "").strip() and not (limits.get().weak_model_fixes
                                             and _looks_like_leaked_reasoning(body)):
                break
            if not (body or "").strip():
                nudge = ("Ти не дав фінальної відповіді (лише міркував). Дай КОНКРЕТНУ "
                          "відповідь користувачу зараз, без подальших роздумів.")
            else:
                nudge = ("Ти показав свої роздуми (самокорекції, службові позначки типу "
                          "\"Wait\"/\"Self-Correction\") прямо у відповіді замість чистого "
                          "тексту. Дай КОНКРЕТНУ фінальну відповідь користувачу зараз — без "
                          "жодних роздумів, самокорекцій чи службових позначок.")
            msgs.append({"role": "user", "content": nudge})
            body, thinking, _ = await self._run_stream(client.chat_stream(msgs, profile=profile))
        return body, thinking

    async def _run_tool_chat(self, msgs: list, tctx: ToolContext, client,
                             max_rounds: int = 6, gather_first: bool = False,
                             gather_profile: dict = config.EXECUTOR) -> str:
        """Тул-цикл для read-only режимів.

        gather_first=True (є прикріплені файли): фаза ЗБОРУ йде з think=OFF
        (gather_profile — за дефолтом EXECUTOR/Qwen для answer-режиму /КОД-аналізу;
        chat-режим передає config.CHAT_EXECUTOR/gemma — інакше chat випадково
        тягнув би кодинг-модель лише тому, що think=off надійніший для tool_calls)
        — модель надійно емітить tool_calls (read_attachment), а не «думає» про них
        без виклику; потім ОДИН фінальний синтез з think=ON (PLANNER/gemma, стрім,
        живий лічильник).

        gather_first=False (простий чат/web_search): think=ON стрім-цикл як є; якщо
        раунди вичерпано на тулах — форсований фінал без тулів."""
        reg = default_registry()
        tools = [t for t in reg.schema() if t["function"]["name"] in self._READONLY_TOOLS]

        if gather_first:
            nudged = False
            digest_memory: dict[str, str] = {}   # name -> накопичувальний дайджест великого файлу
            for _ in range(max_rounds):
                msg = await asyncio.to_thread(
                    lambda: client.chat(msgs, tools=tools, profile=gather_profile))
                tool_calls, recovered = recover_tool_calls(msg)
                if not tool_calls:
                    content = (msg.get("content") or "").strip()
                    # Великий файл читається частинами (read_attachment offset-пагінація,
                    # toolkit.ATTACHMENT_CHUNK_SIZE) — якщо модель не продовжила сама,
                    # дочитуємо БЕЗ її участі (текстовий nudge ненадійний, бачили живцем
                    # 15+ ідентичних повторів "продовжую читати" без жодної дії).
                    cont = pending_continuation(msgs)
                    # Модель НАРАТИВНО оголошує намір прочитати вкладення ("I will call
                    # read_attachment for all 6 files first.") і просто зупиняється —
                    # should_nudge дає лише ОДИН nudge на всю фазу збору, а слабка
                    # модель на важкій багатофайловій задачі стабільно повторює це
                    # навіть ПІСЛЯ нього (другий раз should_nudge мовчить через
                    # already_nudged) -> break без жодних даних -> "(порожня відповідь)"
                    # у фіналі (живий кейс). Імена файлів модель отримала через
                    # attachments_note — то й виконуємо її намір НАПРЯМУ, без надії на
                    # повторну співпрацю. Перевіряємо ще й буквальну згадку
                    # "read_attachment", щоб не зачепити намір про ІНШИЙ тул (run_shell тощо).
                    known = ([p.name for p in Path(tctx.attachments_dir).iterdir() if p.is_file()]
                             if tctx.attachments_dir and Path(tctx.attachments_dir).is_dir() else [])
                    intended = (extract_intended_attachments(content, known)
                                if known and limits.get().weak_model_fixes
                                and "read_attachment" in content else [])
                    if cont is not None:
                        tool_calls, recovered = [cont], True
                    elif intended:
                        tool_calls, recovered = (
                            [{"function": {"name": "read_attachment", "arguments": {"name": n}}}
                             for n in intended],
                            True,
                        )
                    # Якщо контент непорожній і не просочений — це справді "досить
                    # зібрано, йду відповідати" (валідний ранній вихід). Інакше раніше
                    # цикл просто `break`-ався без жодних зібраних даних (типово —
                    # вкладення взагалі не прочитане), звідси "(порожня відповідь)" на
                    # живому кейсі з великим paste-вкладенням. Див. agent_loop.should_nudge.
                    elif should_nudge(content, nudged):
                        nudged = True
                        msgs.append({"role": "user", "content": NUDGE_TEXT})
                        continue
                    else:
                        break
                msgs.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""),
                             "tool_calls": tool_calls})
                await self._dispatch_tool_calls(tool_calls, msgs, reg, tctx,
                                                digest_memory=digest_memory, client=client,
                                                digest_profile=gather_profile)
            async with self:
                self.status = "Формулюю відповідь…"
            body, thinking = await self._synthesize_with_retry(msgs, client)
            async with self:
                self._append("assistant", body or "(порожня відповідь)", "answer",
                             thinking=thinking)
            return body

        for _ in range(max_rounds):
            body, thinking, tool_calls = await self._run_stream(
                client.chat_stream(msgs, tools=tools, profile=config.PLANNER))
            if not tool_calls:                       # модель завершила — це фінальна відповідь
                if not (body or "").strip():
                    # Завершила без жодного тула, але й без контенту — той самий
                    # "застрягла в роздумах" живий кейс, тут без передісторії тулів.
                    body, thinking = await self._synthesize_with_retry(msgs, client)
                async with self:
                    self._append("assistant", body, "answer", thinking=thinking)
                return body
            msgs.append({"role": "assistant", "content": body, "tool_calls": tool_calls})
            await self._dispatch_tool_calls(tool_calls, msgs, reg, tctx)
        # Раунди вичерпано на викликах тулів → змушуємо модель відповісти зібраним (без тулів).
        async with self:
            self.status = "Формулюю відповідь…"
        msgs.append({"role": "user",
                     "content": "Достатньо читання. Дай повну відповідь українською за "
                                "зібраними даними, більше не викликай тулів."})
        body, thinking = await self._synthesize_with_retry(msgs, client)
        async with self:
            self._append("assistant", body or "(не вдалося сформувати відповідь)",
                         "answer", thinking=thinking)
        return body

    async def _estimate_rounds(self, text: str, names: list, client,
                               profile: dict = config.EXECUTOR, cid: str = "") -> int:
        """Адаптивний ліміт раундів тул-циклу. Коли є прикріплені файли — оцінюємо
        через _estimate_iters (модель каже n тул-викликів → max(6, n*2)), щоб ліміт
        масштабувався під кількість/складність файлів, а не впирався в хардкод. Без
        файлів — дефолт 6 (ліміт усе одно не зв'язує простий чат: він завершується
        першим раундом без тулів). profile: chat-режим передає CHAT_EXECUTOR (gemma).

        Модель не знає РЕАЛЬНОГО розміру файлу (бачить лише імʼя), тож її власна
        оцінка систематично занижена для великих вкладень — живий кейс: цикл
        вичерпав раунди на півдорозі через 386KB файл і пішов на синтез із
        недочитаним дайджестом (обірвана відповідь). Розмір ми знаємо ДЕТЕРМІНОВАНО
        (без LLM) — рахуємо мінімум раундів від нього напряму й беремо максимум з
        оцінкою моделі, а не покладаємось лише на її вгадування."""
        if not names:
            return 6
        size_floor = 6
        if cid:
            sizes = {n["name"]: n["size"] for n in A.list_saved(cid)}
            chunk = limits.get().chunk
            total_chunks = sum(-(-sizes.get(n, 0) // chunk) for n in names)
            if total_chunks:
                size_floor = total_chunks + 4   # +4 запас на нюджі/нечитальні раунди
        model_estimate = await asyncio.to_thread(
            lambda: _estimate_iters(text, "files: " + "; ".join(names), client, profile=profile))
        return max(model_estimate, size_floor)

    @staticmethod
    def _inject_files(content: str, cur_names: list, other_names: list) -> str:
        """Вшити в текст повідомлення ДВА переліки файлів, щоб модель зорієнтувалась:
        - cur_names: прикріплені САМЕ до цього повідомлення → опрацювати;
        - other_names: інші файли розмови → перечитати через read_attachment ЛИШЕ якщо
          запит стосується саме їх (інакше вони вже в контексті через підсумок).
        Усі читаються тулом read_attachment(name)."""
        cur = [n for n in (cur_names or []) if n]
        others = [n for n in (other_names or []) if n]
        if not cur and not others:
            return content
        lines = ["[Доступні файли — читай тулом read_attachment(name):"]
        if cur:
            lines.append("• Додані до ЦЬОГО повідомлення (опрацюй): " + "; ".join(cur))
        if others:
            lines.append("• Інші файли розмови (перечитуй ЛИШЕ якщо запит саме про них): "
                         + "; ".join(others))
        return content + "\n\n" + "\n".join(lines) + "]"

    @staticmethod
    def _attach_or_reference(cid: str, content: str, cur_names: list, other_names: list) -> tuple[str, bool]:
        """Якщо файли цього повідомлення (cur_names) РАЗОМ малі (A.INLINE_BUDGET) —
        вшити їхній ВМІСТ напряму в текст (один LLM-виклик, без тул-циклу читання).
        Інакше — лишити посилання на тул read_attachment, як і для other_names.
        Повертає (новий_текст, чи_лишились_файли_що_потребують_тула)."""
        cur = [n for n in (cur_names or []) if n]
        others = [n for n in (other_names or []) if n]
        if cur and A.total_size(cid, cur) <= limits.get().inline:
            inlined = A.read_combined(cid, cur)
            content = content + "\n\n[Вкладені файли цього повідомлення — вміст нижче]\n" + inlined
            cur = []
        new_content = State._inject_files(content, cur, others)
        return new_content, bool(cur or others)

    async def _chat_reply(self, text: str, client, att_note: str = "") -> str:
        """Чат-режим: думаюча модель + read-only тули. У поточну user-репліку вшиваємо
        імена файлів, прикріплених САМЕ до неї (m['attachments']); модель читає їх через
        read_attachment (attachments_dir = тека вкладень сесії)."""
        async with self:
            cid = self.current_id
            proj_id = self.chat_project_id
            cur_atts = list(self.messages[-1].get("attachments", []) or []) if self.messages else []
            history = [{"role": m["role"], "content": m["content"]}
                       for m in self.messages
                       if m["role"] in ("user", "assistant")
                       and m.get("kind", "text") in ("text", "answer") and m["content"]][-limits.get().history_msgs:]
        proj = CP.load_project(proj_id) if proj_id else None
        project_block = CP.as_system_block(proj.prompt) if proj else ""
        all_names = [n["name"] for n in A.list_saved(cid)]
        other_names = [n for n in all_names if n not in cur_atts]
        needs_tool_files = bool(other_names)
        # Вшити переліки файлів у ОСТАННЮ user-репліку (поточний хід), якщо файли є.
        # cur_atts (файли САМЕ цього повідомлення) вшиваються ВМІСТОМ напряму, якщо
        # сумарно малі (A.INLINE_BUDGET) — без жодного read_attachment-раунду;
        # other_names лишаються лише посиланням (тулом, "якщо запит саме про них").
        if all_names and history and history[-1]["role"] == "user":
            new_content, needs_tool_files = self._attach_or_reference(
                cid, history[-1]["content"], cur_atts, other_names)
            history[-1] = {**history[-1], "content": new_content}
        tctx = ToolContext(root=A.session_dir(cid),
                           permissions={"edits": "off", "shell": "off"}, client=client,
                           attachments_dir=A.session_dir(cid))
        all_topics = await asyncio.to_thread(topics.list_topics)
        topics_note = topics.available_topics_note(all_topics)
        # project_block ПЕРШИМ — інструкції проекту задають рамку розмови, тема й
        # вкладення йдуть як додатковий контекст усередині цієї рамки.
        system = project_block + CHAT_SYSTEM + ("\n\n" + topics_note if topics_note else "")
        msgs = [{"role": "system", "content": system}] + history
        rounds = await self._estimate_rounds(text, cur_atts or all_names, client,
                                             profile=config.CHAT_EXECUTOR, cid=cid)
        # think=off-збір ПОТРІБЕН лише якщо ЛИШИЛИСЬ файли/теми, що читаються тулом
        # (other_names чи topics) — інакше модель НАДІЙНО викликала б read_attachment
        # (think=on часто «думає» про тул, але не викликає). Якщо все вже вшито
        # напряму (needs_tool_files=False, без тем) — прямий think=on-стрім без
        # зайвого тул-циклу взагалі: саме та "обробка за один виклик", яку просили.
        # gather_profile=CHAT_EXECUTOR (gemma) — chat-режим не торкає кодинг-модель.
        return await self._run_tool_chat(msgs, tctx, client, rounds,
                                         gather_first=bool(needs_tool_files or all_topics),
                                         gather_profile=config.CHAT_EXECUTOR)

    # Системний промпт answer-режиму В ТУЛ-ЦИКЛІ: на відміну від answerer.SYSTEM, явно
    # каже про read_file/list_dir — інакше модель «просить користувача прислати файли»
    # замість того, щоб прочитати їх самій (той самий баг, що зупиняв аналіз після плану).
    _ANSWER_TOOLS_SYSTEM = (
        "You are a code analysis assistant for a local project. You have tools: "
        "read_file(path) and list_dir(path) to inspect ANY file in the project, plus "
        "web_search. The project structure is in the context. To answer ACCURATELY you "
        "MUST read the relevant files yourself with read_file — NEVER ask the user to "
        "paste or provide file contents, you can read them directly. Read the files you "
        "need, then answer concisely, citing the file/function. BUT if the recent conversation "
        "or summary in the context already contains what is needed (e.g. you analysed this "
        "project a moment ago), answer from it WITHOUT re-reading; read only what is missing "
        "or may have changed. If the user clarifies what they meant (e.g. 'it was just a "
        "question'), answer THAT — do not redo earlier work. Do NOT propose code edits "
        "unless asked. Answer in the user's language.\n"
        "IMPORTANT about your capabilities: this is the read-only ANALYSIS mode, so here "
        "you only have read tools. But the agent CAN edit files and run shell commands "
        "(git commit/push, run tests, etc.) via run_shell/edit_file in its EXECUTION mode. "
        "So if the user asks you to DO something (commit, push, run, modify a file), do NOT "
        "say you are unable — instead tell them to phrase it as a task (e.g. 'commit and "
        "push the changes') so the agent runs it in execution mode."
    )

    async def _answer_reply(self, text: str, ctx: str, att_note: str, root: str, client) -> str:
        """Answer-режим (аналіз коду): думаюча модель + read-only тули. root = корінь
        проєкту, тож модель читає і файли проєкту, і прикріплені (за повним шляхом)."""
        ANSWER_SYSTEM = self._ANSWER_TOOLS_SYSTEM
        async with self:
            cid = self.current_id
            cur_atts = list(self.messages[-1].get("attachments", []) or []) if self.messages else []
        tctx = ToolContext(root=Path(root) if root else Path.home(),
                           permissions={"edits": "off", "shell": "off"}, client=client,
                           attachments_dir=A.session_dir(cid), track_project=bool(root),
                           extra_roots=[Path(x) for x in self.extra_dirs])
        all_names = [n["name"] for n in A.list_saved(cid)]
        other_names = [n for n in all_names if n not in cur_atts]
        # cur_atts малі сумарно -> вшиваємо вмістом напряму (менше тул-раундів);
        # answer-режим однаково forced gather_first=True нижче (читає й код проєкту).
        question, _ = self._attach_or_reference(cid, text, cur_atts, other_names)
        parts = [f"Контекст:\n{ctx}" if ctx else "", f"Питання:\n{question}"]
        user = "\n\n".join(p for p in parts if p)
        msgs = [{"role": "system", "content": ANSWER_SYSTEM},
                {"role": "user", "content": user}]
        rounds = await self._estimate_rounds(text, cur_atts or all_names, client, cid=cid)
        # answer-режим = аналіз коду: ЗАВЖДИ think=off-збір, щоб модель надійно читала
        # файли проєкту (read_file/list_dir). Без цього вона лише «планує» прозою й
        # зупиняється, нічого не прочитавши (саме той баг «стоп після планування»).
        # Фінальний синтез усе одно think=on (у _run_tool_chat).
        return await self._run_tool_chat(msgs, tctx, client, rounds, gather_first=True)

    async def _maybe_auto_title(self, user_text: str, client):
        """Автоперейменування "Новий чат" -> коротка осмислена назва за першим
        повідомленням (convo.generate_title), один раз. Той самий cid-гард, що й
        _update_summary — поки летів LLM-виклик, користувач міг перемкнути чат.
        Дешевий некритичний виклик: будь-яка проблема (порожній результат,
        кривий title) -> просто лишаємо дефолтну назву, без retry/нюджів."""
        async with self:
            cid = self.current_id
            chat = self.mode == "chat"
            still_default = self.title in ("", "Новий чат")
        if not still_default:
            return
        title = await asyncio.to_thread(
            lambda: convo.generate_title(user_text, client, kind="chat" if chat else "code"))
        if not title:
            return
        async with self:
            if self.current_id == cid:
                self.title = title
            if cid:
                s = sess.load_session(cid)
                if s.title in ("", "Новий чат"):
                    s.title = title
                    sess.save_session(s)
                    self.sessions = sess.list_sessions()

    async def _update_summary(self, user_text: str, outcome: str):
        """Оновлення контекст-памʼяті (LLM-підсумок) після завершеного запиту. Модель
        однопотокова, тож це триває в межах busy; нові повідомлення стають у чергу
        (queued_text) і виконаються після — вже зі свіжим стисненим контекстом.
        cid-гард: якщо користувач перемкнув чат, пишемо в сесію-походження."""
        if not (outcome or "").strip():
            return
        client = await asyncio.to_thread(get_client)
        async with self:
            cur = self.context_summary
            cid = self.current_id
            chat = self.mode == "chat"
            kind = "chat" if chat else "code"
            cur_topics = list(self.session_topics)
        # chat: загальний підсумок розмови менше потрібен у деталях (деталі живуть у
        # темах нижче) — зменшуємо бюджет умовно втричі, як домовлено з користувачем.
        budget = limits.get().summary // 3 if chat else limits.get().summary
        events: list[tuple] = []
        note_cb = lambda w, b, a: events.append((w, b, a, ""))     # noqa: E731
        new = await asyncio.to_thread(
            lambda: convo.update_summary(cur, user_text, outcome, client, budget=budget, kind=kind,
                                         on_compress=note_cb))
        new_topics = cur_topics
        if chat:
            new_topics = await asyncio.to_thread(
                lambda: self._update_topic(user_text, outcome, cur_topics, client, note_cb))
        if events:                                     # побажання розробників: казати про стиснення
            async with self:
                self._apply_compress_events(cid, events)
        async with self:
            if self.current_id == cid:                # та сама сесія — оновити й у State
                self.context_summary = new
                self.session_topics = new_topics
            if cid:                                   # у файл — завжди в сесію-походження
                s = sess.load_session(cid)
                s.context_summary = new
                s.topics = new_topics
                sess.save_session(s)

    def _apply_compress_events(self, cid: str, events: list[tuple]) -> None:
        """Подія стиснення -> нотатка в чаті (якщо ця сесія відкрита) + запис в історію
        стискань сесії (панель «Контекст»). Викликати всередині `async with self`."""
        recs = [{"what": w, "before": b, "after": a, "name": n, "ts": time.time()}
                for (w, b, a, n) in events]
        if self.current_id == cid:
            for (w, b, a, n) in events:
                self._append("assistant", self._compress_msg(w, b, a, n), "note")
            self.compress_log = [*self.compress_log, *recs]
        if cid:
            s = sess.load_session(cid)
            s.compressions = [*s.compressions, *recs]
            sess.save_session(s)

    @staticmethod
    def _compress_msg(what: str, before: int, after: int, name: str = "") -> str:
        label = f"{what} «{name}»" if name else what
        return (f"🗜 Стиснув контекст: {label} — було {before:_} → стало {after:_} символів "
                "(старі деталі згорнуто, щоб вміститись у вікно моделі).").replace("_", " ")

    def _update_topic(self, user_text: str, outcome: str, cur_topics: list[str], client,
                      on_compress=None) -> list[str]:
        """Класифікувати цей хід розмови за темою (нова чи існуюча — список тем
        СПІЛЬНИЙ для всього chat-режиму, не лише цієї сесії), злити в нотатку теми
        й зберегти на диск. Повертає оновлений список тем ЦІЄЇ сесії (для підказки
        моделі в наступних ходах). Синхронна (LLM-виклики) — викликати з to_thread."""
        existing = topics.list_topics()
        name = topics.classify_topic(user_text, outcome, existing, client, profile=config.CHAT_EXECUTOR)
        prev = topics.load_topic(name)
        note = topics.update_topic_note(prev, user_text, outcome, client, profile=config.CHAT_EXECUTOR,
                                        on_compress=on_compress)
        if not note.strip():
            # Куций хід (нема що класифікувати) -> модель не дала нічого корисного
            # (update_topic_note вже відкинула луна-плейсхолдери) — НЕ створюємо
            # сміттєвий файл теми. Живий кейс: "Новий topic.txt" з вмістом "(порожньо)".
            return cur_topics
        topics.save_topic(name, note)
        return cur_topics if name in cur_topics else [*cur_topics, name]

    @rx.event
    def stop_generation(self):
        """Перервати поточну генерацію: встановити stop_event для активної сесії."""
        import threading
        cid = self.current_id
        if not cid:
            return
        ev = _STOP_EVENTS.get(cid)
        if ev:
            ev.set()
        else:
            # якщо stop_event ще не зареєстровано — створюємо вже встановленим
            e = threading.Event()
            e.set()
            _STOP_EVENTS[cid] = e
        self.stopping = True

    @rx.event(background=True)
    async def send_task(self, form_data: dict | None = None):
        """Надсилання. Модель однопотокова: якщо зайнята (busy) — повідомлення стає в
        чергу (додається до наявного) і виконається після з уже свіжим контекстом."""
        async with self:
            if not self.current_id:
                s = sess.new_session("Новий чат", "", kind=self.mode)
                if self.mode == "chat" and self.current_project_id:
                    s.project_id = self.current_project_id
                sess.save_session(s)
                self.sessions = sess.list_sessions()
                pre = list(self.attachments)            # chips, прикріплені до створення сесії
                self._select(s.id)
                A.rename_session("_tmp", s.id)          # перенести файли _tmp → сесія
                self.attachments = pre                  # _select затер chips — повертаємо
            # Тека, введена в полі на головній сторінці, ще не збережена в сесію
            # (save_folder окремо не викликали). Застосовуємо ДО перевірки — інакше
            # надсилання з головної з уведеною текою хибно скаржиться «вкажіть теку».
            if not self.project_root and self.folder_input.strip():
                self.project_root = self.folder_input.strip()
                if self.title in ("", "Новий чат"):
                    self.title = Path(self.project_root).name
                self._save_current()
            if self.mode != "chat" and not self.project_root:
                self._append("assistant", "Спершу вкажіть робочу теку.", "note")
                return
            text = self.task.strip()
            if not text:
                return
            self.task = ""
            att_names = [a["name"] for a in self.attachments if not a.get("error")]
            # Великий вставлений/набраний текст у полі вводу не має жодного ліміту
            # (на відміну від файлових вкладень — MAX_FILE_SIZE) і летить прямо в
            # промпт, переповнюючи контекст (живий кейс: 687k символів — модель
            # падає). Той самий ліміт, що й для файлів — понад нього зберігаємо як
            # вкладення на диск; модель читає частинами через read_attachment, а не
            # отримує суцільний блок у контексті.
            if len(text) > limits.get().max_file:
                kb = len(text) // 1024
                paste_name = f"paste_{int(time.time())}.txt"
                A.save(self.current_id, paste_name, text)
                att_names.append(paste_name)
                text = (f"[Вставлений текст ({kb} KB) занадто великий для прямого "
                        f"включення — збережено як вкладення '{paste_name}'. Прочитай "
                        f"його частинами через read_attachment('{paste_name}').]")
            # Імена прикріплених файлів → у саме повідомлення (видно в чаті), потім
            # стейджинг-чіпи прибираємо з-під інпута. Файли лишаються на диску.
            self._append("user", text, attachments=att_names)
            self.attachments = []
            if self.has_pending and not self.busy and orch.is_confirmation(text):
                self._append("assistant", "Це підтвердження плану — натисніть «Виконати» під планом (або «Відхилити»).", "note")
                return
            if self.busy or self.has_pending or self.has_question:                              # модель зайнята або чекає на дію користувача -> у чергу
                self.queued_text = (self.queued_text + "\n\n" + text).strip() if self.queued_text else text
                return
            self.busy = True
        await self._process_one(text)
        await self._drain_and_unbusy()

    async def _drain_and_unbusy(self):
        """Після завершення запиту: якщо в черзі є повідомлення — виконати його (busy
        тримається), інакше зняти busy. Перевірка + зняття — атомарні, без втрати черги."""
        while True:
            await self._refresh_git_async()                  # зміни/гілка після завершеного запиту
            async with self:
                if self.queued_text:
                    nxt = self.queued_text
                    self.queued_text = ""
                    self.status = "Обробка запиту з черги…"
                else:
                    self.busy = False
                    self.status = ""
                    self.stopping = False                 # скидаємо стоп-стан на всіх шляхах
                    _STOP_EVENTS.pop(self.current_id, None)
                    return
            await self._process_one(nxt)

    async def _process_one(self, text: str):
        """Обробити ОДНЕ повідомлення (user-бульбашку вже додано). Включає фінальний
        LLM-підсумок контексту. busy не чіпає — ним керує send_task/_drain_and_unbusy.

        Винятки (обрив зʼєднання з Ollama, кривий JSON тощо) ловимо тут і показуємо в
        чаті — інакше виняток вилітає з background-події ДО того, як has_pending/
        has_question/busy встигають скинутись, і кнопки/черга висять назавжди."""
        logs.session_var.set(self.current_id or "-")      # id чату в усіх рядках лога цього запиту
        try:
            await self._process_one_inner(text)
        except Exception as e:
            _log.exception("збій обробки повідомлення")
            async with self:
                self.has_pending = False
                self.has_question = False
                self._append("assistant", f"⚠ Помилка: {e}", "note")

    async def _process_one_inner(self, text: str):
        async with self:
            self._reset_tokens()
            self.activity = []                     # нова стрічка подій для цього запиту
            # Свіжа стоп-подія на кожен запит, щоб stop_generation і виконавець кроків
            # завжди працювали з одним і тим самим обʼєктом (а не None, захопленим зарано).
            _STOP_EVENTS[self.current_id] = threading.Event()
            chat_mode = self.mode == "chat"
            root = self.project_root
            refs = [str(x) for x in self.references]
            plan_first = self.plan_first or self.edits == "ask"
            summary = self.context_summary
            att_note = self._attachments_note()     # шляхи прикріплених файлів (модель читає сама)
            last_ai = orch.last_assistant_text(self.messages)      # ПОВНА попередня відповідь (не підсумок)
            dialogue = orch.recent_dialogue(self.messages)         # останні репліки діалогу (не лише підсумок)
            answering = self.has_question
            if answering:
                self.has_question = False
                self.status = "Обмірковую відповідь…"
            else:
                self.status = "Думаю…" if chat_mode else "Визначаю тип запиту…"

        client = await asyncio.to_thread(get_client)
        su, so = text, ""
        pmap = ""                                     # «Карта проєкту» — у контекст усіх режимів код-режиму
        if not chat_mode and root:
            pmap = await asyncio.to_thread(lambda: projectmap.context_block(root))
            pmap = (pmap + "\n\n") if pmap else ""

        if chat_mode:
            so = await self._chat_reply(text, client, att_note)
        elif answering:
            su, so = await self._resume_planning(text, root, plan_first, client)
        else:
            approved = orch.expand_confirmation(text, last_ai)      # «роби» = виконай щойно запропоноване
            if approved:
                mode, task_text = "edit", approved
                su = f"{text} (виконати запропоноване: {last_ai[:160]}…)"
                async with self:
                    self._append("assistant", "▶ Сприймаю це як команду виконати те, що щойно було запропоновано.", "note")
            else:
                task_text = text
                mode = await asyncio.to_thread(
                    lambda: classify_intent(text, client, context=orch.context_for_intent(last_ai)))
            if mode == "answer":
                async with self:
                    self.status = "Аналізую код…"
                ctx = await asyncio.to_thread(lambda: build_context(root, refs, text))
                so = await self._answer_reply(text, convo.as_context(summary) + dialogue + pmap + ctx,
                                              att_note, root, client)
            elif mode == "shell":
                async with self:
                    self.status = "Виконую запит…"
                ctx = self._exec_ctx(root, client)
                rn = self._roots_note()
                shell_ctx = (f"Project root: {root}\n{rn}{pmap}{convo.as_context(summary)}" if summary
                             else f"Project root: {root}\n{rn}{pmap}")
                if att_note:
                    shell_ctx += "\n" + att_note
                final, _ = await self._run_tool_step(text, ctx, context=shell_ctx,
                                                     stop_event=_STOP_EVENTS.get(self.current_id))
                so = final or "виконано дії інструментами"
            else:  # plan / edit -> планувальник-діалог
                async with self:
                    self.status = "Обмірковую план…"
                doc = await asyncio.to_thread(lambda: load_project_doc(root))
                struct = await asyncio.to_thread(lambda: scan_structure(root))
                su2, so = await self._deliberate_flow(
                    task_text, convo.as_context(summary) + dialogue + pmap + struct, doc, [], root, plan_first, client)
                su = su2 if not approved else su

        if (so or "").strip():
            await self._maybe_auto_title(text, client)
            async with self:
                self.status = "Оновлюю памʼять…"
            await self._update_summary(su, so)
        # Файли вкладень лишаються на диску (ручна чистка) — нічого не скидаємо.

    @rx.event(background=True)
    async def answer_planner(self, value: str):
        """Відповідь на питання планувальника кліком по кандидату."""
        async with self:
            if not self.has_question or self.busy or not self.current_id:
                return
            self.busy = True
            self._reset_tokens()
            _STOP_EVENTS[self.current_id] = threading.Event()   # свіжа стоп-подія на запит
            self._append("user", value)
            self.has_question = False
            self.status = "Обмірковую відповідь…"
            root = self.project_root
            plan_first = self.plan_first or self.edits == "ask"
        client = await asyncio.to_thread(get_client)
        task, outcome = value, ""
        try:
            task, outcome = await self._resume_planning(value, root, plan_first, client)
        except Exception as e:
            async with self:
                self.has_pending = False
                self.has_question = False
                self._append("assistant", f"⚠ Помилка: {e}", "note")
        if (outcome or "").strip():
            async with self:
                self.status = "Оновлюю памʼять…"
            await self._update_summary(task, outcome)
        await self._drain_and_unbusy()

    async def _resume_planning(self, value: str, root: str, plan_first: bool, client):
        """Продовжити планування після відповіді користувача: додати її в history,
        повторити deliberate. Повертає (task, outcome)."""
        async with self:
            s = sess.load_session(self.current_id)
            q = s.pending_question or {}
        history = list(q.get("history") or []) + [{"question": q.get("question", ""), "answer": value}]
        task = q.get("task", value)
        return await self._deliberate_flow(task, q.get("context", ""), q.get("doc"),
                                           history, root, plan_first, client)

    async def _deliberate_flow(self, task: str, context: str, doc, history: list,
                               root: str, plan_first: bool, client):
        """Один хід планувальника-діалогу: clarify/choose -> питання+кнопки (пауза);
        plan -> готовий план (виконати або показати на підтвердження).
        Повертає (task, outcome) — outcome='' якщо пауза/не виконано (підсумок не потрібен)."""
        async with self:
            att_note = self._attachments_note()
        full_context = (context + "\n\n" + att_note).strip() if att_note else context
        result = await asyncio.to_thread(
            lambda: deliberate(task, full_context, client, doc, history))
        action = result["action"]

        if action in ("clarify", "choose"):
            q = {"task": task, "context": context, "doc": doc, "history": history,
                 "question": result["question"], "reasoning": result["reasoning"],
                 "options": result["options"]}
            async with self:
                head = (result["reasoning"] + "\n\n") if result["reasoning"] else ""
                self._append("assistant", f"{head}**{result['question']}**", "plan")
                self.q_text = result["question"]
                self.q_reasoning = result["reasoning"]
                self.q_options = result["options"]
                self.has_question = True
                s = sess.load_session(self.current_id)
                s.set_pending_question(q)
                s.clear_pending_plan()
                sess.save_session(s)
            return task, ""

        # action == "plan"
        state = result["state"]
        async with self:
            s = sess.load_session(self.current_id)
            s.clear_pending_question()
            sess.save_session(s)
            self._load_question(None)
        if not state or not state.steps:
            async with self:
                self._append("assistant", "Не вдалося скласти план.", "note")
            return task, ""
        big = orch.is_big_plan(state)                 # ≥4 кроків — показуємо на схвалення НАВІТЬ без «план наперед»
        if plan_first or big:
            n_act = sum(1 for s in state.steps if not s.is_noop)
            head = (f"⚠ **Великий план: {n_act} кроків.** Перегляньте й підтвердьте (кнопка «Виконати»), "
                    "перш ніж я почну.\n\n") if big else ""
            async with self:
                self._append("assistant", head + render_for_user(state), "plan")
                s = sess.load_session(self.current_id)
                s.set_pending_plan(state)
                sess.save_session(s)
                self.has_pending = True
            return task, ""                           # виконається пізніше -> підсумок там
        outcome = await self._execute_steps(state, root, client)
        return task, outcome

    @rx.event(background=True)
    async def execute_pending(self):
        async with self:
            if not self.has_pending or not self.current_id or self.busy:
                return
            self.busy = True
            self.status = "Виконую план…"
            _STOP_EVENTS[self.current_id] = threading.Event()   # свіжа стоп-подія на запит
            root, cid = self.project_root, self.current_id
            # Кнопки «Виконати/Відхилити» прив'язані до has_pending — ховаємо їх ОДРАЗУ
            # на старті (план уже беремо з диска нижче), інакше вони висять увесь час
            # виконання й зникають лише наприкінці.
            self.has_pending = False
            s0 = sess.load_session(cid)
            plan = s0.get_pending_plan()
            s0.clear_pending_plan()
            sess.save_session(s0)

        client = await asyncio.to_thread(get_client)
        outcome = ""
        try:
            outcome = await self._execute_steps(plan, root, client)
        except Exception as e:
            async with self:
                self._append("assistant", f"⚠ Помилка виконання: {e}", "note")
        if (outcome or "").strip():
            async with self:
                self.status = "Оновлюю памʼять…"
            await self._update_summary(plan.task, outcome)
        await self._drain_and_unbusy()

    @rx.event(background=True)
    async def discard_pending(self):
        async with self:
            if self.current_id:
                s = sess.load_session(self.current_id)
                s.clear_pending_plan()
                sess.save_session(s)
            self.has_pending = False
        await self._drain_and_unbusy()           # черга могла накопичитись поки план чекав

    @rx.event(background=True)
    async def cancel_question(self):
        async with self:
            if self.current_id:
                s = sess.load_session(self.current_id)
                s.clear_pending_question()
                sess.save_session(s)
            self._load_question(None)
        await self._drain_and_unbusy()            # те саме для скасованого питання


def nav_item(icon: str, label: str, on_click=None) -> rx.Component:
    return rx.hstack(
        rx.icon(icon, size=16, class_name="text-gray-400"),
        rx.text(label, class_name="text-sm text-gray-200"),
        on_click=on_click,
        class_name="items-center gap-3 px-2 py-1.5 rounded-lg hover:bg-white/5 "
                   "cursor-pointer w-full",
    )


def session_item(s: dict) -> rx.Component:
    active = State.current_id == s["id"]
    return rx.hstack(
        rx.hstack(
            rx.icon("message-square", size=14, class_name="text-gray-500 shrink-0"),
            rx.text(s["title"], class_name="text-sm text-gray-200 truncate"),
            on_click=lambda: State.select_chat(s["id"]),
            class_name="items-center gap-2 grow min-w-0 cursor-pointer",
        ),
        rx.menu.root(
            rx.menu.trigger(
                rx.icon("ellipsis", size=14,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0"),
            ),
            rx.menu.content(
                rx.menu.item("Перейменувати",
                             on_click=lambda: State.open_rename(s["id"], s["title"])),
                rx.cond(
                    s["kind"] == "chat",
                    rx.menu.item("Перенести…", on_click=lambda: State.open_move_dialog(s["id"])),
                    rx.fragment(),
                ),
                rx.menu.item("Видалити", on_click=lambda: State.delete_chat(s["id"]),
                             class_name="text-red-400"),
            ),
        ),
        class_name=rx.cond(active, "bg-white/10", "hover:bg-white/5")
        + " items-center gap-1 px-2 py-1.5 rounded-lg w-full",
    )


def rename_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Перейменувати чат"),
            rx.input(value=State.rename_input, on_change=State.set_rename_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_rename_open(False)),
                rx.button("Зберегти", on_click=State.save_rename),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.rename_open,
        on_open_change=State.set_rename_open,
    )


def project_item(p: dict) -> rx.Component:
    """Рядок проекту в сайдбарі: клік — фільтр чатів цього проекту; 3-крапки —
    редагувати промпт / видалити."""
    active = State.current_project_id == p["id"]
    return rx.hstack(
        rx.hstack(
            rx.icon("folder", size=14, class_name="text-gray-500 shrink-0"),
            rx.text(p["name"], class_name="text-sm text-gray-200 truncate"),
            on_click=lambda: State.select_project(p["id"]),
            class_name="items-center gap-2 grow min-w-0 cursor-pointer",
        ),
        rx.menu.root(
            rx.menu.trigger(
                rx.icon("ellipsis", size=14,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0"),
            ),
            rx.menu.content(
                rx.menu.item("Редагувати", on_click=lambda: State.open_edit_project(p["id"])),
                rx.menu.item("Видалити", on_click=lambda: State.delete_project_event(p["id"]),
                             class_name="text-red-400"),
            ),
        ),
        class_name=rx.cond(active, "bg-white/10", "hover:bg-white/5")
        + " items-center gap-1 px-2 py-1.5 rounded-lg w-full",
    )


def project_dialog() -> rx.Component:
    """Створення/редагування проекту: назва + промпт (до 5000 символів), що
    вшивається в системний промпт кожного повідомлення чатів цього проекту."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title(rx.cond(State.project_edit_id != "", "Редагувати проект", "Новий проект")),
            rx.input(placeholder="Назва проекту", value=State.project_name_input,
                     on_change=State.set_project_name_input, class_name="w-full mt-2"),
            rx.text_area(
                placeholder="Інструкції проекту — додаються до кожного повідомлення в чатах "
                            "цього проекту (до 5000 символів)",
                value=State.project_prompt_input, on_change=State.set_project_prompt_input,
                max_length=CP.PROMPT_MAX_CHARS, rows="8", class_name="w-full mt-2",
            ),
            rx.text("До 5000 символів — зайве буде обрізано",
                    class_name="text-xs text-gray-500 mt-1"),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_project_dialog_open(False)),
                rx.button("Зберегти", on_click=State.save_project_dialog),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.project_dialog_open,
        on_open_change=State.set_project_dialog_open,
    )


def move_dialog() -> rx.Component:
    """Перенести чат у проект (чи "без проекту")."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Перенести чат у проект"),
            rx.vstack(
                rx.box(
                    rx.text("Без проекту", class_name="text-sm text-gray-200"),
                    on_click=lambda: State.move_chat_to_project(""),
                    class_name="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer w-full",
                ),
                rx.foreach(
                    State.projects,
                    lambda p: rx.box(
                        rx.text(p["name"], class_name="text-sm text-gray-200"),
                        on_click=lambda: State.move_chat_to_project(p["id"]),
                        class_name="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer w-full",
                    ),
                ),
                class_name="w-full gap-1 mt-2 max-h-72 overflow-y-auto",
            ),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_move_dialog_open(False)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.move_dialog_open,
        on_open_change=State.set_move_dialog_open,
    )


def confirm_dialog() -> rx.Component:
    """Попап для shell=ask: підтвердити запуск команди. Керований лише кнопками
    (без on_open_change), щоб закриття не лишило воркер у вічному очікуванні."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Виконати команду в консолі?"),
            rx.code_block(State.confirm_text, language="powershell",
                          class_name="w-full mt-2"),
            rx.flex(
                rx.button("Відхилити", variant="soft", color_scheme="red",
                          on_click=State.confirm_no),
                rx.button("Виконати", on_click=State.confirm_yes),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.confirm_open,
    )


def mode_switch() -> rx.Component:
    """Верхній перемикач режимів Чат | Код."""
    def tab(label: str, m: str, icon: str) -> rx.Component:
        active = State.mode == m
        return rx.hstack(
            rx.icon(icon, size=14),
            rx.text(label, class_name="text-sm"),
            on_click=lambda: State.set_mode(m),
            class_name=rx.cond(active, "bg-white/10 text-gray-100", "text-gray-400 hover:bg-white/5")
            + " items-center justify-center gap-1.5 grow rounded-md py-1.5 cursor-pointer",
        )
    return rx.hstack(
        tab("Чат", "chat", "message-circle"),
        tab("Код", "code", "code"),
        class_name="w-full gap-1 p-1 rounded-lg bg-white/[0.03] mb-1",
    )


def llm_connection_section() -> rx.Component:
    """Підключення до LLM: локальна Ollama або OpenAI-сумісний сервер (vLLM)."""
    def field(label, ctrl):
        return rx.hstack(rx.text(label, class_name="text-sm text-gray-300 w-24 shrink-0"),
                         ctrl, class_name="items-center gap-3 w-full")
    remote = rx.vstack(
        field("Адреса", rx.input(value=State.llm_host, on_change=State.set_llm_host,
                                 placeholder="напр. llm.example.com", class_name="flex-1")),
        field("Порт", rx.input(value=State.llm_port, on_change=State.set_llm_port,
                               placeholder="8000", class_name="flex-1")),
        field("API-ключ", rx.input(
            value=State.llm_key_input, on_change=State.set_llm_key_input, type="password",
            placeholder=rx.cond(State.llm_key_saved, "збережено (введіть, щоб замінити)", "ваш ключ"),
            class_name="flex-1")),
        field("Модель", rx.input(value=State.llm_model, on_change=State.set_llm_model,
                                 placeholder="авто (перша на сервері)", class_name="flex-1")),
        gap="2", class_name="w-full",
    )
    return rx.vstack(
        rx.text("Підключення до LLM", class_name="text-xs text-gray-500 uppercase tracking-wide"),
        rx.segmented_control.root(
            rx.segmented_control.item("Локально (Ollama)", value="ollama"),
            rx.segmented_control.item("Сервер (OpenAI API)", value="openai"),
            value=State.llm_backend, on_change=State.set_llm_backend, size="2", class_name="w-full",
        ),
        rx.cond(State.llm_backend == "openai", remote, rx.fragment()),
        rx.hstack(
            rx.button("Перевірити", variant="soft", on_click=State.test_llm_connection,
                      loading=State.llm_testing),
            rx.button("Зберегти", on_click=State.save_llm_settings),
            rx.button("Діагностика", variant="outline", on_click=State.export_diagnostics,
                      title="Зібрати логи в архів, щоб надіслати розробнику"),
            class_name="gap-2",
        ),
        rx.cond(State.llm_test_msg != "",
                rx.text(State.llm_test_msg,
                        class_name=rx.cond(State.llm_test_ok, "text-xs text-green-400",
                                           "text-xs text-red-400"))),
        gap="2", class_name="w-full mb-4",
    )


def settings_dialog() -> rx.Component:
    def font_slider(label, value, on_change, min_v, max_v):
        return rx.hstack(
            rx.text(label, class_name="text-sm text-gray-300 w-24 shrink-0"),
            rx.slider(
                value=[value],
                on_change=on_change,
                min=min_v,
                max=max_v,
                step=1,
                class_name="flex-1",
            ),
            rx.text(value, "px", class_name="text-sm text-gray-400 w-10 shrink-0 text-right"),
            class_name="items-center gap-3 w-full",
        )

    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.hstack(
                    rx.heading("Налаштування", size="4"),
                    rx.spacer(),
                    rx.dialog.close(
                        rx.icon_button(rx.icon("x", size=16), variant="ghost", size="1"),
                    ),
                    class_name="w-full items-center mb-4",
                ),
                llm_connection_section(),
                # Тема
                rx.text("Тема", class_name="text-xs text-gray-500 uppercase tracking-wide mb-1"),
                rx.segmented_control.root(
                    rx.segmented_control.item("Світла", value="light"),
                    rx.segmented_control.item("Темна", value="dark"),
                    rx.segmented_control.item("Авто", value="system"),
                    value=State.theme,
                    # set_color_mode — фронтенд-тригер, що реально перемикає тему (next-themes).
                    # set_theme лише зберігає вибір у State/на диск; без set_color_mode тема
                    # візуально не мінялась (це й був баг «перемикачі нічого не роблять»).
                    on_change=lambda v: [State.set_theme(v), set_color_mode(v)],
                    size="2", class_name="mb-4 w-full",
                ),
                # Шрифти
                rx.text("Шрифти", class_name="text-xs text-gray-500 uppercase tracking-wide mb-2"),
                font_slider("Чат", State.font_chat,
                            lambda v: State.set_font_chat(v), 12, 32),
                font_slider("Інтерфейс", State.font_ui,
                            lambda v: State.set_font_ui(v), 11, 24),
                gap="4", class_name="w-full px-2",
            ),
            style={"backgroundColor": PANEL, "maxWidth": "560px", "width": "92vw"},
        ),
        open=State.settings_open,
        on_open_change=State.toggle_settings,
    )


def project_section() -> rx.Component:
    """Проекти chat-режиму (аналог Claude/ChatGPT Projects): список + "+ Новий
    проект". Лише mode="chat" — код-режим має власну прив'язку до робочої теки."""
    return rx.cond(
        State.is_chat,
        rx.fragment(
            rx.hstack(
                rx.text("Проекти", class_name="text-xs text-gray-500 uppercase tracking-wide"),
                rx.spacer(),
                rx.icon("plus", size=13, on_click=State.open_new_project,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer"),
                class_name="items-center mt-5 mb-1 px-2",
            ),
            rx.cond(
                State.projects,
                rx.vstack(rx.foreach(State.projects, project_item), class_name="w-full gap-0.5"),
                rx.text("Поки немає проектів", class_name="text-sm text-gray-500 px-2"),
            ),
        ),
        rx.fragment(),
    )


def sidebar() -> rx.Component:
    return rx.flex(
        settings_dialog(),
        mode_switch(),
        nav_item("plus", "Новий чат", State.new_chat),
        project_section(),
        rx.cond(
            State.is_chat & (State.current_project_id != ""),
            rx.fragment(
                rx.hstack(
                    rx.icon("arrow-left", size=12),
                    rx.text("Усі чати", class_name="text-xs"),
                    on_click=lambda: State.select_project(""),
                    class_name="items-center gap-1 text-gray-500 hover:text-gray-200 cursor-pointer "
                               "px-2 mt-3",
                ),
                # "+ Новий чат" САМЕ в межах поточного проекту — між назвою проекту
                # (вище, в project_section) і списком його чатів (нижче); відокремлено
                # від загального "+ Новий чат" нагорі, який теж створює в поточному
                # проекті (State.new_chat вже враховує current_project_id), але тут це
                # видно в контексті, без потреби гортати сайдбар нагору.
                rx.hstack(
                    rx.icon("plus", size=12),
                    rx.text("Новий чат", class_name="text-xs"),
                    on_click=State.new_chat,
                    class_name="items-center gap-1 text-gray-500 hover:text-gray-200 cursor-pointer "
                               "px-2 mt-1.5",
                ),
            ),
            rx.text("Recents", class_name=rx.cond(State.is_chat, "mt-2", "mt-5")
                    + " text-xs text-gray-500 uppercase tracking-wide mb-1 px-2"),
        ),
        rx.vstack(
            rx.cond(
                State.visible_sessions,
                rx.foreach(State.visible_sessions, session_item),
                rx.text("Поки немає чатів", class_name="text-sm text-gray-400 px-2"),
            ),
            class_name=rx.cond(State.is_chat & (State.current_project_id != ""), "pl-3", "")
            + " flex-1 w-full gap-0.5 overflow-y-auto mt-1",
        ),
        rx.spacer(),
        # self-start: не розтягувати на всю ширину колонки (інакше контур-пігулка йде
        # через увесь сайдбар). icon_button — квадрат за розміром іконки, ліворуч.
        rx.icon_button(
            rx.icon("settings", size=18),
            on_click=State.toggle_settings,
            variant="ghost",
            color_scheme="gray",
            size="2",
            class_name="self-start mb-2 ml-1 rounded-full hover:bg-white/10 text-gray-400",
        ),
        direction="column",
        class_name="w-64 max-[900px]:w-48 shrink-0 h-full p-2 gap-0.5",
        style={"backgroundColor": PANEL, "borderRight": "1px solid rgba(255,255,255,0.08)"},
    )


def folder_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("folder", size=14), State.folder_name, type="button",
                      variant="ghost", size="1", class_name="text-gray-300 gap-1"),
        ),
        rx.dialog.content(
            rx.dialog.title("Робоча тека проєкту"),
            rx.input(placeholder="Абсолютний шлях до папки",
                     value=State.folder_input, on_change=State.set_folder_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.dialog.close(rx.button("Зберегти", on_click=State.save_folder)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
    )


def ref_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("plus", size=12), "файл", type="button", variant="ghost",
                      size="1", class_name="text-gray-400"),
        ),
        rx.dialog.content(
            rx.dialog.title("Файл-джерело (read-only)"),
            rx.input(placeholder="Абсолютний шлях до файлу",
                     value=State.ref_input, on_change=State.set_ref_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.dialog.close(rx.button("Додати", on_click=State.add_ref)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
    )


def _code_controls() -> rx.Component:
    """Контроли лише для Код-режиму: тека, дозволи, план наперед."""
    return rx.hstack(
        folder_dialog(),
        rx.hstack(
            rx.text("правки", class_name="text-xs text-gray-500"),
            rx.select(["ask", "auto"], value=State.edits, on_change=State.set_edits,
                      size="1", variant="soft", width="5rem"),
            class_name="items-center gap-1",
        ),
        rx.hstack(
            rx.text("консоль", class_name="text-xs text-gray-500"),
            rx.select.root(
                rx.select.trigger(variant="soft", width="6.2rem"),
                rx.select.content(
                    rx.select.item("smart", value="smart"),
                    rx.select.item("ask", value="ask"),
                    rx.select.item("auto", value="auto"),
                    rx.select.item("allowlist", value="allowlist"),
                    rx.select.item("off", value="off"),
                ),
                value=State.shell, on_change=State.set_shell,
                size="1",
            ),
            class_name="items-center gap-1",
        ),
        class_name="items-center gap-2 flex-wrap",
    )


def attachment_chips() -> rx.Component:
    """Рядок чіпів прикріплених файлів під textarea."""
    def chip(a: dict) -> rx.Component:
        has_error = a["error"] != ""
        return rx.hstack(
            rx.icon("file", size=12,
                    class_name=rx.cond(has_error, "text-red-400", "text-gray-400")),
            rx.text(a["name"], class_name=rx.cond(has_error, "text-red-300", "text-gray-300")
                    + " text-xs truncate max-w-28"),
            rx.cond(has_error,
                    rx.text(a["error"], class_name="text-xs text-red-400 truncate max-w-24"),
                    rx.fragment()),
            rx.icon("x", size=11,
                    class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0",
                    on_click=lambda: State.remove_attachment(a["name"])),
            class_name="items-center gap-1 px-2 py-0.5 rounded-full border "
                       + rx.cond(has_error, "border-red-500/30 bg-red-900/20",
                                 "border-white/10 bg-white/5"),
        )
    return rx.cond(
        State.attachments,
        rx.hstack(
            rx.foreach(State.attachments, chip),
            class_name="flex-wrap gap-1.5 mt-2",
        ),
        rx.fragment(),
    )


def controls_bar() -> rx.Component:
    return rx.hstack(
        rx.upload(
            rx.icon_button(rx.icon("paperclip", size=15), type="button", variant="ghost",
                           size="1", class_name="text-gray-400"),
            id="file_upload",
            multiple=True,
            on_drop=State.handle_upload(rx.upload_files(upload_id="file_upload")),
            accept={
                "text/plain": [".txt", ".md", ".csv", ".log", ".rst"],
                "text/x-python": [".py", ".pyw", ".pyi"],
                "application/json": [".json", ".jsonc"],
                "text/javascript": [".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"],
                "text/html": [".html", ".htm"],
                "text/css": [".css", ".scss", ".sass"],
                "text/yaml": [".yaml", ".yml"],
                "application/toml": [".toml"],
                "text/x-sh": [".sh", ".bash", ".zsh", ".bat", ".ps1"],
                "application/xml": [".xml", ".svg"],
                "application/sql": [".sql"],
            },
            no_drag=True,
            # Прибрати дефолтну рамку/padding rx.upload (вони задаються як ПРЯМІ
            # props через setdefault, не через style) — лишити тільки іконку-кнопку.
            border="none",
            padding="0",
            width="fit-content",
        ),
        rx.cond(State.is_chat, rx.fragment(), _code_controls()),
        rx.spacer(),
        rx.cond(
            State.is_chat,
            rx.hstack(rx.icon("globe", size=14, class_name="text-gray-500"),
                      rx.text("веб-пошук", class_name="text-xs text-gray-500"),
                      class_name="items-center gap-1"),
            rx.hstack(
                rx.text("план наперед", class_name="text-xs text-gray-500"),
                rx.switch(checked=State.plan_first, on_change=State.set_plan_first, size="1"),
                class_name="items-center gap-2",
            ),
        ),
        rx.cond(
            State.busy & (State.queued_text == "") & ~State.stopping,
            # Зайнята + черга порожня + ще не зупиняємось → кнопка Стоп
            rx.button(
                rx.icon("square", size=16),
                on_click=State.stop_generation,
                size="1", radius="full",
                class_name="bg-red-500 text-white ml-1 hover:bg-red-600",
            ),
            # Інакше → submit (send або + в чергу)
            rx.button(
                rx.cond(State.busy, rx.icon("plus", size=16), rx.icon("arrow-up", size=16)),
                type="submit",
                size="1", radius="full", class_name="bg-white text-black ml-1",
            ),
        ),
        class_name="w-full items-center mt-2 gap-2 flex-wrap",
    )


def extra_dir_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("plus", size=12), "тека", type="button", variant="ghost",
                      size="1", class_name="text-gray-400"),
        ),
        rx.dialog.content(
            rx.dialog.title("Додаткова тека проєкту"),
            rx.dialog.description(
                "Агент зможе читати й писати в цій теці (за абсолютним шляхом). Якщо теки ще нема — "
                "її буде створено: наприклад, щоб скопіювати туди проєкт і працювати далі в копії.",
                class_name="text-xs text-gray-400",
            ),
            rx.input(placeholder="Абсолютний шлях до теки",
                     value=State.extra_input, on_change=State.set_extra_input,
                     class_name="w-full mt-2"),
            rx.cond(State.extra_error != "",
                    rx.text(State.extra_error, class_name="text-xs text-red-400 mt-1"), rx.fragment()),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.button("Додати", on_click=State.add_extra_dir),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.extra_open, on_open_change=State.set_extra_open,
    )


def extra_dirs_row() -> rx.Component:
    return rx.hstack(
        rx.icon("folder-plus", size=12, class_name="text-gray-500"),
        rx.text("теки:", class_name="text-xs text-gray-500"),
        rx.foreach(
            State.extra_dirs,
            lambda d: rx.hstack(
                rx.text(d, class_name="text-xs text-gray-300 truncate max-w-48", title=d),
                rx.icon("x", size=11, class_name="text-gray-500 cursor-pointer",
                        on_click=lambda: State.remove_extra_dir(d)),
                class_name="items-center gap-1 px-1.5 py-0.5 rounded bg-white/5",
            ),
        ),
        extra_dir_dialog(),
        class_name="items-center gap-2 mt-1 flex-wrap",
    )


def references_row() -> rx.Component:
    return rx.hstack(
        rx.icon("lock", size=12, class_name="text-gray-500"),
        rx.text("джерела:", class_name="text-xs text-gray-500"),
        rx.foreach(
            State.references,
            lambda r: rx.hstack(
                rx.text(r, class_name="text-xs text-gray-300 truncate max-w-40"),
                rx.icon("x", size=11, class_name="text-gray-500 cursor-pointer",
                        on_click=lambda: State.remove_ref(r)),
                class_name="items-center gap-1 px-1.5 py-0.5 rounded bg-white/5",
            ),
        ),
        ref_dialog(),
        class_name="items-center gap-2 mt-1 flex-wrap",
    )


def token_bar() -> rx.Component:
    """Лічильник токенів поточного запиту + індикатор черги — над полем вводу."""
    left = rx.cond(
        (State.tok_out > 0) | State.busy,
        rx.hstack(
            rx.cond(State.busy, rx.spinner(size="1"), rx.fragment()),
            rx.text(State.tokens_label, class_name="text-xs text-gray-500 font-mono"),
            rx.cond(
                State.queued_text != "",
                rx.hstack(rx.icon("clock", size=12, class_name="text-amber-400"),
                          rx.text("у черзі", class_name="text-xs text-amber-400"),
                          class_name="items-center gap-1"),
                rx.fragment(),
            ),
            class_name="items-center gap-2",
        ),
        rx.fragment(),
    )
    return rx.hstack(left, class_name="items-center w-full px-1 mb-1")


def input_box() -> rx.Component:
    return rx.form(
        rx.vstack(
            token_bar(),
            rx.box(
                rx.text_area(
                    placeholder=rx.cond(
                        State.is_chat,
                        "Напишіть повідомлення…  (Enter — надіслати, Shift+Enter — новий рядок)",
                        "Опишіть задачу...  (Enter — надіслати, Shift+Enter — новий рядок)"),
                    value=State.task,
                    on_change=State.set_task,
                    enter_key_submit=True,
                    class_name="w-full bg-transparent text-gray-100 placeholder:text-gray-500 "
                               "resize-none outline-none border-none",
                    style={"fontSize": State.chat_font_px},
                    rows="2",
                ),
                controls_bar(),
                rx.cond(State.is_chat, rx.fragment(), rx.fragment(extra_dirs_row(), references_row())),
                attachment_chips(),
                class_name="w-full rounded-2xl p-3 border " + BORDER,
                style={"backgroundColor": INPUT},
            ),
            class_name="w-full gap-0",      # ширину задає батьківський контейнер
        ),
        on_submit=State.send_task,
        reset_on_submit=False,
        class_name="w-full",
    )


def message_bubble(m: dict) -> rx.Component:
    mine = m["role"] == "user"
    return rx.box(
        rx.cond(
            mine,
            rx.vstack(
                rx.text(m["content"], class_name="whitespace-pre-wrap text-gray-100",
                        style={"fontSize": State.chat_font_px}),
                rx.cond(
                    m["attachments"].to(list[str]).length() > 0,
                    rx.hstack(
                        rx.foreach(
                            m["attachments"].to(list[str]),
                            lambda n: rx.hstack(
                                rx.icon("file", size=11, class_name="text-gray-400"),
                                rx.text(n, class_name="text-xs text-gray-300 truncate max-w-40"),
                                class_name="items-center gap-1 px-2 py-0.5 rounded-full bg-black/20"),
                        ),
                        class_name="flex-wrap gap-1 mt-1 justify-end",
                    ),
                    rx.fragment(),
                ),
                class_name="items-end gap-0",
            ),
            rx.box(
                rx.cond(
                    m["thinking"] != "",
                    rx.el.details(
                        rx.el.summary(
                            rx.cond(m["kind"] == "step", "хід виконання", "Роздуми моделі"),
                            class_name="text-xs text-gray-500 cursor-pointer select-none"),
                        rx.markdown(m["thinking"], class_name="max-w-full overflow-x-auto break-words"),
                        class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 max-w-full "
                                   "overflow-y-auto overflow-x-hidden",
                    ),
                    rx.fragment(),
                ),
                rx.markdown(m["content"], class_name="max-w-full overflow-x-auto break-words"),
                style={"fontSize": State.chat_font_px},
                class_name="min-w-0 max-w-full",
            ),
        ),
        # user — компактна бульбашка справа; assistant — на всю ширину (вирівняно з полем вводу).
        class_name=rx.cond(
            mine,
            "self-end bg-white/10 max-w-[85%]",
            "self-start bg-white/[0.03] w-full min-w-0",
        ) + " rounded-2xl px-4 py-2.5",
    )


def pending_bar() -> rx.Component:
    return rx.hstack(
        rx.button("Виконати план", on_click=State.execute_pending,
                  class_name="bg-white text-black rounded-lg px-3 py-1.5 text-sm"),
        rx.button("Відхилити", on_click=State.discard_pending, variant="soft", size="2"),
        class_name="self-start gap-2 mt-1",
    )


def q_option(opt: dict) -> rx.Component:
    """Клікабельний кандидат/підхід — відповідь на питання планувальника."""
    return rx.box(
        rx.text(opt["label"], class_name="text-sm text-gray-100 font-medium"),
        rx.cond(
            opt["detail"] != "",
            rx.text(opt["detail"], class_name="text-xs text-gray-400"),
            rx.fragment(),
        ),
        on_click=lambda: State.answer_planner(opt["label"]),
        class_name="cursor-pointer rounded-lg px-3 py-2 bg-white/5 hover:bg-white/10 "
                   "border " + BORDER,
    )


def question_bar() -> rx.Component:
    """Питання планувальника: клікабельні кандидати + підказка, що можна й вписати."""
    return rx.vstack(
        rx.hstack(rx.foreach(State.q_options, q_option),
                  class_name="flex-wrap gap-2 items-stretch"),
        rx.hstack(
            rx.text("…або впишіть відповідь у поле нижче",
                    class_name="text-xs text-gray-500"),
            rx.button("Скасувати", on_click=State.cancel_question,
                      variant="ghost", size="1", class_name="text-gray-400"),
            class_name="items-center gap-2",
        ),
        class_name="self-start gap-2 mt-1",
    )


def status_line() -> rx.Component:
    return rx.cond(
        State.busy & ~State.streaming,
        rx.hstack(
            rx.spinner(size="1"),
            rx.text(State.status, class_name="text-sm text-gray-400 italic"),
            class_name="self-start items-center gap-2 px-1",
        ),
        rx.fragment(),
    )


def streaming_bubble() -> rx.Component:
    """Тимчасова бульбашка під час стріму: роздуми під катом + контент.
    Лічильник токенів — над полем вводу (token_bar)."""
    return rx.box(
        rx.cond(
            State.stream_thinking != "",
            rx.el.details(
                rx.el.summary("Роздуми моделі",
                              class_name="text-xs text-gray-500 cursor-pointer select-none"),
                rx.markdown(State.stream_thinking, class_name="max-w-full overflow-x-auto break-words"),
                class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 max-w-full "
                           "overflow-y-auto overflow-x-hidden",
            ),
            rx.fragment(),
        ),
        rx.markdown(State.stream_content, class_name="max-w-full overflow-x-auto break-words"),
        class_name="self-start bg-white/[0.03] rounded-2xl px-4 py-2.5 w-full min-w-0",
        style={"fontSize": State.chat_font_px},
    )


def activity_panel() -> rx.Component:
    """Жива стрічка «Що робить Ігорь» під катом: кожен запит до моделі та виклик тула
    (який скрипт/команда і який результат) + годинник «остання подія N с тому» —
    видно, чи є рух, чи агент завис."""
    return rx.cond(
        State.activity.length() > 0,
        rx.el.details(
            rx.el.summary(
                rx.hstack(
                    rx.icon("activity", size=14),
                    rx.text("Що робить Ігорь · подій: ", State.activity.length(), " · остання ",
                            rx.el.span(data_ts=State.activity_last, class_name="igor-ago"),
                            class_name="text-xs"),
                    rx.cond(State.busy, rx.spinner(size="1")),
                    class_name="items-center gap-2 text-gray-500 cursor-pointer",
                ),
            ),
            rx.vstack(rx.foreach(State.activity, activity_row_full), gap="2",
                      class_name="mt-2 max-h-96 overflow-y-auto w-full"),
            class_name="w-full rounded-lg border border-white/10 px-3 py-2",
        ),
        rx.fragment(),
    )


def activity_row_full(r: dict) -> rx.Component:
    """Рядок стрічки: статус, час, тул і його команда; нижче — результат (для тулів)."""
    return rx.vstack(
        rx.hstack(
            rx.cond(r["status"] == "run", rx.spinner(size="1"),
                    rx.cond(r["status"] == "ok", rx.text("✓", class_name="text-xs text-green-400"),
                            rx.text("✗", class_name="text-xs text-red-400"))),
            rx.text(r["t"], class_name="text-xs text-gray-600 font-mono shrink-0"),
            rx.text(r["title"], class_name=rx.cond(r["kind"] == "tool", "text-xs text-sky-300 font-mono shrink-0",
                                                   "text-xs text-gray-400 shrink-0")),
            class_name="items-center gap-2 w-full",
        ),
        rx.text(r["detail"], class_name="text-xs text-gray-400 font-mono pl-5 whitespace-pre-wrap break-all"),
        gap="0", class_name="w-full",
    )


def chat_view() -> rx.Component:
    return rx.vstack(
        rx.foreach(State.messages, message_bubble),
        rx.cond(State.streaming, streaming_bubble(), rx.fragment()),
        status_line(),
        activity_panel(),
        rx.cond(State.has_pending, pending_bar(), rx.fragment()),
        rx.cond(State.has_question, question_bar(), rx.fragment()),
        id="chat-scroll",
        on_mount=rx.call_script(SCROLL_SETUP_JS),
        class_name="w-full flex-1 overflow-y-auto overflow-x-hidden min-w-0 px-4 min-[900px]:px-[1cm] py-6 gap-3",
        # scrollbar-gutter: stable — скролбар завжди резервує місце праворуч, тож ширина
        # контенту стабільна; поле вводу компенсує ту саму ширину через --sbw (нижче).
        style={"fontSize": State.chat_font_px, "scrollbarGutter": "stable"},
    )


def scroll_down_button() -> rx.Component:
    """Кнопка-стрілка: промотати чат донизу. По центру, над полем вводу."""
    return rx.button(
        rx.icon("arrow-down", size=18),
        on_click=rx.call_script(SCROLL_BOTTOM_JS),
        radius="full", size="2",
        id="scroll-down-btn",
        # Початково сховано; SCROLL_SETUP_JS показує лише коли чат прокручено вгору.
        style={"display": "none"},
        class_name="absolute left-1/2 -translate-x-1/2 bottom-full mb-3 bg-white/10 "
                   "hover:bg-white/20 text-gray-100 shadow-lg backdrop-blur z-10",
    )


def ctx_row(r: dict) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.box(class_name="w-3 h-3 rounded-sm shrink-0", style={"backgroundColor": r["color"]}),
            rx.text(r["label"], class_name="text-sm text-gray-200 grow"),
            rx.text(r["tokens_label"], class_name="text-sm text-gray-300 font-mono"),
            rx.text(r["pct_label"], class_name="text-xs text-gray-500 w-14 text-right font-mono"),
            class_name="items-center gap-2 w-full",
        ),
        rx.cond(r["note"] != "", rx.text(r["note"], class_name="text-xs text-gray-500 pl-5")),
        rx.cond(
            r["compress"] != "",
            rx.hstack(rx.icon("shrink", size=12, class_name="shrink-0 mt-0.5 text-amber-400"),
                      rx.text(r["compress"], class_name="text-xs text-amber-400/90"),
                      class_name="items-start gap-1 pl-5"),
        ),
        gap="1", class_name="w-full",
    )


def _section(title: str, *children, **kw) -> rx.Component:
    return rx.vstack(
        rx.text(title, class_name="text-xs text-gray-500 uppercase tracking-wide"),
        *children, gap="2", class_name="w-full py-3 border-b border-white/10", **kw)


def _kv(r: dict) -> rx.Component:
    return rx.hstack(
        rx.text(r["label"], class_name="text-sm text-gray-500 w-28 shrink-0"),
        rx.text(r["value"], class_name="text-sm text-gray-200 break-all"),
        class_name="items-start gap-2 w-full")


def _folder(r: dict) -> rx.Component:
    return rx.hstack(
        rx.icon("folder", size=14, class_name="text-gray-500 shrink-0 mt-0.5"),
        rx.vstack(
            rx.text(r["name"], class_name="text-sm text-gray-200 break-all"),
            rx.text(r["path"], class_name="text-xs text-gray-600 break-all"),
            gap="0", class_name="grow min-w-0"),
        rx.text(r["tag"], class_name="text-xs text-gray-500 shrink-0"),
        class_name="items-start gap-2 w-full")


def _change(r: dict) -> rx.Component:
    return rx.hstack(
        rx.text(r["path"], class_name="text-xs text-gray-300 font-mono break-all grow min-w-0"),
        rx.text(r["delta"], class_name="text-xs text-gray-500 font-mono shrink-0"),
        class_name="items-start gap-2 w-full")


def context_panel() -> rx.Component:
    """Постійна права панель (як у Claude Code): середовище, контекст (що займає вікно
    моделі + дані про стиснення), теки/файли чату, зміни в git. Ховається на вузьких вікнах."""
    context = rx.vstack(
        rx.hstack(
            rx.text(State.ctx_pct, "% використано", class_name="text-sm text-gray-200"),
            rx.spacer(),
            rx.text(State.ctx_total, class_name="text-sm text-gray-400 font-mono"),
            class_name="w-full items-center",
        ),
        rx.hstack(
            rx.foreach(State.ctx_segments,
                       lambda s: rx.box(style={"width": s["width"], "backgroundColor": s["color"]},
                                        class_name="h-full")),
            class_name="w-full h-2 rounded-full overflow-hidden gap-px",
            style={"backgroundColor": "#333"},
        ),
        rx.vstack(rx.foreach(State.ctx_rows, ctx_row), gap="3", class_name="w-full mt-1"),
        rx.hstack(
            rx.box(class_name="w-3 h-3 rounded-sm shrink-0", style={"backgroundColor": "#4b4b4b"}),
            rx.text("Вільно", class_name="text-sm text-gray-400 grow"),
            rx.text(State.ctx_free, class_name="text-sm text-gray-400 font-mono"),
            class_name="items-center gap-2 w-full",
        ),
        rx.cond(State.tok_out > 0,
                rx.text("Останній запит: ", State.tokens_label, class_name="text-xs text-gray-500 font-mono")),
        rx.text("Токени — оцінка за символами. Підсумок розмови, дайджести файлів і нотатки тем "
                "стискаються автоматично при перевищенні порогу; про кожне стиснення Ігорь пише "
                "в чаті й у лозі.", class_name="text-xs text-gray-600"),
        gap="3", class_name="w-full")
    changes = rx.cond(
        State.git_is_repo & (State.mode == "code"),
        _section(
            "Зміни",
            rx.hstack(
                rx.text(State.git_summary, class_name="text-sm font-mono text-green-400"),
                rx.text("у ", State.git_branch, class_name="text-xs text-gray-500"),
                class_name="items-center gap-2"),
            rx.foreach(State.git_files, _change),
            rx.cond(State.git_more > 0,
                    rx.text("… ще ", State.git_more, class_name="text-xs text-gray-600")),
        ),
        rx.fragment(),
    )
    return rx.box(
        rx.vstack(
            rx.heading("Ігорь", size="3", class_name="mb-1"),
            _section("Середовище", rx.foreach(State.env_rows, _kv)),
            _section("Контекст", context),
            rx.cond(State.folder_rows.length() > 0,
                    _section("Теки та файли", rx.foreach(State.folder_rows, _folder)),
                    rx.fragment()),
            changes,
            gap="0", class_name="w-full",
        ),
        class_name="hidden lg:block w-[320px] shrink-0 h-full overflow-y-auto p-4 border-l border-white/10",
        style={"backgroundColor": PANEL},
    )


def main_area() -> rx.Component:
    return rx.cond(
        State.messages,
        rx.vstack(
            chat_view(),
            rx.box(
                scroll_down_button(),
                input_box(),
                # Праворуч додаємо ширину скролбара (--sbw), щоб рамка поля вводу
                # збігалася з правим краєм відповіді (чат має скролбар, інпут — ні).
                class_name="w-full min-w-0 pl-4 pr-[calc(1rem+var(--sbw,0px))] "
                           "min-[900px]:pl-[1cm] min-[900px]:pr-[calc(1cm+var(--sbw,0px))] pb-4 relative",
            ),
            class_name="flex-1 h-full w-full min-w-0 relative",
        ),
        rx.center(
            rx.vstack(
                rx.heading(
                    rx.cond(State.has_current, State.title, "Що робимо?"),
                    class_name="text-4xl text-gray-200 mb-2", style=SERIF,
                ),
                input_box(),
                spacing="5",
                class_name="w-full max-w-2xl items-center px-4",
            ),
            class_name="flex-1 h-full min-w-0",
        ),
    )


def index() -> rx.Component:
    return rx.hstack(
        sidebar(),
        main_area(),
        context_panel(),
        rename_dialog(),
        project_dialog(),
        move_dialog(),
        confirm_dialog(),
        # fixed inset-0, а не h-screen/w-screen: 100vw включає смугу прокрутки й дає зайвий скрол документа;
        # на широких моніторах після розгортання вікна це давало зсув інтерфейсу й білі поля.
        class_name="fixed inset-0 overflow-hidden",
        style={"backgroundColor": BG},
        spacing="0",
    )


app = rx.App(
    stylesheets=["https://fonts.googleapis.com/css2?family=Newsreader:ital@0;1&display=swap"],
    head_components=[rx.el.style("html,body{margin:0;height:100%;overflow:hidden}")],
)
app.add_page(index, title=f"Ігорь! · {logs.app_version()}",
             on_load=[State.load_sessions, State.load_settings])
