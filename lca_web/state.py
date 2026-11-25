"""local-code-agent — стан застосунку (Reflex State) і його допоміжні хуки/константи.

G3: керування сесіями через бекенд (agent/session.py): створення/список/вибір,
тека, дозволи, план-наперед, джерела — зберігаються в сесію.
Прив'язки runner (answer/shell/plan/edit) — G4.
"""
import asyncio
import functools
import json
import os
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import reflex as rx
from reflex_base.style import set_color_mode

from agent import attachments as A
from agent import images
from agent import sessionmem
from agent import backend as B
from agent import config
from agent import convo
from agent import activity as act
from agent import context_view as cv
from agent import orchestration as orch
from agent import projectmap
from agent import limits
from agent import logs
from agent import chat_projects as CP
from agent import session as sess
from agent import topics
from agent.agent_loop import is_incomplete, step_iter_limit, _looks_like_leaked_reasoning
from agent.agent_loop import summarize_history
from agent.steploop import LoopHooks
from agent.answerer import build_context
from agent.plan_runner import PlanEnv, PlanRunner, StepRun, tail_for_plan
from agent.intent import quick_route
from agent.memory import render_for_user
from agent.planner import deliberate
from agent.project import load_project_doc, scan_structure
from agent.project_info import git_info
from agent.toolkit import ToolContext, default_registry

CHAT_SYSTEM = (
    "You are a helpful thinking assistant. You can search the web with the web_search "
    "tool when you need current or external information. If the user attached files, "
    "their names are listed in context — read any you need with the read_file(path=<name>) "
    "tool, find a place in a big one with search(pattern) (NEVER ask the user to paste or resend them; "
    "they are already available). "
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
_PLAN_ACTIVE: set = set()          # id чатів, де зараз виконується план (повідомлення йдуть планувальнику як коментар)
_LIVE_NOTES: dict = {}             # id чату -> [{"text", "seen"}]: коментарі користувача по ходу виконання


def _take_live_notes(cid) -> list[str]:
    """Забрати (і прибрати) накопичені коментарі користувача для планувальника."""
    items = _LIVE_NOTES.pop(cid, [])
    return [i["text"] for i in items]

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



























# Вставка файлів через Ctrl+V: перенаправляємо файли з буфера обміну у прихований
# input компонента rx.upload → спрацьовує наявний handle_upload. Якщо у буфері лише
# текст (files порожній) — нічого не робимо, звичайна вставка в textarea працює як є.
PASTE_SETUP_JS = """
(() => {
  if (window._lcaPasteBound) return;
  window._lcaPasteBound = true;
  // Єдина точка: передати файли (з буфера чи з перетягування) у прихований input rx.upload → handle_upload.
  const forward = (files) => {
    const input = document.querySelector('.rx-Upload input[type="file"]');
    if (!input || !files.length) return false;
    const dt = new DataTransfer();
    files.forEach(f => dt.items.add(f));
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  };
  document.addEventListener('paste', (e) => {
    const files = Array.from((e.clipboardData && e.clipboardData.files) || []);
    if (!files.length) return;                    // у буфері лише текст — звичайна вставка в textarea
    if (forward(files)) e.preventDefault();       // скриншот/файл із буфера (Ctrl+V)
  });
  // Перетягування файлів і зображень на ВСЕ вікно: підказка-накладка, а на drop — ті самі файли в handle_upload.
  let depth = 0, ov = null;
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  const show = () => {
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'igor-drop';
      ov.textContent = 'Відпустіть, щоб прикріпити файли або зображення';
      Object.assign(ov.style, { position: 'fixed', inset: '0', zIndex: '9999', display: 'flex', alignItems: 'center',
        justifyContent: 'center', background: 'rgba(20,20,20,0.72)', border: '2px dashed rgba(255,255,255,0.4)',
        color: '#fff', fontSize: '20px', pointerEvents: 'none' });
      document.body.appendChild(ov);
    }
    ov.style.display = 'flex';
  };
  const hide = () => { depth = 0; if (ov) ov.style.display = 'none'; };
  document.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; show(); });
  document.addEventListener('dragover', (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
  document.addEventListener('dragleave', (e) => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) hide(); });
  document.addEventListener('drop', (e) => { if (!hasFiles(e)) return; e.preventDefault(); hide(); forward(Array.from(e.dataTransfer.files || [])); });
  window.addEventListener('blur', hide);
})();
"""









GPU_JS = r"""
(function(){
  if (window.__igorGpu) return;
  var last = null;
  function el(id){ return document.getElementById(id); }
  function txt(id, s){ var e = el(id); if (e) e.textContent = s || ''; }
  function draw(){
    var c = el('gpu-canvas'); if (!c || !last) return;
    var dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
    if (!w || !h) return;
    var pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (c.width !== pw || c.height !== ph) { c.width = pw; c.height = ph; }
    var g = c.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    g.strokeStyle = 'rgba(255,255,255,0.10)'; g.lineWidth = 1;
    var y50 = Math.round(h / 2) + 0.5; g.beginPath(); g.moveTo(0, y50); g.lineTo(w, y50); g.stroke();
    var s = last.series || [], want = Math.max(2, Math.round((last.span || 300) / 5)), cnt = Math.max(s.length, want);
    var step = w / (cnt - 1), start = w - (s.length - 1) * step;
    function X(i){ return start + i * step; }
    function Y(v){ return h - 1 - (v / 100) * (h - 2); }
    var seg = [];
    function flush(){
      if (seg.length) {
        g.beginPath(); g.moveTo(X(seg[0][0]), h); for (var k = 0; k < seg.length; k++) g.lineTo(X(seg[k][0]), Y(seg[k][1]));
        g.lineTo(X(seg[seg.length - 1][0]), h); g.closePath(); g.fillStyle = 'rgba(56,189,248,0.22)'; g.fill();
        g.beginPath(); for (var m = 0; m < seg.length; m++) { if (m) g.lineTo(X(seg[m][0]), Y(seg[m][1])); else g.moveTo(X(seg[m][0]), Y(seg[m][1])); }
        g.strokeStyle = '#38bdf8'; g.lineWidth = 1.5; g.stroke();
      }
      seg = [];
    }
    for (var i = 0; i < s.length; i++) { if (s[i] === null || s[i] === undefined) flush(); else seg.push([i, s[i]]); }
    flush();
  }
  function render(){
    if (!last) return;
    var v = last.v || {};
    txt('gpu-status', v.status); txt('gpu-util', v.util); txt('gpu-line', v.temp_power); txt('gpu-vram', v.vram);
    txt('gpu-model', v.model);
    txt('gpu-hint', v.state === 'ok' ? '' : 'Дані навантаження GPU недоступні' + (v.hint ? ' — ' + v.hint : ''));
    var card = el('gpu-card'); if (card) card.style.display = v.state === 'na' ? 'none' : 'flex';   // 'flex': початковий none сидить у CSS-класі, '' його не знімає
    var colors = {ok: '#22c55e', wait: '#f59e0b', na: '#6b7280'};
    var dot = el('gpu-dot'); if (dot) dot.style.backgroundColor = colors[v.state] || '#ef4444';
    var u = el('gpu-util'); if (u) u.style.color = v.state === 'ok' ? '#7dd3fc' : '';
    var box = el('gpu-graph'); if (box) box.style.display = v.state === 'ok' ? '' : 'none';
    draw();
  }
  window.__igorGpu = function(d){ last = d; render(); };
  window.__igorGpuRender = render;
  window.addEventListener('resize', draw);
})();
"""
STREAM_THINK_TAIL = 3000                    # скільки символів живих роздумів показуємо під час стріму
STREAM_FLUSH_S = 0.2                       # як часто (с) оновлювати стрім відповіді/роздумів у UI
SHOWN_STEP = 40                            # порція повідомлень історії, що рендериться (і підвантажується кнопкою)
INPUT_ID = "task-input"                   # поле вводу повідомлення (некероване)
CLEAR_INPUT_JS = (f"(function(){{var e=document.getElementById('{INPUT_ID}');"
                  "if(e){e.value='';e.style.height='';e.style.overflowY='';}})()")

# Поле вводу росте разом із текстом: від 2 рядків до INPUT_MAX_VH % висоти вікна (понад 3× від початкової), далі — скрол
# усередині. Один document-listener на сторінку (переживає перестворення поля при перемиканні макета); відправка
# скидає висоту (CLEAR_INPUT_JS). Поле некероване, тож рахуємо висоту з scrollHeight на кожну подію input.
INPUT_MAX_VH = 40
INPUT_GROW_JS = """
(function(){
  if (window.__igorGrow) return;
  window.__igorGrow = true;
  function grow(e){
    if (!e || e.id !== '__ID__') return;
    var max = Math.max(120, Math.round(window.innerHeight * __VH__ / 100));
    e.style.height = 'auto';
    var h = e.scrollHeight;
    e.style.height = Math.min(h, max) + 'px';
    e.style.overflowY = h > max ? 'auto' : 'hidden';
  }
  document.addEventListener('input', function(ev){ grow(ev.target); }, true);
  window.addEventListener('resize', function(){ grow(document.getElementById('__ID__')); });
})()
""".replace("__ID__", INPUT_ID).replace("__VH__", str(INPUT_MAX_VH))


def restore_input_js(text: str) -> str:
    """JS: дочекатись (до ~2 с), поки поле вводу з'явиться після перемикання макета, і вернути в нього текст."""
    return (f"(function f(t,n){{var e=document.getElementById('{INPUT_ID}');"
            "if(e&&!e.value){e.value=t;e.dispatchEvent(new Event('input',{bubbles:true}));e.focus();return;}"
            "if(n<20)setTimeout(function(){f(t,n+1)},100);}})("
            f"{json.dumps(text)},0)")


UPDATE_POLL_S = float(os.environ.get("IGOR_UPDATE_POLL_S") or 120)   # період перевірки оновлень, с (поки активна розробка — 2 хв; потім 900–3600)
GPU_POLL_S = 10                             # як часто опитувати агент моніторингу GPU, с
_GPU_GEN = 0                                # поколінна відмітка циклу опитування GPU (новий запуск витісняє старий)
_POLL_GEN = 0                              # поколінна відмітка циклу перевірки: новий on_load витісняє старий
_UPDATE_CHECKED = False                 # тиха перевірка оновлень на старті — раз на запуск


class _UIHooks(LoopHooks):
    """Хуки ядра циклу (agent.steploop) для UI: диспетчер тулів із async-підтвердженням shell, картки активності,
    коментарі користувача по ходу, проміжні перевірки планувальника, запис про стиснення в чат і панель."""

    def __init__(self, state, cid: str, stop_event=None, checkpoint=None):
        super().__init__(stop_event=stop_event)
        self.st, self.cid, self._checkpoint = state, cid, checkpoint

    async def dispatch(self, registry, name, args, ctx):
        return await self.st._dispatch_tool(registry, name, args, ctx)

    def live_messages(self) -> list[str]:
        """Коментар користувача по ходу: показуємо виконавцю одразу, а планувальник отримає його (не прибираємо) після кроку."""
        out = []
        for n in _LIVE_NOTES.get(self.cid, []):
            if not n["seen"]:
                n["seen"] = True
                out.append(n["text"])
        return out

    async def before_llm(self, messages):
        async with self.st:
            aid = self.st._act_start("llm", "Запит до моделі", f"повідомлень у контексті: {len(messages)}")
        return aid, time.monotonic()

    async def after_llm(self, token, stats, n_calls):
        aid, t0 = token
        async with self.st:
            self.st.ctx_live = int(stats.get("prompt", 0) or 0)
            self.st._act_done(aid, f"{time.monotonic() - t0:.1f}с · ↑{stats.get('prompt', 0)} ↓{stats.get('out', 0)} токенів"
                                   + (f" · викликів тулів: {n_calls}" if n_calls else " · текстова відповідь"))

    async def guard_warning(self, reason):
        async with self.st:
            self.st._append("assistant", f"⚠ Помітив повтор без прогресу ({reason}) — прошу змінити підхід.", "note")

    async def summarize(self, messages, client, window):
        async with self.st:
            aid = self.st._act_start("llm", "Стиснення контексту", "підсумок історії кроку від моделі")
        t0 = time.monotonic()
        done, b4, af = await asyncio.to_thread(summarize_history, messages, client, window)
        async with self.st:
            self.st._act_done(aid, f"{time.monotonic() - t0:.1f}с · " + (f"{b4} → {af} символів" if done else "пропущено"), ok=done)
            if done:
                self.st._apply_compress_events(self.cid, [("історія кроку", b4, af, "")])
        return done, b4, af

    async def checkpoint(self, actions):
        return await self._checkpoint(actions) if self._checkpoint else None


class _ReadHooks(_UIHooks):
    """Хуки ядра циклу для режимів чату й аналізу (лише читання): стрім відповіді з роздумами (сильна модель), дайджест
    ВЕЛИКИХ вкладень, кожен виклик тулу — рядок у чаті (як і раніше), стоп — за поточною стоп-подією чату."""

    def __init__(self, state, cid: str, client, stream: bool, digest_profile: dict):
        super().__init__(state, cid)
        self.client, self.stream, self.digest_profile = client, stream, digest_profile
        self.last_thinking = ""
        self.digest_memory: dict[str, str] = {}

    def should_stop(self) -> bool:
        ev = _STOP_EVENTS.get(self.cid)        # подію могли створити вже після старту (кнопка «Стоп») — беремо поточну
        return ev is not None and ev.is_set()

    async def call_llm(self, client, messages, tools, profile):
        if not self.stream:
            return await super().call_llm(client, messages, tools, profile)
        body, thinking, calls = await self.st._run_stream(client.chat_stream(messages, tools=tools, profile=profile),
                                                          keep_stop=True)
        self.last_thinking = thinking
        out = {"role": "assistant", "content": body}
        if calls:
            out["tool_calls"] = calls
        return out

    async def dispatch(self, registry, name, args, ctx):
        label = str(args.get("path") or args.get("name") or args.get("pattern") or args.get("query") or name)
        async with self.st:
            self.st.status = f"{name}: {label}"[:60]
        result = await self.st._dispatch_tool(registry, name, args, ctx)
        shown = result
        att = self.st._needs_digest(ctx, name, result)
        if att:
            async with self.st:
                self.st.status = f"Узагальнюю прочитане ({att})…"
            shown = await self.st._digest_attachment_chunk(att, result, self.digest_memory, self.client,
                                                          self.digest_profile)
        async with self.st:
            self.st._append("assistant", f"🔧 {name} · {label}", "step", thinking=shown)
        return shown


class _PlanUI:
    """Адаптер ядра виконання плану (agent.plan_runner.PlanRunner) до Reflex-стану: статус, повідомлення в чат,
    збереження очікуваного плану, коментарі користувача по ходу, ОДИН крок — через _run_tool_step (картки активності,
    підтвердження shell, проміжні перевірки)."""

    def __init__(self, st, cid: str, ctx, stop_event):
        self.st, self.cid, self.ctx, self.stop_event = st, cid, ctx, stop_event

    async def status(self, text: str) -> None:
        async with self.st:
            self.st.status = text

    async def say(self, content: str, kind: str = "note") -> None:
        async with self.st:
            self.st._append("assistant", content, kind)

    async def save_pending(self, plan) -> None:
        async with self.st:
            self.st._save_pending(plan)

    def take_live_notes(self) -> list[str]:
        return _take_live_notes(self.cid)

    async def run_step(self, step_text: str, *, title: str, context: str, checkpoint=None) -> StepRun:
        final, log = await self.st._run_tool_step(step_text, self.ctx, title=title, context=context,
                                                  stop_event=self.stop_event, checkpoint=checkpoint)
        async with self.st:
            return StepRun(final, log, list(self.st._step_touched), list(self.st._step_actions))


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
    shown_count: int = 40                  # скільки останніх повідомлень рендеримо (довга історія гальмує відкриття)
    has_pending: bool = False
    queued_text: str = ""                  # повідомлення, що чекає, поки модель зайнята
    stopping: bool = False                 # стоп запитано, чекаємо переривання
    context_summary: str = ""              # контекст-памʼять розмови (підсумок)
    summary_upto: int = 0                  # підсумок покриває messages[:summary_upto]; далі — дослівний хвіст
    compress_log: list[dict] = []          # історія стискань контексту цієї сесії
    activity: list[dict[str, str]] = []    # жива стрічка: запити до моделі й виклики тулів (під катом)
    activity_last: str = ""                # ISO-час останньої події (годинник «N с тому» на клієнті)
    _act_seq: int = 0                      # лічильник id подій (backend-only)
    _step_touched: list = []               # файли, які змінив останній виконаний крок (backend-only)
    _step_actions: list = []               # (тул, args, результат) останнього кроку — для звіту планувальнику
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
    llm_context: str = ""
    notes_rev: int = 0             # збільшується, коли змінились нотатки сесії → перерахунок панелі контексту
    llm_rev: int = 0               # збільшується при збереженні підключення → перерахунок панелі контексту
    llm_key_input: str = ""
    llm_key_saved: bool = False
    server_names: list[str] = []           # назви збережених серверів (самі профілі й ключі — лише у файлі налаштувань)
    server_pick: str = ""
    llm_models: list[str] = []             # моделі, які повернув сервер/Ollama при «Перевірити» (випадаючий список)
    llm_test_msg: str = ""
    llm_test_ok: bool = False
    llm_testing: bool = False
    # самооновлення (agent/updater.py)
    app_ver: str = ""
    update_available: bool = False         # на GitHub є новіша версія
    update_latest: str = ""
    update_notes: str = ""
    update_msg: str = ""                   # результат перевірки / хід завантаження
    update_ok: bool = True                 # колір повідомлення (false — помилка)
    update_busy: bool = False              # іде перевірка або завантаження
    update_dismissed: bool = False         # банер закрито до наступного запуску
    # Навантаження GPU сервера (agent/gpumon.py): вмикається в Налаштуваннях, показується зверху правої панелі
    gpu_enabled: bool = True               # за замовчуванням увімкнено (agent/settings.py); явне «вимкнено» зберігається
    gpu_port: str = "8001"                # порт агента моніторингу на сервері
    # Самі показники GPU у стані НЕ зберігаються: бекенд пушить їх скриптом (window.__igorGpu) прямо в DOM картки,
    # тож оновлення раз на 10 с не змінює жодної змінної стану й не перемальовує React-інтерфейс (ввід не підвисає).
    _update_info: dict = {}                # відповідь updater.check (backend-only: з URL-ами ресурсів)
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
    ctx_live: int = 0              # реальний prompt останнього запиту виконавця (токени) — для панелі контексту
    tok_out: int = 0
    tok_eval_ns: int = 0
    tok_tps: float = 0.0
    tok_model: str = ""

    @rx.event
    def set_task(self, v: str):
        self.task = v

    @rx.event
    def submit_task(self, form_data: dict):
        """Відправка з поля вводу. Поле НЕКЕРОВАНЕ (текст живе в браузері, не в стані): керований input
        слав подію на кожен символ, а Reflex після кожної зміни стану перемальовував усю сторінку разом
        зі списком повідомлень — у довгому чаті це давало лаги вводу по пів секунди. Тут текст вперше
        потрапляє в стан — лише при відправці."""
        text = str((form_data or {}).get("task") or "")
        if not text.strip() and not any(a.get("image") and not a.get("error") for a in self.attachments):
            return
        if self.mode != "chat" and not self.project_root and not self.folder_input.strip():
            self._append("assistant", "Спершу вкажіть робочу теку.", "note")
            return rx.call_script(restore_input_js(text))   # підказка перемикає макет → поле створюється заново; повертаємо текст
        self.task = text
        return [rx.call_script(CLEAR_INPUT_JS), State.send_task]

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
        self.ctx_live = 0
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
            rows.append({"label": "Модель", "value": eff["model"]})
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
        _ = self.llm_rev                                      # перерахунок після збереження налаштувань (вікно/бекенд)
        lim = limits.get()
        chat = self.mode == "chat"
        msgs = [dict(m) for m in self.messages][self.summary_upto:]    # старіші — у підсумку, у вікні лише хвіст
        _ = self.attachments                                  # перерахунок при зміні чипів
        cid = self.current_id
        names = A.list_text(cid) if cid else []
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
        _ = self.notes_rev                                    # нотатки сесії змінились (remember / звіт субагента)
        notes_note = sessionmem.index_block(cid) if (not chat and cid) else ""
        pmap_text = projectmap.context_block(self.project_root) if (not chat and self.project_root) else ""
        from agent.agent_loop import SYSTEM as EXEC_SYSTEM
        rep = cv.build(
            window=lim.context_tokens, system=CHAT_SYSTEM if chat else EXEC_SYSTEM,
            tools_tok=_tools_tokens(chat), project=project, project_map=pmap_text, topics_note=topics_note,
            notes_note=notes_note, summary=self.context_summary,
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
        _ = self.llm_rev
        rep = json.loads(self.ctx_report)
        live = round(100 * self.ctx_live / limits.get().context_tokens) if self.ctx_live else 0
        return max(int(rep["used_pct"]), live)

    @rx.var
    def ctx_total(self) -> str:
        rep = json.loads(self.ctx_report)
        if self.ctx_live and self.ctx_live > 0:
            k = f"{self.ctx_live / 1000:.1f}k"
            return f"≈{rep['used_label']} / {rep['window_label']} · останній запит {k}"
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

    def _recent_image_urls(self) -> list[str]:
        """Картинки для моделі (data-URL): із двох останніх повідомлень користувача, що мають зображення, не більше
        images.MAX_IMAGES_PER_REQUEST. Викликати всередині `async with self` (читає self.messages)."""
        if not self.current_id:
            return []
        groups, names = 0, []
        for m in reversed(list(self.messages)):
            if m.get("role") != "user":
                continue
            ims = [n for n in (m.get("attachments") or []) if images.is_image_name(n)]
            if ims:
                names = ims + names
                groups += 1
                if groups >= 2:
                    break
        return images.data_urls(A.session_dir(self.current_id), names)

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
        self.summary_upto = 0
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
        self.shown_count = SHOWN_STEP
        self.messages = [{"thinking": "", "attachments": [], "thumbs": [], **m} for m in s.messages]
        self.has_pending = s.pending_plan is not None
        self.context_summary = s.context_summary
        self.summary_upto = s.summary_upto
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
                attachments: list | None = None, thumbs: list | None = None):
        self.messages = self.messages + [
            {"role": role, "content": content, "kind": kind, "thinking": thinking,
             "attachments": attachments or [], "thumbs": thumbs or []}]
        if self.current_id:
            s = sess.load_session(self.current_id)
            # Розгорнути Reflex-проксі, ВКЛЮЧНО з вкладеним списком attachments
            # (інакше asdict() при збереженні падає на MutableProxy).
            s.messages = [
                {"role": m["role"], "content": m["content"],
                 "kind": m.get("kind", "text"), "thinking": m.get("thinking", ""),
                 "attachments": list(m.get("attachments", []) or []),
                 "thumbs": list(m.get("thumbs", []) or [])}
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

    @rx.var
    def shown_messages(self) -> list[dict]:
        """Лише останні `shown_count` повідомлень — решта підвантажується кнопкою (клієнт не рендерить усю історію)."""
        return self.messages[-self.shown_count:] if len(self.messages) > self.shown_count else self.messages

    @rx.var
    def hidden_count(self) -> int:
        return max(0, len(self.messages) - self.shown_count)

    @rx.event
    def show_earlier(self):
        self.shown_count = self.shown_count + SHOWN_STEP

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
        sessionmem.clear(sid)
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
        self.app_ver = logs.app_version()
        self.font_chat = d.get("font_chat", 14)
        self.font_ui = d.get("font_ui", 13)
        from agent import backend
        eff = backend.resolve(d)
        self.llm_backend = eff["backend"]
        self.llm_model = d.get("llm_model", "")
        self.llm_context = str(d.get("llm_context", "") or "")
        self.llm_key_saved = bool(d.get("llm_api_key"))
        from agent import servers
        self.server_names = servers.names()
        A.migrate_legacy()                                      # вкладення зі старого місця (<репо>/.attachments)
        topics.migrate_legacy()                                 # теми чату зі старого місця (<код>/.chat_topics)
        self.gpu_enabled = bool(d.get("gpu_monitor", True))
        self.gpu_port = str(d.get("gpu_monitor_port") or "8001")
        self.llm_host, self.llm_port = d.get("llm_host", ""), str(d.get("llm_port", ""))
        if not self.llm_host and eff["backend"] == "openai":      # дефолт із .env/config
            from urllib.parse import urlparse
            u = urlparse(eff["base_url"])
            self.llm_host, self.llm_port = u.hostname or "", str(u.port or "")
        # Тему застосовує next-themes сам (зберігає в localStorage між перезавантаженнями);
        # set_color_mode НЕ можна викликати з бекенд-хендлера — лише шрифт через DOM.
        # PASTE_SETUP_JS — одноразово вішає document-listener вставки файлів (Ctrl+V).
        # poll_gpu стартуємо ТУТ, а не окремо в on_load: фонова подія читає gpu_enabled, виставлений вище, а при
        # паралельному старті могла прочитати ще старе значення й одразу вийти.
        return [rx.call_script(self.ui_font_js), rx.call_script(PASTE_SETUP_JS), rx.call_script(INPUT_GROW_JS),
                rx.call_script(AGO_JS), rx.call_script(GPU_JS), State.poll_gpu]

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

    # ── Навантаження GPU сервера ────────────────────────────────────────────────
    def _persist_gpu(self):
        from agent import settings as S
        S.save({**S.load(), "gpu_monitor": self.gpu_enabled, "gpu_monitor_port": self.gpu_port})

    @rx.event
    def set_gpu_monitor(self, v: bool):
        self.gpu_enabled = bool(v)
        self._persist_gpu()
        return State.poll_gpu                              # перезапускає (або зупиняє) цикл опитування

    @rx.event
    def set_gpu_port(self, v: str):
        self.gpu_port = "".join(ch for ch in v if ch.isdigit())[:5]
        self._persist_gpu()

    @rx.event(background=True)
    async def poll_gpu(self):
        """Цикл опитування агента моніторингу (раз на GPU_POLL_S). Запускається з on_load і при перемиканні
        налаштування; кожен запуск витісняє попередній цикл (_GPU_GEN). Вимкнено → очищає панель й виходить."""
        global _GPU_GEN
        from agent import gpumon
        _GPU_GEN += 1
        me = _GPU_GEN
        while me == _GPU_GEN:
            async with self:
                enabled, port = self.gpu_enabled, self.gpu_port
            if not enabled:
                return
            eff = B.resolve()
            if eff["backend"] != "openai":
                d = {"state": "na", "msg": "доступно лише для серверного бекенду (OpenAI API)", "series": []}
            else:
                key = eff.get("api_key") or os.environ.get(config.REMOTE_KEY_ENV, "")     # як і для виклику моделі
                d = await asyncio.to_thread(lambda: gpumon.fetch(eff["base_url"], key, port))
            if me != _GPU_GEN:                                # поки чекали, могли вимкнути/перезапустити
                return
            payload = {"v": gpumon.view(d), "series": d.get("series") or [], "span": gpumon.HISTORY_S}
            # НЕ через стан: подія-скрипт оновлює DOM картки напряму, React при цьому нічого не перемальовує
            yield rx.call_script("window.__igorGpu&&window.__igorGpu(" + json.dumps(payload, ensure_ascii=False) + ")")
            await asyncio.sleep(GPU_POLL_S)

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
    def pick_local_preset(self, idx: int):
        """Швидкий вибір локальної моделі разом із відповідним вікном (див. config.LOCAL_PRESETS)."""
        _, model, ctx = config.LOCAL_PRESETS[int(idx)]
        self.llm_model = model
        self.llm_context = str(ctx)

    @rx.event
    def set_llm_context(self, v: str):
        self.llm_context = "".join(ch for ch in v if ch.isdigit())[:7]

    @rx.event
    def set_llm_key_input(self, v: str):
        self.llm_key_input = v

    @rx.event
    def save_llm_settings(self):
        self._apply_llm_settings()

    @rx.event
    def set_server_pick(self, v: str):
        self.server_pick = v

    @rx.event
    def save_server(self):
        """«Зберегти сервер»: застосувати поточні налаштування й додати їх (разом з API-ключем) до списку збережених."""
        from agent import servers
        from agent import settings as S
        self._apply_llm_settings()
        cur = S.load()
        name = servers.save({"backend": self.llm_backend, "host": self.llm_host, "port": self.llm_port,
                             "model": self.llm_model, "context": self.llm_context, "api_key": cur.get("llm_api_key", "")})
        self.server_names = servers.names()
        self.server_pick = name
        self.llm_test_msg, self.llm_test_ok = f"Сервер збережено: {name}", True

    @rx.event
    def load_server(self):
        """«Завантажити»: підставити збережений профіль (разом з ключем) і одразу застосувати."""
        from agent import servers
        from agent import settings as S
        e = servers.get(self.server_pick)
        if not e:
            self.llm_test_msg, self.llm_test_ok = "Оберіть збережений сервер зі списку.", False
            return
        self.llm_backend, self.llm_host, self.llm_port = e["backend"] or "openai", e["host"], e["port"]
        self.llm_model, self.llm_context, self.llm_key_input = e["model"], e["context"], ""
        self.llm_models = []                                   # список моделей попереднього сервера вже не актуальний
        d = S.load()
        d.update(llm_backend=self.llm_backend, llm_host=e["host"], llm_port=e["port"], llm_model=e["model"],
                 llm_context=e["context"], llm_api_key=e["api_key"])
        S.save(d)
        reset_client()
        self.llm_rev += 1
        self.llm_key_saved = bool(e["api_key"])
        self.llm_test_msg, self.llm_test_ok = f"Завантажено: {e['name']}", True

    @rx.event
    def delete_server(self):
        from agent import servers
        name = self.server_pick
        if name and servers.delete(name):
            self.llm_test_msg, self.llm_test_ok = f"Видалено зі збережених: {name}", True
        self.server_names = servers.names()
        self.server_pick = ""

    def _apply_llm_settings(self):
        """Зберегти підключення й скинути закешований клієнт — наступний запит піде
        вже на нову адресу/ключ. Порожнє поле ключа = лишити збережений."""
        from agent import settings as S
        cur = S.load()
        new = {**cur, "llm_backend": self.llm_backend, "llm_host": self.llm_host.strip(),
               "llm_port": self.llm_port.strip(), "llm_model": self.llm_model.strip(),
               "llm_context": self.llm_context.strip()}
        if self.llm_key_input.strip():
            new["llm_api_key"] = self.llm_key_input.strip()
        S.save(new)
        _log.info("налаштування підключення: backend=%s host=%s port=%s model=%s ключ_змінено=%s",
                  new["llm_backend"], new["llm_host"], new["llm_port"], new["llm_model"],
                  bool(self.llm_key_input.strip()))
        reset_client()
        self.llm_rev += 1
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

    # ── Оновлення ────────────────────────────────────────────────────────────────
    @rx.event(background=True)
    async def check_updates(self, silent: bool = False):
        """Спитати GitHub про нову версію. silent=True (старт): без повідомлень, якщо нових нема чи збій мережі."""
        from agent import updater
        global _UPDATE_CHECKED
        if silent:
            if _UPDATE_CHECKED:                                 # тиха перевірка — раз на запуск, а не на кожне перезавантаження сторінки
                return
            _UPDATE_CHECKED = True
        await self._do_update_check(silent)

    @rx.event(background=True)
    async def poll_updates(self):
        """Періодична тиха перевірка оновлень (раз на UPDATE_POLL_S). Запускається з on_load; кожне нове
        завантаження сторінки витісняє попередній цикл (_POLL_GEN), тож працює рівно один."""
        global _POLL_GEN
        _POLL_GEN += 1
        me = _POLL_GEN
        while me == _POLL_GEN:
            await asyncio.sleep(UPDATE_POLL_S)
            if me != _POLL_GEN:
                return
            try:
                await self._do_update_check(True)
            except Exception as e:                              # noqa: BLE001 — фонова перевірка не має падати
                _log.warning("періодична перевірка оновлень не вдалась: %s", e)

    async def _do_update_check(self, silent: bool):
        from agent import updater
        async with self:
            if self.update_busy:
                return
            self.update_busy = True
            if not silent:
                self.update_msg, self.update_ok = "Перевіряю…", True
            cur = logs.app_version()
        info = await asyncio.to_thread(lambda: updater.check(cur, timeout=4.0 if silent else 8.0))
        async with self:
            self.update_busy = False
            self.app_ver = cur
            if info["newer"]:
                fresh = info["latest"] != self.update_latest        # нова версія, про яку ще не казали
                self._update_info = info
                self.update_available, self.update_latest = True, info["latest"]
                self.update_notes = info["notes"]
                if fresh:
                    self.update_dismissed = False                   # закритий банер не вертаємо щодві хвилини
                    _log.info("знайдено оновлення %s (у вас %s)", info["latest"], cur)
                self.update_msg, self.update_ok = f"Доступна нова версія {info['latest']} (у вас {cur}).", True
            elif silent:
                pass
            elif info["ok"]:
                self.update_available = False
                self.update_msg, self.update_ok = ((f"У вас остання версія ({cur}).") if info["latest"]
                                                   else "Опублікованих версій на GitHub ще немає."), True
            else:
                self.update_msg, self.update_ok = f"Не вдалося перевірити оновлення: {info['error']}", False

    @rx.event
    def dismiss_update(self):
        self.update_dismissed = True

    @rx.event(background=True)
    async def install_update(self):
        """Завантажити нову exe, звірити суму й попросити лаунчер закритись та підмінити файл."""
        from agent import updater
        async with self:
            if self.update_busy or not self.update_available:
                return
            info = dict(self._update_info)
            self.update_busy = True
            self.update_msg, self.update_ok = "Завантажую оновлення…", True
        ok, why = updater.can_self_update()
        if not ok:
            async with self:
                self.update_busy, self.update_msg, self.update_ok = False, why, False
            return
        box = {"p": 0.0}
        task = asyncio.create_task(asyncio.to_thread(
            lambda: updater.download(info, progress=lambda p: box.__setitem__("p", p))))
        while not task.done():
            await asyncio.sleep(0.5)
            async with self:
                self.update_msg = f"Завантажую оновлення… {int(box['p'] * 100)}%"
        try:
            await task
        except RuntimeError as e:
            _log.error("оновлення не завантажено: %s", e)
            async with self:
                self.update_busy, self.update_msg, self.update_ok = False, str(e), False
            return
        _log.info("оновлення %s завантажено й перевірено — перезапуск", info.get("latest"))
        async with self:
            self.update_msg, self.update_ok = "Оновлення завантажено. Перезапускаю Ігоря…", True
        updater.request_restart()

    @rx.event(background=True)
    async def test_llm_connection(self):
        """Перевірити адресу/порт/ключ (GET /models) до збереження; підставляє модель."""
        from agent import backend, settings as S
        async with self:
            if self.llm_testing:
                return
            self.llm_testing, self.llm_test_msg = True, "Перевіряю…"
            host, port, typed = self.llm_host, self.llm_port, self.llm_key_input.strip()
            local_model = self.llm_model.strip() if self.llm_backend == "ollama" else None
        if local_model is not None:                                # локальний режим: Ollama, а не OpenAI-сервер
            ok, msg = await asyncio.to_thread(lambda: backend.test_ollama(local_model))
            found = await asyncio.to_thread(backend.ollama_models)
            async with self:
                self.llm_testing, self.llm_test_ok, self.llm_test_msg = False, ok, msg
                self.llm_models = found
            return
        key = typed or S.load().get("llm_api_key", "")
        ok, msg, models = await asyncio.to_thread(
            lambda: backend.test_connection(backend.build_base_url(host, port), key))
        async with self:
            self.llm_testing, self.llm_test_ok, self.llm_test_msg = False, ok, msg
            self.llm_models = list(models) if ok else []
            if ok and models and not self.llm_model.strip():
                self.llm_model = models[0]

    # ── Вкладення (#10) ────────────────────────────────────────────────────────
    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        cid = self.current_id or "_tmp"     # до створення сесії — у тимчасову теку _tmp
        for file in files:
            if len(self.attachments) >= A.MAX_FILES:
                break
            data = await file.read()
            if images.sniff(data) or images.is_image_name(file.filename):
                # Зображення (скріпка, Ctrl+V, перетягування): перевірка за сигнатурою, зменшення, мініатюра.
                # Імена унікалізуємо за чіпами й файлами сесії (вставлений скриншот завжди «image.png» —
                # не можна затерти картинку з попереднього повідомлення).
                taken = {a["name"] for a in self.attachments} | {a["name"] for a in A.list_saved(cid)}
                res = images.prepare(file.filename, data, taken)
                raw = res.pop("_data", b"")
                if not res["error"] and raw:
                    A.save_bytes(cid, res["name"], raw, res["thumb"])
                chip = {"name": res["name"], "size": res["size"], "truncated": False, "error": res["error"],
                        "image": True, "thumb": res["thumb"]}
                self.attachments = self.attachments + [chip]
                continue
            if any(a["name"] == file.filename for a in self.attachments):
                continue                           # вже є — пропустити дублікат
            result = A.process(file.filename, data)
            content = result.pop("_content", "")  # вміст не тримаємо в State
            if not result["error"] and content:
                A.save(cid, file.filename, content)   # валідний → на диск
            result.setdefault("image", False)
            result.setdefault("thumb", "")
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

    def _save_pending(self, plan) -> None:
        """Зберегти план (зі статусами) як очікуваний: «Виконати» продовжить з першого
        невиконаного кроку. Викликати всередині `async with self`."""
        s = sess.load_session(self.current_id)
        s.set_pending_plan(plan)
        sess.save_session(s)
        self.has_pending = True

    async def _execute_steps(self, plan, root: str, client):
        """Обгортка виконання плану: поки він іде, повідомлення користувача не стають у чергу «на потім», а
        передаються планувальнику як коментар по ходу (див. send_task, _take_live_notes). Коментарі, що не
        встигли дійти до планувальника (прийшли після останньої перевірки), потрапляють у звичайну чергу."""
        async with self:
            cid = self.current_id
        _PLAN_ACTIVE.add(cid)
        try:
            return await self._execute_steps_inner(plan, root, client)
        finally:
            _PLAN_ACTIVE.discard(cid)
            left = _take_live_notes(cid)
            if left:
                async with self:
                    txt = "\n\n".join(left)
                    self.queued_text = (self.queued_text + "\n\n" + txt).strip() if self.queued_text else txt

    async def _execute_steps_inner(self, plan, root: str, client):
        """Виконати план: уся логіка — в ядрі агента (agent.plan_runner.PlanRunner: кроки, перегляд планувальником,
        зупинка/відновлення, звірка, підсумок); тут — лише середовище (PlanEnv) і адаптер до стану (_PlanUI).
        ToolContext: edits=auto (план уже схвалено — через plan_first або edits=auto); shell — як у сесії."""
        ctx = self._exec_ctx(root, client)
        async with self:
            cid = self.current_id
            env = PlanEnv(root=root, roots_note=self._roots_note(), autonomous=self.edits == "auto",
                          summary=self.context_summary, session_id=cid,
                          # хвіст розмови після підсумку — знімок на старті плану (далі в чат ідуть самі кроки плану)
                          tail=tail_for_plan(convo.dialogue_tail(list(self.messages), self.summary_upto)),
                          att_note=self._attachments_note())
        return await PlanRunner(_PlanUI(self, cid, ctx, _STOP_EVENTS.get(cid)), client, env).execute(plan)

    def _exec_ctx(self, root: str, client) -> ToolContext:
        """ToolContext для виконання: edits=auto (план/запит уже схвалено). Підтвердження
        shell тепер робить async-шар (_dispatch_tool/_confirm_async), тож confirm=None —
        самі тули блокуючого попапу більше не викликають."""
        return ToolContext(root=Path(root),
                           permissions={"edits": "auto", "shell": self.shell},
                           client=client, confirm=None,
                           attachments_dir=A.session_dir(self.current_id), track_project=True,
                           extra_roots=[Path(x) for x in self.extra_dirs], session_id=self.current_id)

    async def _dispatch_tool(self, reg, name: str, args: dict, ctx: ToolContext) -> str:
        """Виконати тул + показати подію в живій стрічці («що виконує і який результат»)."""
        async with self:
            aid = self._act_start("tool", name, act.summarize_args(name, args))
        t0 = time.monotonic()
        result = await self._dispatch_tool_inner(reg, name, args, ctx)
        async with self:
            if name in ("remember", "investigate"):
                self.notes_rev += 1
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
                             context: str = "", stop_event=None, checkpoint=None):
        """Async per-step tool-loop для UI. Саме ядро циклу — agent.steploop.run_loop (спільне з headless run_step
        і субагентом); тут лише підготовка запиту (картинки, дослідницький крок) і хуки середовища (_UIHooks):
        диспетчер із async-підтвердженням shell, картки активності, коментарі користувача по ходу, проміжні
        перевірки планувальника. Додає ОДНЕ повідомлення-крок із катом «хід виконання»."""
        from agent.agent_loop import (SYSTEM as EXEC_SYSTEM, is_research_step, research_report)
        from agent.steploop import LoopConfig, run_loop
        reg = default_registry()
        client = ctx.client
        user = (f"Контекст:\n{context}\n\n" if context else "") + f"Крок:\n{step_text}"
        async with self:
            imgs = self._recent_image_urls()             # скриншот/макет користувача — виконавцю теж
            cid_live = self.current_id
        if is_research_step(step_text) and "investigate" in reg.names():      # дослідницький крок → субагент кодом
            async with self:
                aid_sub = self._act_start("llm", "Субагент-дослідник", "читає файли в окремому контексті")
            t_sub = time.monotonic()
            user += await asyncio.to_thread(research_report, step_text, context, ctx, reg)
            async with self:
                self._act_done(aid_sub, f"{time.monotonic() - t_sub:.1f}с · висновок отримано")
                self.notes_rev += 1
        umsg = {"role": "user", "content": user}
        if imgs:
            umsg["images"] = imgs
        messages = [{"role": "system", "content": EXEC_SYSTEM}, umsg]
        # довгу роботу не обмежуємо (стежимо за прогресом, LoopGuard); hard_iters — лише запобіжник
        res = await run_loop(messages, registry=reg, ctx=ctx, client=client,
                             hooks=_UIHooks(self, cid_live, stop_event, checkpoint),
                             cfg=LoopConfig(max_iters=step_iter_limit()))
        final, full_actions, stats = res.final, res.actions, res.stats
        actions = [(n, r) for n, _a, r in full_actions]
        touched: list[str] = []               # файли, змінені цим кроком (для звірки)
        for name, args, result in full_actions:
            if act.result_ok(result):
                touched.extend(orch.touched_paths(name, args))
        log = [f"{n} -> {r[:80]}" for n, r in actions]

        parts: list[str] = []
        for name, result in actions:
            if name in ("edit_file", "create_from_source", "replace_in_file"):
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
            self._step_actions = full_actions
            self._add_stats(stats)                      # лічильник над полем вводу
            self._append("assistant", content, "step", thinking=log_md)
        return final, log

    async def _run_stream(self, gen, keep_stop: bool = False):
        """Прокрутити стрім із живим лічильником токенів. Повідомлення НЕ додає —
        повертає (body, thinking, tool_calls). Спільне для answer і chat-режиму.
        Підтримує переривання через _STOP_EVENTS[current_id]. keep_stop=True (стрім усередині циклу з тулами) — стоп-подію
        не прибираємо: її має побачити й сам цикл, інакше «Стоп» зупиняв би лише поточний стрім, а цикл ішов би далі."""
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
        # Токени копимо ЛОКАЛЬНО й віддаємо в стан не частіше за STREAM_FLUSH_S: кожне оновлення стану —
        # це повна передача stream_content у браузер + перерозбір markdown усього тексту + перемальовка
        # сторінки. Робити це на кожен токен (десятки разів на секунду) — квадратичне навантаження й лаги.
        acc_c, acc_t = "", ""
        pushed_c = pushed_t = 0                     # скільки символів уже віддано в стан
        last_flush = 0.0
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
            acc_c += ev["content"]
            acc_t += ev["thinking"]
            if ev["content"]:
                counted += 1
            now = time.monotonic()
            if now - last_flush >= STREAM_FLUSH_S and (len(acc_c) != pushed_c or len(acc_t) != pushed_t):
                last_flush = now
                pushed_c, pushed_t = len(acc_c), len(acc_t)
                async with self:
                    self.stream_content = acc_c
                    self.stream_thinking = acc_t[-STREAM_THINK_TAIL:]      # у UI лише хвіст; повні роздуми — acc_t
                    self.tok_out = base_out + counted
                    el = time.time() - t0
                    if el > 0:
                        self.tok_tps = round(self.tok_out / el, 1)
        async with self:
            self.stream_content = acc_c                  # хвіст, що не встиг пройти throttle
            self.stream_thinking = acc_t                 # повні роздуми (для збереження в повідомлення)
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
            if not keep_stop:
                self.stopping = False
                _STOP_EVENTS.pop(cid, None)
        return body, thinking, tool_calls

    # Тули режимів чату й аналізу (лише читання): пошук, читання файлів/вкладень, пам'ять, повна історія розмови.
    _READONLY_TOOLS = ("web_search", "read_file", "search", "list_dir", "recall", "recall_history")

    # Заголовок read_file, коли файл читається частинами (toolkit.h_read_file): "[name: рядки A-B з N]" / "символи".
    _PAGINATED_HEADER_RE = re.compile(r"^\[(.+?): (?:рядки|символи) \d+-\d+ з \d+\]\n")
    # Хвіст-підказка "лишилось N … read_file(path=…, start_line|offset=…)]" — лишаємо як є, щоб
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

    def _needs_digest(self, tctx, name: str, result: str) -> str:
        """Ім'я вкладення, чий шматок варто злити в дайджест ('' — ні): лише читання ВЕЛИКОГО вкладення частинами
        (файл більший за limits.inline — сирі шматки не вмістились би у вікно). Менші файли йдуть як є, без втрат
        (CRUTCHES.md B2: дайджест втратний — губить точні цифри й цитати)."""
        if name not in ("read_file", "read_attachment") or not getattr(tctx, "attachments_dir", None):
            return ""
        m = self._PAGINATED_HEADER_RE.match(result or "")
        if not m:
            return ""
        p = Path(tctx.attachments_dir) / m.group(1)
        try:
            return m.group(1) if p.is_file() and p.stat().st_size > limits.get().inline else ""
        except OSError:
            return ""

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
        if not (body or "").strip():
            # усі спроби з думанням дали лише роздуми (живий кейс b_0.0.37: «(порожня відповідь)») — остання спроба БЕЗ думання
            def _plain():
                m = client.chat(msgs + [{"role": "user", "content": "Дай коротку конкретну відповідь користувачу зараз, "
                                         "без роздумів, на основі всього, що вже відомо."}],
                                profile={**profile, "think": False})
                return (m.get("content") or "").strip()
            try:
                body = await asyncio.to_thread(_plain)
            except Exception:                                   # noqa: BLE001 — запасний шлях не має ламати відповідь
                body = ""
        return body, thinking

    CHAT_FINALIZE = ("Досить збирати інформацію. Дай повну відповідь користувачу за тим, що вже прочитано й знайдено; "
                     "більше не викликай інструменти. Якщо чогось не вистачило — так і скажи, чого саме.")

    @staticmethod
    def _read_floor(cid: str, names: list) -> int:
        """Скільки читань поспіль дозволити до примусового висновку: велике вкладення читається багатьма частинами.
        Рахуємо ДЕТЕРМІНОВАНО з розміру файлів (раніше — ще й окремим викликом моделі в _estimate_rounds)."""
        if not names or not cid:
            return 0
        sizes = {n["name"]: n["size"] for n in A.list_saved(cid)}
        chunk = max(1, limits.get().chunk)
        total = sum(-(-sizes.get(n, 0) // chunk) for n in names)
        return total + 4 if total else 0

    async def _run_read_loop(self, msgs: list, tctx: ToolContext, client, read_floor: int = 0,
                             digest_profile: dict = config.EXECUTOR) -> str:
        """Режими чату й аналізу на СПІЛЬНОМУ ядрі циклу (agent.steploop.run_loop), як і кроки код-режиму: ті самі
        запобіжники (зациклення, читання без дій, стиснення з адресою оригіналу, дочитування вкладень). Раніше тут жив
        окремий цикл _run_tool_chat із фазою збору й синтезу та власними копіями милиць (CRUTCHES.md, розділ C).
          * сильна модель (weak_model_fixes вимкнено): ОДИН цикл із роздумами й стрімом — остання репліка і є
            відповідь (живий тест T1: однофазний цикл надійно викликає тули);
          * слабка модель (weak_model_fixes): збір тулами без роздумів, потім окремий синтез із роздумами (як раніше).
        Порожня відповідь чи висновок після запобіжника → _synthesize_with_retry. Додає відповідь у чат, повертає текст."""
        from agent.agent_loop import READ_FORCE, READ_WARN
        from agent.steploop import STOPPED_MSG, LoopConfig, run_loop
        weak = limits.get().weak_model_fixes
        reg = default_registry().subset(self._READONLY_TOOLS)
        async with self:
            cid = self.current_id
        hooks = _ReadHooks(self, cid, client, stream=not weak, digest_profile=digest_profile)
        cfg = LoopConfig(max_iters=max(step_iter_limit(), read_floor + 8),
                         profile=config.EXECUTOR if weak else config.PLANNER, summarize=False, finalize_on_stuck=True,
                         read_warn=max(READ_WARN, read_floor), read_force=max(READ_FORCE, read_floor + 4),
                         finalize_text=self.CHAT_FINALIZE)
        res = await run_loop(msgs, registry=reg, ctx=tctx, client=client, hooks=hooks, cfg=cfg)
        if weak:                                            # стрім рахує токени сам; виклики без стріму — тут
            async with self:
                self._add_stats(res.stats)
        body, thinking = (res.final or "").strip(), hooks.last_thinking
        if body == STOPPED_MSG:
            async with self:
                self._append("assistant", "⛔ Зупинено.", "note")
            return ""
        if weak or not body or is_incomplete(body):
            async with self:
                self.status = "Формулюю відповідь…"
            if body and is_incomplete(body):
                msgs.append({"role": "user", "content": self.CHAT_FINALIZE})
            body, thinking = await self._synthesize_with_retry(msgs, client)
        async with self:
            self._append("assistant", body or "(не вдалося сформувати відповідь)", "answer", thinking=thinking)
        return body

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
        lines = ["[Доступні файли — читай тулом read_file(path=<ім'я>), шукай у них — search(pattern):"]
        if cur:
            lines.append("• Додані до ЦЬОГО повідомлення (опрацюй): " + "; ".join(cur))
        if others:
            lines.append("• Інші файли розмови (перечитуй ЛИШЕ якщо запит саме про них): "
                         + "; ".join(others))
        return content + "\n\n" + "\n".join(lines) + "]"

    @staticmethod
    def _attach_or_reference(cid: str, content: str, cur_names: list, other_names: list) -> tuple[str, bool]:
        """Якщо файли цього повідомлення (cur_names) РАЗОМ малі (limits.inline) —
        вшити їхній ВМІСТ напряму в текст (один LLM-виклик, без тул-циклу читання).
        Інакше — лишити посилання на read_file(ім'я), як і для other_names.
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
        """Чат-режим: думаюча модель + тули читання (спільне ядро циклу, _run_read_loop). У поточну user-репліку вшиваємо
        імена файлів, прикріплених САМЕ до неї (m['attachments']); модель читає їх через
        read_file за іменем (attachments_dir = тека вкладень сесії)."""
        async with self:
            cid = self.current_id
            proj_id = self.chat_project_id
            imgs = self._recent_image_urls()
            cur_atts = list(self.messages[-1].get("attachments", []) or []) if self.messages else []
            # старіші репліки — у підсумку (раніше він рахувався, але в промпт чату не йшов), свіжі — дослівно
            summary = self.context_summary
            history = [{"role": m["role"], "content": m["content"]}
                       for m in self.messages[self.summary_upto:]
                       if m["role"] in ("user", "assistant")
                       and m.get("kind", "text") in ("text", "answer") and m["content"]][-limits.get().history_msgs:]
        proj = CP.load_project(proj_id) if proj_id else None
        project_block = CP.as_system_block(proj.prompt) if proj else ""
        cur_atts = [n for n in cur_atts if not images.is_image_name(n)]      # картинки йдуть окремо, як зображення
        all_names = [n["name"] for n in A.list_text(cid)]
        other_names = [n for n in all_names if n not in cur_atts]
        # Вшити переліки файлів у ОСТАННЮ user-репліку (поточний хід), якщо файли є.
        # cur_atts (файли САМЕ цього повідомлення) вшиваються ВМІСТОМ напряму, якщо
        # сумарно малі (limits.inline) — без жодного раунду читання;
        # other_names лишаються лише посиланням (тулом, "якщо запит саме про них").
        if all_names and history and history[-1]["role"] == "user":
            new_content, _ = self._attach_or_reference(cid, history[-1]["content"], cur_atts, other_names)
            history[-1] = {**history[-1], "content": new_content}
        tctx = ToolContext(root=A.session_dir(cid),
                           permissions={"edits": "off", "shell": "off"}, client=client,
                           attachments_dir=A.session_dir(cid), session_id=cid)
        all_topics = await asyncio.to_thread(topics.list_topics)
        topics_note = topics.available_topics_note(all_topics)
        # project_block ПЕРШИМ — інструкції проекту задають рамку розмови, тема й
        # вкладення йдуть як додатковий контекст усередині цієї рамки.
        system = (project_block + CHAT_SYSTEM + ("\n\n" + topics_note if topics_note else "")
                  + ("\n\n" + convo.as_context(summary).strip() if summary else ""))
        if imgs and history and history[-1]["role"] == "user":      # зображення — до поточної репліки користувача
            history[-1] = {**history[-1], "images": imgs}
        msgs = [{"role": "system", "content": system}] + history
        return await self._run_read_loop(msgs, tctx, client, self._read_floor(cid, cur_atts or all_names),
                                         digest_profile=config.CHAT_EXECUTOR)

    # Системний промпт answer-режиму В ТУЛ-ЦИКЛІ: на відміну від answerer.SYSTEM, явно
    # каже про read_file/list_dir — інакше модель «просить користувача прислати файли»
    # замість того, щоб прочитати їх самій (той самий баг, що зупиняв аналіз після плану).
    _ANSWER_TOOLS_SYSTEM = (
        "You are a code analysis assistant for a local project. You have tools: "
        "search(pattern) to find lines in project files (use it first to locate code), "
        "read_file(path, start_line, limit) and list_dir(path) to inspect ANY file in the project, "
        "recall_history(query) for exact details of the earlier conversation, plus "
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
            imgs = self._recent_image_urls()
            cur_atts = list(self.messages[-1].get("attachments", []) or []) if self.messages else []
        tctx = ToolContext(root=Path(root) if root else Path.home(),
                           permissions={"edits": "off", "shell": "off"}, client=client,
                           attachments_dir=A.session_dir(cid), track_project=bool(root),
                           extra_roots=[Path(x) for x in self.extra_dirs], session_id=cid)
        cur_atts = [n for n in cur_atts if not images.is_image_name(n)]
        all_names = [n["name"] for n in A.list_text(cid)]
        other_names = [n for n in all_names if n not in cur_atts]
        # cur_atts малі сумарно -> вшиваємо вмістом напряму (менше раундів читання)
        question, _ = self._attach_or_reference(cid, text, cur_atts, other_names)
        parts = [f"Контекст:\n{ctx}" if ctx else "", f"Питання:\n{question}"]
        user = "\n\n".join(p for p in parts if p)
        umsg = {"role": "user", "content": user}
        if imgs:
            umsg["images"] = imgs
        msgs = [{"role": "system", "content": ANSWER_SYSTEM}, umsg]
        # Аналіз коду на спільному ядрі циклу. Колишній баг «зупинка після планування» (модель лише описувала, що
        # прочитає) лікувався фазою збору без роздумів — для слабкої моделі вона лишилась (weak_model_fixes).
        return await self._run_read_loop(msgs, tctx, client, self._read_floor(cid, cur_atts or all_names))

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
        """Контекст-пам'ять після завершеного запиту — «ліниво»: свіжі репліки лишаються в контексті ДОСЛІВНО (хвіст після
        summary_upto), а підсумок оновлюється ОДНИМ викликом лише тоді, коли хвіст переріс межу (convo.compact_tail).
        Раніше — виклик моделі після КОЖНОГО запиту. Модель однопотокова, тож це в межах busy; нові повідомлення стають
        у чергу. cid-гард: якщо користувач перемкнув чат, пишемо в сесію-походження. Теми чату (спільна пам'ять між
        чатами) — як і раніше, після кожного ходу: чат може закінчитись раніше, ніж хвіст переросте межу."""
        if not (outcome or "").strip():
            return
        client = await asyncio.to_thread(get_client)
        async with self:
            cid = self.current_id
            chat = self.mode == "chat"
            cur_topics = list(self.session_topics)
        if not cid:
            return
        lim = limits.get()
        s0 = await asyncio.to_thread(lambda: sess.load_session(cid))
        if chat:   # чат: хвіст = вікно історії; загальний підсумок менш детальний (деталі — у темах), бюджет утричі менший
            kw = dict(max_msgs=lim.history_msgs, max_chars=lim.summary // 2, keep=max(4, lim.history_msgs // 2),
                      budget=lim.summary // 3, kind="chat", kinds=("text", "answer"))
        else:
            kw = dict(max_msgs=convo.TAIL_MAX, max_chars=lim.summary // 4, keep=convo.TAIL_KEEP, budget=lim.summary,
                      kind="code")
        events: list[tuple] = []
        note_cb = lambda w, b, a: events.append((w, b, a, ""))     # noqa: E731
        new, upto = await asyncio.to_thread(
            lambda: convo.compact_tail(s0.context_summary, s0.messages, s0.summary_upto, client, on_compress=note_cb, **kw))
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
                self.summary_upto = upto
                self.session_topics = new_topics
            s = sess.load_session(cid)                # у файл — завжди в сесію-походження
            s.context_summary, s.summary_upto, s.topics = new, upto, new_topics
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
            if not text and any(a.get("image") and not a.get("error") for a in self.attachments):
                text = "Прикріплено зображення."            # картинка без підпису — теж повідомлення
            if not text:
                return
            self.task = ""
            att_names = [a["name"] for a in self.attachments if not a.get("error")]
            thumbs = [a["thumb"] for a in self.attachments if not a.get("error") and a.get("thumb")]
            # Великий вставлений/набраний текст у полі вводу не має жодного ліміту
            # (на відміну від файлових вкладень — limits.max_file) і летить прямо в
            # промпт, переповнюючи контекст (живий кейс: 687k символів — модель
            # падає). Той самий ліміт, що й для файлів — понад нього зберігаємо як
            # вкладення на диск; модель читає частинами через read_file / шукає search, а не
            # отримує суцільний блок у контексті.
            if len(text) > limits.get().max_file:
                kb = len(text) // 1024
                paste_name = f"paste_{int(time.time())}.txt"
                A.save(self.current_id, paste_name, text)
                att_names.append(paste_name)
                text = (f"[Вставлений текст ({kb} KB) занадто великий для прямого "
                        f"включення — збережено як вкладення '{paste_name}'. Прочитай "
                        f"його через read_file('{paste_name}') частинами або знайди потрібне search(pattern).]")
            # Імена прикріплених файлів → у саме повідомлення (видно в чаті), потім
            # стейджинг-чіпи прибираємо з-під інпута. Файли лишаються на диску.
            self._append("user", text, attachments=att_names, thumbs=thumbs)
            self.attachments = []
            if self.has_pending and not self.busy and orch.is_confirmation(text):
                self._append("assistant", "Це підтвердження плану — натисніть «Виконати» під планом (або «Відхилити»).", "note")
                return
            if self.busy and self.current_id in _PLAN_ACTIVE and not self.has_question:
                # План виконується: це коментар по ходу. Не чекаємо кінця — планувальник побачить його на
                # найближчій перевірці разом зі звітом виконавця й скоригує план; виконавець — між своїми діями.
                _LIVE_NOTES.setdefault(self.current_id, []).append({"text": text, "seen": False})
                self._append("assistant", "📝 Коментар прийнято — планувальник врахує його після поточного кроку "
                                          "(виконавець побачить раніше).", "note")
                return
            # Модель зайнята або чекає «Виконати/Відхилити» -> у чергу. Питання планувальника (has_question) НЕ блокує:
            # текст у полі — це відповідь на нього (_process_one_inner: answering → _resume_planning). Раніше такий
            # текст відкладався в чергу й ніхто не реагував, поки не натиснеш «Скасувати».
            if self.busy or self.has_pending:
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
            autonomous = self.edits == "auto"          # режим самостійних правок: лог помилки = виправляти
            summary = self.context_summary
            att_note = self._attachments_note()     # шляхи прикріплених файлів (модель читає сама)
            last_ai = orch.last_assistant_text(self.messages)      # ПОВНА попередня відповідь (не підсумок)
            dialogue = convo.dialogue_tail(self.messages, self.summary_upto)   # репліки після підсумку — дослівно
            answering = self.has_question
            if answering:
                self.has_question = False
                self.status = "Обмірковую відповідь…"
            else:
                self.status = "Думаю…" if chat_mode else "Обмірковую запит…"

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
                task_text, quick = approved, "edit"
                su = f"{text} (виконати запропоноване: {last_ai[:160]}…)"
                async with self:
                    self._append("assistant", "▶ Сприймаю це як команду виконати те, що щойно було запропоновано.", "note")
            else:
                task_text = text
                quick = quick_route(text, autonomous=autonomous, has_attachments=bool(att_note))   # однозначне — без моделі

            async def answer() -> str:
                async with self:
                    self.status = "Аналізую код…"
                ctx = await asyncio.to_thread(lambda: build_context(root, refs, text))
                return await self._answer_reply(text, convo.as_context(summary) + dialogue + pmap + ctx,
                                                att_note, root, client)

            if quick == "answer":
                so = await answer()
            else:
                # ОДИН виклик планувальника і визначає тип запиту (answer / shell), і планує (раніше — окремий
                # класифікатор наміру перед ним). «роби» й лог помилки в auto — одразу планування, без маршрутизації.
                doc = await asyncio.to_thread(lambda: load_project_doc(root))
                struct = await asyncio.to_thread(lambda: scan_structure(root))
                ctx_plan = ((orch.AUTONOMY_NOTE + "\n\n" if autonomous else "") + convo.as_context(summary) + dialogue
                            + pmap + struct)
                result = await self._decide(task_text, ctx_plan, doc, [], route=not quick, autonomous=autonomous,
                                            client=client)
                if result["action"] == "answer":
                    so = await answer()
                elif result["action"] == "shell":
                    so = await self._shell_reply(text, root, pmap, summary, att_note, client)
                else:
                    su2, so = await self._handle_decision(result, task_text, ctx_plan, doc, [], root,
                                                          plan_first or result.get("wait", False), client)
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
            logs.session_var.set(self.current_id or "-")
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

    async def _shell_reply(self, text: str, root: str, pmap: str, summary: str, att_note: str, client) -> str:
        """«Просто запусти» (планувальник обрав shell): один крок виконавця без плану."""
        async with self:
            self.status = "Виконую запит…"
        ctx = self._exec_ctx(root, client)
        rn = self._roots_note()
        shell_ctx = f"Project root: {root}\n{rn}{pmap}{convo.as_context(summary)}"
        if att_note:
            shell_ctx += "\n" + att_note
        final, _ = await self._run_tool_step(text, ctx, context=shell_ctx, stop_event=_STOP_EVENTS.get(self.current_id))
        return final or "виконано дії інструментами"

    async def _decide(self, task: str, context: str, doc, history: list, route: bool = False,
                      autonomous: bool = False, client=None) -> dict:
        """Один виклик планувальника (planner.deliberate). route=True — він же визначає тип запиту (answer/shell)."""
        async with self:
            self.status = "Обмірковую запит…" if route else "Обмірковую план…"
            att_note = self._attachments_note()
            imgs = self._recent_image_urls()
        client = client or await asyncio.to_thread(get_client)
        full_context = (context + "\n\n" + att_note).strip() if att_note else context
        kw = {"images": imgs} if imgs else {}
        return await asyncio.to_thread(
            lambda: deliberate(task, full_context, client, doc, history, route=route, autonomous=autonomous,
                               has_attachments=bool(att_note), **kw))

    async def _deliberate_flow(self, task: str, context: str, doc, history: list,
                               root: str, plan_first: bool, client):
        """Один хід планувальника-діалогу (продовження після відповіді на його питання): рішення + обробка.
        Повертає (task, outcome) — outcome='' якщо пауза/не виконано (підсумок не потрібен)."""
        result = await self._decide(task, context, doc, history, client=client)
        return await self._handle_decision(result, task, context, doc, history, root, plan_first, client)

    async def _handle_decision(self, result: dict, task: str, context: str, doc, history: list,
                               root: str, plan_first: bool, client):
        """clarify/choose -> питання+кнопки (пауза); plan -> готовий план (виконати або показати на підтвердження)."""
        action = result["action"]

        if action in ("clarify", "choose"):
            question, reasoning = (result.get("question") or "").strip(), (result.get("reasoning") or "").strip()
            if not question:
                # Модель обрала уточнення, але питання не сформулювала (лише пояснення). Раніше виходило «****» —
                # у markdown це горизонтальна лінія — без жодного питання. Пояснення і є питання; нічого не вигадуємо.
                question, reasoning = reasoning or "Потрібне уточнення — напишіть відповідь у полі нижче.", ""
            q = {"task": task, "context": context, "doc": doc, "history": history,
                 "question": question, "reasoning": reasoning,
                 "options": result["options"]}
            async with self:
                head = (reasoning + "\n\n") if reasoning else ""
                self._append("assistant", f"{head}**{question}**", "plan")
                self.q_text = question
                self.q_reasoning = reasoning
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
        logs.session_var.set(cid or "-")                   # id чату в рядках лога виконання плану (раніше тут був «-»)

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
