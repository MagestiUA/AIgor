"""Реєстр інструментів для агентного tool-loop.

Кожен інструмент = {схема, обробник}. Додати новий тул = зареєструвати його, без
змін у циклі. Обробники працюють у ToolContext (root, дозволи, клієнт, confirm).

Файлові операції — окремі тули (write_file/read_file/list_dir), щоб модель не
залежала від PowerShell-cmdlet'ів; run_shell — для запуску програм (python/pytest/git).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import time as _time

from . import limits
from . import logs
from . import tools as T
from .executor import apply_edit, create_from_source, run_edit, run_patch
from .project import scan_structure


@dataclass
class ToolContext:
    root: Path
    permissions: dict = field(default_factory=lambda: {"edits": "ask", "shell": "allowlist"})
    client: object = None
    confirm: Callable | None = None     # (text) -> bool
    attachments: list = field(default_factory=list)   # метадані вкладень (без вмісту)
    attachments_dir: Path | None = None  # тека з файлами вкладень сесії (для read_attachment)
    track_project: bool = False          # оновлювати «Карту проєкту» при read/write (код-режим)
    extra_roots: list = field(default_factory=list)   # додаткові теки проєкту: читання/запис за абсолютним шляхом
    session_id: str = ""                 # id чату — для нотаток сесії (remember/recall)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    required: list
    handler: Callable                    # (args: dict, ctx: ToolContext) -> str


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        # Тули лише для сумісності (стара назва -> тул, що її замінив): модель могла бачити стару назву в історії чи
        # підказках — виклик виконується, але в схемі й names() таких тулів нема.
        self._hidden: dict[str, str] = {}

    def register(self, tool: Tool, compat_for: str = "") -> None:
        self._tools[tool.name] = tool
        if compat_for:
            self._hidden[tool.name] = compat_for

    def visible_for(self, name: str) -> str:
        """Назва тулу, яку бачить модель: для прихованого тулу сумісності — його замінник."""
        return self._hidden.get(name, name)

    def names(self) -> list[str]:
        return [n for n in self._tools if n not in self._hidden]

    def subset(self, names) -> "ToolRegistry":
        """Реєстр лише з цими тулами (+ приховані тули сумісності, якщо їхній замінник лишився)."""
        r = ToolRegistry()
        keep = set(names)
        for n, t in self._tools.items():
            if n in keep or self._hidden.get(n) in keep:
                r.register(t, compat_for=self._hidden.get(n, ""))
        return r

    def schema(self) -> list[dict]:
        """Формат Ollama tools=[...]."""
        return [{
            "type": "function",
            "function": {
                "name": t.name, "description": t.description,
                "parameters": {"type": "object", "properties": t.parameters,
                               "required": t.required},
            },
        } for t in self._tools.values() if t.name not in self._hidden]

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> str:
        log = logs.get("tools")
        t = self._tools.get(name)
        if not t:
            log.warning("невідомий інструмент: %s", name)
            return f"невідомий інструмент: {name}"
        t0 = _time.monotonic()
        try:
            out = t.handler(args or {}, ctx)
            _track_project(name, args or {}, out, ctx)
        except Exception as e:
            log.error("тул %s упав за %.2fс (аргументи: %s): %s", name, _time.monotonic() - t0,
                      ",".join((args or {}).keys()), e, exc_info=True)
            return f"помилка інструмента {name}: {e}"
        log.info("тул %s ок %.2fс args=[%s] результат=%d симв.%s", name, _time.monotonic() - t0,
                 ",".join((args or {}).keys()), len(out or ""),
                 f" | {logs.clip(args)} -> {logs.clip(out)}" if logs.content_enabled() else "")
        return out


def _track_project(name: str, args: dict, out: str, ctx) -> None:
    """Після читання/зміни файлу оновлюємо «Карту проєкту»: що було, коли читали, і свіжий скелет
    після власних правок. Помилки карти ніколи не ламають тул."""
    if not getattr(ctx, "track_project", False):
        return
    try:
        from . import activity, projectmap
        if not activity.result_ok(out):
            return
        pth = args.get("path") or args.get("target")
        if pth and not _under_root(ctx, pth):             # файл у додатковій теці — не в карті цього проєкту
            return
        if name == "read_file" and args.get("path"):
            projectmap.mark_read(ctx.root, args["path"])
        elif name in ("write_file", "edit_file") and args.get("path"):
            projectmap.refresh_file(ctx.root, args["path"])
        elif name == "create_from_source" and args.get("target"):
            projectmap.refresh_file(ctx.root, args["target"])
    except Exception:                                      # noqa: BLE001
        logs.get("tools").exception("оновлення карти проєкту не вдалось")


def h_project_note(args, ctx):
    from . import projectmap
    rel = (args.get("path") or "").strip()
    note = (args.get("note") or "").strip()
    if not rel or not note:
        return "помилка: потрібні path і note"
    p = _resolve(ctx, rel)
    if not p.is_file():
        return f"файл не знайдено: {rel}"
    if not _under_root(ctx, str(p)):
        return "нотатки в карті проєкту ведуться лише для файлів головної теки"
    ok =projectmap.set_note(ctx.root, str(p.relative_to(ctx.root.resolve())), note)
    return "нотатку збережено в карті проєкту" if ok else "не вдалось зберегти нотатку"


# ── Обробники ────────────────────────────────────────────────────────────────
def _resolve(ctx: ToolContext, path: str) -> Path:
    """Резолвити шлях відносно кореня проєкту й боронити вихід за його межі
    (traversal через `..` або абсолютний шлях). Без цього модель — особливо при
    edits=auto — могла б читати/писати будь-де на диску (напр. ~/.ssh, поза проєктом).
    Кидає ValueError; ToolRegistry.dispatch ловить його й віддає текст моделі."""
    root = ctx.root.resolve()
    p = Path(path)
    full = (p if p.is_absolute() else root / p).resolve()
    for base in [root, *(Path(x).resolve() for x in ctx.extra_roots), *_aux_roots(ctx)]:   # корінь + додані теки + службові
        if full == base or base in full.parents:
            return full
    raise ValueError(f"шлях поза межами проєкту заборонено: {path}")


def _aux_roots(ctx) -> list[Path]:
    """Службові теки, які модель може читати за повним шляхом: повні виводи тулів (spill) і вкладення цього чату."""
    from .spill import SPILL_ROOT
    out = [SPILL_ROOT.resolve()]
    if getattr(ctx, "attachments_dir", None):
        out.append(Path(ctx.attachments_dir).resolve())
    return out


def _attachment_files(ctx) -> list[Path]:
    d = getattr(ctx, "attachments_dir", None)
    if not d or not Path(d).is_dir():
        return []
    from .images import is_image_name
    return sorted(p for p in Path(d).iterdir() if p.is_file() and not is_image_name(p.name))


def _find_attachment(ctx, name: str) -> Path | None:
    """Вкладення за іменем, прощаючи неточності довгого імені: точне → без регістру → підрядок."""
    files = _attachment_files(ctx)
    q = Path((name or "").strip()).name
    if not q or not files:
        return None
    exact = next((p for p in files if p.name == q), None)
    if exact:
        return exact
    ql = q.lower()
    cand = ([p for p in files if p.name.lower() == ql] or [p for p in files if ql in p.name.lower()]
            or [p for p in files if p.name.lower() in ql])
    return cand[0] if cand else None


def _is_attachment(ctx, p: Path) -> bool:
    d = getattr(ctx, "attachments_dir", None)
    return bool(d) and Path(d).resolve() in p.resolve().parents


def _under_root(ctx: ToolContext, path: str) -> bool:
    """Файл лежить у ГОЛОВНІЙ теці проєкту (карта проєкту ведеться лише для неї)."""
    try:
        root = ctx.root.resolve()
        p = Path(path)
        full = (p if p.is_absolute() else root / p).resolve()
        return full == root or root in full.parents
    except (OSError, ValueError):
        return False


def h_list_dir(args, ctx):
    return scan_structure(_resolve(ctx, args.get("path", ".")))


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# Підказка автопродовження для ВКЛАДЕНЬ (agent_loop.pending_continuation дочитує їх сам, якщо модель зупинилась):
# лише для вкладень — частково прочитаний файл коду модель могла читати навмисно (start_line/limit).
CONTINUE_HINT = "лишилось {left} рядків. Щоб продовжити, виклич read_file(path='{label}', start_line={nxt})"
CONTINUE_HINT_CHARS = "лишилось {left} символів. Щоб продовжити, виклич read_file(path='{label}', offset={nxt})"


def _locate(ctx, raw: str) -> tuple[Path | None, str, str]:
    """Знайти файл для read_file -> (шлях, мітка для моделі, помилка). Порядок: шлях у проєкті/дозволених теках →
    вкладення за іменем (нечітко). Шлях поза дозволеними теками — помилка «поза межами» (якщо це не ім'я вкладення)."""
    err = ""
    try:
        p = _resolve(ctx, raw)
        if p.is_file():
            return p, (p.name if _is_attachment(ctx, p) else raw), ""
    except ValueError as e:
        err = str(e)
    att = _find_attachment(ctx, raw)
    if att is not None:
        return att, att.name, ""
    if err:
        return None, raw, err
    names = ", ".join(p.name for p in _attachment_files(ctx))
    return None, raw, f"файл не знайдено: {raw}" + (f". Прикріплені файли: {names}" if names else "")


def _read_by_chars(text: str, label: str, offset: int, cap: int, att: bool = False) -> str:
    """Читання за позицією в СИМВОЛАХ (offset): для рядків, довших за ліміт (вставлений текст в один рядок,
    мініфіковані файли). Для вкладень — та сама підказка «лишилось…», яку pending_continuation дочитує сам."""
    chunk = text[offset:offset + cap]
    end = offset + len(chunk)
    head = f"[{label}: символи {offset}-{end} з {len(text)}]"
    if end >= len(text):
        return f"{head}\n{chunk}"
    if att:
        return f"{head}\n{chunk}\n\n…[" + CONTINUE_HINT_CHARS.format(left=len(text) - end, label=label, nxt=end) + "]"
    return f"{head}\n{chunk}\n\n…[далі: read_file(path='{label}', offset={end})]"


def h_read_file(args, ctx):
    """Прочитати файл проєкту або вкладення. Маленький файл — цілком і як є; великий — частинами за РЯДКАМИ
    (start_line з 1, limit — скільки рядків) із заголовком «рядки a-b з N» і підказкою, як читати далі.
    Рядкові номери узгоджені з тулом search (шлях:рядок), тож модель читає лише потрібне місце."""
    raw = str(args.get("path") or args.get("name") or "").strip()
    if not raw:
        return "помилка: не вказано path для read_file"
    p, label, err = _locate(ctx, raw)
    if p is None:
        return err
    att = _is_attachment(ctx, p)
    text = T.text_for_model(T.read_file(p))
    cap = limits.get().chunk if att else limits.get().read_chars
    start, limit = max(1, _int(args.get("start_line"), 1)), max(0, _int(args.get("limit")))
    if args.get("offset") and not args.get("start_line"):
        return _read_by_chars(text, label, max(0, _int(args.get("offset"))), cap, att)
    if start == 1 and not limit and len(text) <= cap:
        return f"=== {label} ===\n{text}" if att else text
    lines = text.split("\n")
    total = len(lines) - (1 if text.endswith("\n") else 0)
    if start > max(total, 1):
        return f"[{label}: у файлі лише {total} рядків]"
    out, used, i = [], 0, start - 1
    while i < total and (not limit or len(out) < limit):
        ln = lines[i]
        if out and used + len(ln) + 1 > cap:
            break
        if not out and len(ln) > cap:                       # один рядок довший за ліміт — далі читаємо за символами
            return _read_by_chars(text, label, sum(len(x) + 1 for x in lines[:i]), cap, att)
        out.append(ln)
        used += len(ln) + 1
        i += 1
    end = start - 1 + len(out)
    body = "\n".join(out)
    head = f"[{label}: рядки {start}-{end} з {total}]"
    if end >= total:
        return f"{head}\n{body}"
    left = total - end
    if att and not limit:
        tail = "\n\n…[" + CONTINUE_HINT.format(left=left, label=label, nxt=end + 1) + "]"
    elif not limit:
        # Живий кейс (Cerberus3): не маючи як дочитати файл, модель витрачала ітерації на `python -c "print(s[a:b])"`.
        tail = (f"\n\n…[файл триває (ще {left} рядків): read_file(path='{label}', start_line={end + 1}); знайти місце — "
                "search(pattern, path). НЕ копіюй вміст вручну — для дублювання у новий файл клич create_from_source, "
                "для зміни на місці — replace_in_file або edit_file (вони зберігають великі дані-літерали байт-у-байт).]")
    else:
        tail = f"\n…[далі ще {left} рядків: read_file(path='{label}', start_line={end + 1})]"
    return f"{head}\n{body}{tail}"


def h_search(args, ctx):
    from .search import search
    raw = str(args.get("path") or "").strip()
    try:
        base = _resolve(ctx, raw) if raw else ctx.root.resolve()
    except ValueError as e:
        return str(e)
    ic = args.get("ignore_case")
    ic = None if ic in (None, "") else (str(ic).lower() not in ("false", "0", "no"))
    return search(base, str(args.get("pattern") or ""), str(args.get("glob") or ""), ic, rel_to=ctx.root)


def h_read_attachment(args, ctx):
    """Старий тул read_attachment (тепер вкладення читає read_file за іменем). Лишається як псевдонім для викликів зі
    старої історії/підказок: шукає САМЕ серед вкладень."""
    if not _attachment_files(ctx):
        return "немає прикріплених файлів"
    q = str(args.get("name") or args.get("path") or "").strip()
    match = _find_attachment(ctx, q)
    if match is None:
        names = ", ".join(p.name for p in _attachment_files(ctx))
        return f"файл '{Path(q).name}' не знайдено серед вкладень. Доступні: {names}"
    a = {"path": str(match)}
    if args.get("start_line"):
        a["start_line"] = args["start_line"]
    elif args.get("offset"):
        a["offset"] = args["offset"]
    return h_read_file(a, ctx)


def h_read_topic(args, ctx):
    """Прочитати нотатку теми (chat-режим, спільна памʼять між чатами — agent.topics).
    Нечіткий пошук за іменем, як read_attachment."""
    from . import topics
    q = (args.get("name") or "").strip()
    existing = topics.list_topics()
    if not existing:
        return "немає збережених тем"
    match = next((t for t in existing if t == q), None)
    if not match and q:
        ql = q.lower()
        cand = ([t for t in existing if t.lower() == ql]
                or [t for t in existing if ql in t.lower()]
                or [t for t in existing if t.lower() in ql])
        match = cand[0] if cand else None
    if not match:
        return f"тема '{q}' не знайдена. Існуючі теми: {', '.join(existing)}"
    content = topics.load_topic(match)
    if len(content) > 8000:
        content = content[:8000] + f"\n\n…[обрізано: показано 8000 з {len(content)} символів]"
    return f"=== тема: {match} ===\n{content}"


def h_write_file(args, ctx):
    rel = (args.get("path") or "").strip()
    if not rel:
        return "помилка: не вказано path для write_file"
    p = _resolve(ctx, rel)
    if p.is_dir():
        return f"помилка: '{rel}' — це тека, а не файл"
    content = args.get("content", "")
    p.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if p.is_file():                         # перезапис: зберегти стиль відступів/кінців рядків
        content, note = T.match_style(T.read_file(p), content, p.name)
    else:
        content = T.new_file_style(p, content)
    T.backup_file(p)
    T.write_file(p, content)
    return f"записано {p.name} ({len(content)} символів)" + (f" [{note}]" if note else "")


def h_run_shell(args, ctx):
    from .shell_guard import classify
    cmd = (args.get("command") or "").strip()
    mode = ctx.permissions.get("shell", "allowlist")
    if mode == "off":
        return "консоль вимкнена (дозвіл shell=off)"
    if mode == "ask":
        if ctx.confirm and not ctx.confirm(f"Виконати: {cmd}"):
            return "команду відхилено користувачем"
        r = T.run_shell(cmd, cwd=str(ctx.root), allow_all=True, use_shell=True)
    elif mode == "smart":
        if classify(cmd) == "danger":
            if ctx.confirm and not ctx.confirm(f"⚠ Небезпечна команда:\n{cmd}"):
                return "команду відхилено користувачем"
        r = T.run_shell(cmd, cwd=str(ctx.root), allow_all=True, use_shell=True)
    elif mode == "auto":
        r = T.run_shell(cmd, cwd=str(ctx.root), allow_all=True, use_shell=True)
    else:  # allowlist
        r = T.run_shell(cmd, cwd=str(ctx.root))
        if not r.allowed:
            return f"заблоковано allowlist: {cmd}"
    from .spill import spill
    raw = r.stdout or r.stderr or f"rc={r.returncode}"
    if r.returncode not in (0, None) and r.stdout and r.stderr:
        raw = f"{r.stdout}\n[stderr]\n{r.stderr}"             # помилка команди — теж у вивід, не лише stdout
    out = spill(raw, "shell")                                  # довгий вивід: голова+хвіст у контекст, повний — у файл
    if r.returncode not in (0, None) and out != f"rc={r.returncode}":
        out += f"\n[код завершення: {r.returncode}]"      # без цього планувальник не бачить, що команда впала
    return out


def _apply_or_reject(res, path, ctx, made: str) -> str:
    if not res.ok:
        return f"помилка: {res.error}"
    if ctx.permissions.get("edits") == "auto" or (ctx.confirm and ctx.confirm(res.diff)):
        apply_edit(path, res)
        return f"{made} {Path(path).name}:\n{res.diff}"
    return f"відхилено. diff:\n{res.diff}"


def h_edit_file(args, ctx):
    p = _resolve(ctx, args.get("path", ""))
    if not p.is_file():
        return f"файл не знайдено: {args.get('path')}"
    res = run_patch(p, args.get("instruction", ""), ctx.client, False)
    return _apply_or_reject(res, p, ctx, "застосовано до")


def h_create_from_source(args, ctx):
    target = _resolve(ctx, args.get("target", ""))
    source = _resolve(ctx, args.get("source", ""))
    if not source.is_file():
        return f"джерело не знайдено: {args.get('source')}"
    res = create_from_source(target, source, args.get("instruction", ""), ctx.client)
    return _apply_or_reject(res, target, ctx, "створено")


def h_remember(args, ctx):
    from . import sessionmem
    if not ctx.session_id:
        return "нотатки недоступні: немає активної сесії"
    name = (args.get("name") or "").strip()
    text = (args.get("text") or "").strip()
    if not name or not text:
        return "потрібні name і text"
    slug = sessionmem.save_note(ctx.session_id, name, text)
    return f"нотатку збережено: {slug}" if slug else "не вдалось зберегти нотатку"


def h_recall(args, ctx):
    from . import sessionmem
    name = (args.get("name") or "").strip()
    if not ctx.session_id:
        return "нотатки недоступні: немає активної сесії"
    text = sessionmem.read_note(ctx.session_id, name) if name else None
    if text is None:
        have = ", ".join(s for s, _, _ in sessionmem.list_notes(ctx.session_id)) or "нотаток ще нема"
        return f"нотатки «{name}» нема. Є: {have}"
    return text


def h_recall_history(args, ctx):
    from . import history
    from . import session as sess
    if not ctx.session_id:
        return "історія недоступна: немає активної сесії"
    try:
        msgs = sess.load_session(ctx.session_id).messages
    except (OSError, ValueError, KeyError):
        return "історія недоступна: сесію не знайдено"
    try:
        number = int(args.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    return history.search(msgs, str(args.get("query") or ""), number or None)


def h_web_search(args, ctx):
    from . import websearch
    res = websearch.search(args.get("query", ""),
                           int(args.get("max_results", websearch.DEFAULT_MAX) or websearch.DEFAULT_MAX))
    return websearch.format_results(res)


def default_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool("list_dir", "Показати структуру файлів проєкту.",
                    {"path": {"type": "string", "description": "тека, типово корінь"}},
                    [], h_list_dir))
    r.register(Tool("read_file",
                    "Прочитати файл проєкту АБО прикріплений користувачем файл (для вкладення достатньо імені, можна "
                    "неточного). Невеликий файл повертається цілком; великий — частинами за рядками з заголовком "
                    "«рядки a-b з N» і підказкою, як читати далі. Щоб прочитати лише потрібне місце, спершу знайди його "
                    "тулом search, потім read_file(path, start_line=N, limit=40).",
                    {"path": {"type": "string", "description": "шлях до файлу або ім'я вкладення"},
                     "start_line": {"type": "integer", "description": "з якого рядка читати (з 1)"},
                     "limit": {"type": "integer", "description": "скільки рядків прочитати (типово — скільки вміститься)"}},
                    ["path"], h_read_file))
    r.register(Tool("search",
                    "Знайти рядки у файлах за текстом або регулярним виразом (замість grep/Select-String у консолі). "
                    "Повертає «шлях:рядок: текст». Без великих літер у шаблоні регістр не враховується.",
                    {"pattern": {"type": "string", "description": "текст або регулярний вираз (напр. 'def load_', 'TODO|FIXME')"},
                     "path": {"type": "string", "description": "тека чи файл, де шукати (типово — корінь проєкту)"},
                     "glob": {"type": "string", "description": "фільтр файлів, напр. '*.py' або '*.ts,*.tsx'"},
                     "ignore_case": {"type": "boolean", "description": "true/false — примусово (типово: розумний регістр)"}},
                    ["pattern"], h_search))
    r.register(Tool("project_note",
                    "Записати/оновити коротку нотатку про файл у «Карті проєкту» (навіщо файл, де що шукати). "
                    "Викликай, коли побачив, що опис файлу застарів або ти зрозумів, що він робить. Лише ПРО "
                    "файл проєкту; сам проєкт не змінюється.",
                    {"path": {"type": "string"}, "note": {"type": "string", "description": "1–2 речення"}},
                    ["path", "note"], h_project_note))
    # read_attachment зведено в read_file (вкладення читаються за іменем); назва лишається прихованим тулом для старих викликів
    r.register(Tool("read_attachment", "", {"name": {"type": "string"}}, ["name"], h_read_attachment),
               compat_for="read_file")
    r.register(Tool("write_file",
                    "Створити або перезаписати файл із заданим вмістом. "
                    "Батьківські директорії створюються АВТОМАТИЧНО — не потрібно окремо "
                    "викликати mkdir чи run_shell для створення тек.",
                    {"path": {"type": "string", "description": "шлях до файлу (не тека)"},
                     "content": {"type": "string"}},
                    ["path", "content"], h_write_file))
    r.register(Tool("run_shell",
                    "Запустити команду в консолі Windows. cwd вже встановлено в корінь "
                    "проєкту. Використовуй для: python, pip install, pytest, git, "
                    "python -m venv .venv тощо. НЕ використовуй для створення тек або "
                    "копіювання файлів — для цього є write_file і create_from_source.",
                    {"command": {"type": "string", "description": "команда для виконання"}},
                    ["command"], h_run_shell))
    r.register(Tool("edit_file",
                    "Змінити наявний файл за інструкцією: модель вносить ТОЧКОВІ правки (лише змінені "
                    "фрагменти), решта файлу не чіпається; великі дані-літерали зберігаються автоматично. "
                    "У instruction чітко вкажи ЩО й ДЕ (функція/клас/рядок) змінити.",
                    {"path": {"type": "string"}, "instruction": {"type": "string"}},
                    ["path", "instruction"], h_edit_file))
    r.register(Tool("create_from_source",
                    "Створити НОВИЙ файл як копію наявного файлу-джерела з опційним "
                    "рефакторингом. ЦЕ ЄДИНИЙ ПРАВИЛЬНИЙ спосіб скопіювати/продублювати "
                    "файл у новий: великі дані-літерали копіюються байт-у-байт. Використовуй "
                    "замість ручного read_file+write_file і замість shell-copy.",
                    {"target": {"type": "string", "description": "новий файл"},
                     "source": {"type": "string", "description": "наявний файл-джерело"},
                     "instruction": {"type": "string",
                                     "description": "що змінити; '' якщо просто копія"}},
                    ["target", "source", "instruction"], h_create_from_source))
    r.register(Tool("web_search",
                    "Пошук в інтернеті актуальної/зовнішньої інформації (документація, "
                    "помилки, новини). Повертає короткі сніпети (заголовок, URL, опис).",
                    {"query": {"type": "string", "description": "пошуковий запит"},
                     "max_results": {"type": "integer", "description": "скільки результатів, типово 5"}},
                    ["query"], h_web_search))
    r.register(Tool("read_topic",
                    "Прочитати нотатку попередньої аналітичної розмови за темою (chat-режим, "
                    "СПІЛЬНА памʼять між усіма чатами). Викликай, коли ЦЯ розмова продовжує "
                    "тему зі списку доступних тем у контексті.",
                    {"name": {"type": "string", "description": "назва теми"}},
                    ["name"], h_read_topic))
    r.register(Tool("remember",
                    "Записати коротку нотатку сесії про те, що ти з'ясував і що знадобиться пізніше (де що лежить, обмеження, "
                    "рішення). Нотатки не займають контекст — у ньому лише перелік назв. НЕ клади сюди довгі значення "
                    "(токени, ключі, великі рядки): вкажи файл, звідки їх брати.",
                    {"name": {"type": "string", "description": "коротка назва"},
                     "text": {"type": "string", "description": "суть, 1-10 рядків"}},
                    ["name", "text"], h_remember))
    r.register(Tool("recall",
                    "Прочитати нотатку сесії за назвою (перелік є в контексті; назва може бути неповною).",
                    {"name": {"type": "string"}}, ["name"], h_recall))
    r.register(Tool("recall_history",
                    "Знайти деталі в ПОВНІЙ історії цієї розмови, яку стиснуто в підсумок: точні команди, тексти помилок, "
                    "що саме просив користувач чи що ти відповідав. Викликай, коли в підсумку бракує подробиць, замість "
                    "вгадувати. query — ключові слова (імена файлів, фрагмент помилки); number — прочитати повністю "
                    "повідомлення №N з результатів пошуку; без параметрів — останні репліки.",
                    {"query": {"type": "string", "description": "ключові слова для пошуку"},
                     "number": {"type": "integer", "description": "номер повідомлення з попереднього пошуку"}},
                    [], h_recall_history))
    from .subagent import INVESTIGATE_TOOL      # лениво: subagent імпортує toolkit
    r.register(INVESTIGATE_TOOL)
    return r
