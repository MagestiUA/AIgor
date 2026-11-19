"""Per-step tool-loop: дає моделі інструменти й крутить цикл на ОДИН крок плану.

Модель сама вирішує, які тули викликати; ми виконуємо й повертаємо результат, поки
вона не завершить крок (без виклику тула) або не вичерпає ліміт ітерацій. Контекст
вузький (один крок), цикл короткий — безпечно для слабкої локальної моделі.
"""
from __future__ import annotations

import json
import re

from . import config
from . import limits
from . import logs
from .loopguard import (CHECKPOINT_PREFIX, CONTEXT_FULL_PREFIX, STUCK_PREFIX, WARN_TEXT, LoopGuard, context_full)
from .llm import OllamaClient
from .toolkit import ToolContext, ToolRegistry, default_registry

ITER_LIMIT_MSG = "досягнуто ліміту ітерацій"   # крок НЕ завершено — не вважати «done»


CHECKPOINT_EVERY = 10     # кожні стільки дій (викликів тулів) виконавець звітує планувальнику
COMPACT_RATIO = 0.6        # з якої частки вікна починаємо стискати старі результати тулів
COMPACT_KEEP = 6           # скільки останніх повідомлень (хвіст) не чіпаємо
COMPACT_HEAD = 500         # скільки символів лишаємо від старого результату тулу


def compact_tool_results(messages: list[dict], keep_last: int = COMPACT_KEEP, head: int = COMPACT_HEAD) -> int:
    """Стиснути СТАРІ результати тулів у циклі кроку (на місці): лишити початок + позначку.
    Дії вже виконано, а їх підсумок бачив виконавець; без цього контекст кроку росте до 90% вікна й крок
    переривається. -> скільки повідомлень стиснуто."""
    n = 0
    for m in messages[:max(0, len(messages) - keep_last)]:
        c = m.get("content")
        if m.get("role") == "tool" and isinstance(c, str) and len(c) > head + 80 and "[стиснуто" not in c[-60:]:
            m["content"] = c[:head] + f"\n…[стиснуто: було {len(c)} символів; повторіть виклик, якщо потрібно]"
            n += 1
    return n


READ_ONLY_TOOLS = ("list_dir", "read_file", "read_attachment", "web_search", "project_note", "investigate")
READ_WARN = 8              # стільки читань ПІДРЯД (без жодної зміни/команди) → просимо дати висновок
READ_FORCE = 12            # ... а стільки → примусовий висновок без тулів
FINALIZE_TEXT = (
    "Досить збирати інформацію. Дай ВИСНОВОК за тим, що вже знайдено: відповідь на завдання кроку, точні шляхи "
    "файлів і назви функцій. НЕ викликай інструменти. Якщо чогось не вистачило — так і скажи, що саме."
)
READ_NUDGE = ("Ти вже прочитав багато файлів поспіль. Якщо інформації достатньо — дай висновок зараз, без нових "
              "викликів. Читай далі лише те, чого справді бракує для відповіді.")


class ReadStreak:
    """Слідкує за «читанням без дій»: слабка модель читає файл за файлом і не переходить до висновку.
    Кроки, що вже щось змінювали чи запускали (будь-який не-читальний тул), не чіпаємо — ними опікується планувальник."""

    def __init__(self) -> None:
        self.streak = 0
        self.acted = False
        self.warned = False

    def observe(self, name: str) -> None:
        if name in READ_ONLY_TOOLS:
            self.streak += 1
        else:
            self.streak = 0
            self.acted = True

    def verdict(self) -> str:
        """"" | "warn" | "force"."""
        if self.acted:
            return ""
        if self.streak >= READ_FORCE:
            return "force"
        if self.streak >= READ_WARN and not self.warned:
            self.warned = True
            return "warn"
        return ""


def finalize(messages: list[dict], client) -> str:
    """Останній виклик БЕЗ тулів: примусовий висновок із зібраного. Порожньо, якщо модель нічого не дала."""
    msgs = list(messages) + [{"role": "user", "content": FINALIZE_TEXT}]
    try:
        out = (client.chat(msgs, profile=config.EXECUTOR).get("content") or "").strip()
    except Exception as e:
        logs.get("agent").warning("примусовий висновок не вдався: %s", e)
        return ""
    return out


_RESEARCH = re.compile(r"\b(знайди|з'ясуй|дізнайся|досліди|дослідж\w*|проаналізуй|огляни|переглянь|поясни|"
                       r"як працює|де в проєкті|де знаходиться|find|investigate|explore|analy[sz]e|explain)\b", re.I)
_CHANGE = re.compile(r"\b(виправ\w*|зміни|змінити|додай|додати|створи|створити|напиши|написати|видали|видалити|замін\w+|"
                     r"реалізуй|онови|оновити|перейменуй|запусти|встанови|fix|edit|create|write|implement|add|remove|"
                     r"run|install|delete|rename)\b", re.I)


def is_research_step(step_text: str) -> bool:
    """Крок суто «дізнатись/знайти» (без змін і команд): його варто віддати субагентові кодом, а не покладатись на
    те, що слабка модель сама делегує (живий тест: вона викликає investigate, а потім усе одно перечитує файли)."""
    t = step_text or ""
    return bool(_RESEARCH.search(t)) and not _CHANGE.search(t)


def research_report(step_text: str, context: str, ctx, registry) -> str:
    """Запустити субагента на дослідницькому кроці й повернути блок для промпту виконавця ('' якщо не застосовно)."""
    if "investigate" not in registry.names() or not is_research_step(step_text):
        return ""
    from .subagent import investigate
    task = step_text + (f"\n\nКонтекст: {context[:1500]}" if context else "")
    try:
        rep = investigate(task, ctx)
    except Exception as e:
        logs.get("agent").warning("авто-делегування субагенту не вдалось: %s", e)
        return ""
    return ("\n\nЗВІТ СУБАГЕНТА-ДОСЛІДНИКА (він уже прочитав потрібні файли; довіряй йому й НЕ перечитуй ті самі "
            "файли — відповідай на крок за цим звітом, читай лише точкові рядки, якщо чогось справді бракує):\n" + rep)


def is_incomplete(final: str | None) -> bool:
    """Крок НЕ завершено (ліміт / зациклення / переповнений контекст) — не вважати «done»."""
    f = final or ""
    return f == ITER_LIMIT_MSG or f.startswith(STUCK_PREFIX) or f.startswith(CONTEXT_FULL_PREFIX) or f.startswith(CHECKPOINT_PREFIX)

SYSTEM = (
    "You are an execution agent for ONE task step on a Windows machine (PowerShell). "
    "Working directory (project root) is provided in context — all relative paths are "
    "relative to it; run_shell cwd is already set to it.\n\n"
    "You have tools — pick the RIGHT one:\n"
    "- write_file(path, content): create a brand-new file. PARENT DIRECTORIES ARE CREATED "
    "AUTOMATICALLY — never call mkdir or run_shell just to create a folder. Pass a file "
    "path (not a folder). Example: write_file('src/utils/helper.py', '...').\n"
    "- edit_file(path, instruction): refactor/change an existing file in place.\n"
    "- create_from_source(target, source, instruction): duplicate an existing file into a "
    "new file (copy + optional refactor). Copies large data literals byte-for-byte. NEVER "
    "use shell copy or read+write manually for this.\n"
    "- read_file(path), list_dir(path): inspect files. list_dir with no path shows project root.\n"
    "- run_shell(command): run programs — python, pip, pytest, git, etc. cwd=project root. "
    "Use for: 'python -m venv .venv', 'pip install -r requirements.txt', 'pytest', etc. "
    "For venv pip use: '.venv\\Scripts\\pip install ...' (Windows) or '.venv/bin/pip install ...' (Linux). "
    "Do NOT use run_shell to create directories or copy files — use write_file instead.\n"
    "run_shell runs Windows PowerShell 5.1 with stdin closed (interactive prompts fail "
    "instead of hanging): chain commands with a semicolon (`&&` between commands is auto-translated), "
    "use SINGLE quotes for literal strings (double quotes expand $variables; a double quote "
    "INSIDE a single-quoted argument must be escaped as \\\" or it is lost), pass ssh/adb "
    "remote commands as one quoted string, use ssh -o BatchMode=yes, and never run endless "
    "commands (adb logcat without -d, tail -f).\n"
    "- web_search(query): look up docs, errors, external info.\n"
    "- investigate(task): delegate READ-ONLY research (many files, logs, unfamiliar code) to a sub-agent with its "
    "own clean context; you get back only a short conclusion. Use it BEFORE editing when understanding needs "
    "reading more than ~3 files. It changes nothing - you do the edits.\n"
    "- project_note(path, note): update the project map note about a file when its description is "
    "outdated (it does not change the project).\n\n"
    "When changing an EXISTING file PRESERVE its style: indentation (tabs vs spaces), line "
    "endings and quote style; do not reformat code you were not asked to change. Prefer "
    "edit_file for targeted changes; rewrite a whole file with write_file only when you "
    "must, and then keep every untouched line byte-for-byte as it was.\n"
    "If a run_shell command FAILS, read the error and issue a CORRECTED command — do "
    "NOT repeat the same failing command. (E.g. git: stage with 'git add -A', then "
    "commit with 'git commit -m \"message\"' — '-m' belongs to commit, NOT to add.)\n"
    "NEVER invent data, lists, or mechanisms that aren't shown in your context (e.g. a "
    "hardcoded list of app names/IDs when the project already has a search/lookup "
    "mechanism for that). If the step implies an existing project mechanism should be "
    "reused, use read_file/list_dir FIRST to find and use the real one — do not fall "
    "back to a made-up substitute.\n"
    "When the step is done, reply with a SHORT confirmation and NO tool call. "
    "Reply in the user's language."
)


_ESTIMATE_SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}
_ESTIMATE_SYSTEM = (
    "You are a planning assistant. Given a task step description, estimate the minimum "
    "number of tool calls needed to complete it (write_file, run_shell, read_file, etc.). "
    "Count each file creation/edit as 1, each shell command as 1. Reply ONLY as JSON: {\"n\": <integer>}."
)


def _estimate_iters(step_text: str, context: str, client: OllamaClient,
                    profile: dict = config.EXECUTOR) -> int:
    """Спитати модель скільки тул-викликів потрібно для кроку → повернути n*2 (мін 6).
    profile: chat-режим передає CHAT_EXECUTOR (gemma)."""
    user = (f"Context:\n{context}\n\n" if context else "") + f"Step:\n{step_text}"
    try:
        msg = client.chat(
            [{"role": "system", "content": _ESTIMATE_SYSTEM},
             {"role": "user", "content": user}],
            profile=profile, fmt=_ESTIMATE_SCHEMA,
        )
        n = json.loads(msg.get("content") or "{}").get("n", 6)
        # Запас на самокорекцію: слабка модель часто витрачає 2-3 ітерації на невдалі
        # спроби (напр. плутає `git add -m` з `git commit -m`), тож множник і підлога
        # вищі — інакше крок упирається в ліміт ще до завершення (як було з commit).
        return max(10, int(n) * 3)
    except Exception:
        return 15


# Деякі моделі (помічено на qwen3-coder) під складеними інструкціями плутають тег
# самого виклику (<tool_call>) із тегом опису сигнатур (<tools>) і виводять спробу
# викликати тул як звичайний текст замість structured tool_calls. Без цієї перевірки
# крок "тихо" завершується без жодної дії (calls=[] виглядає як "модель закінчила").
_LEAKED_CALL_RE = re.compile(r'<tool_call>|<tools>|"arguments"\s*:\s*\{', re.IGNORECASE)


def _looks_like_leaked_tool_call(content: str) -> bool:
    return bool(content) and bool(_LEAKED_CALL_RE.search(content))


# Живий кейс (gemma4, gather-фаза _run_tool_chat): модель пише НАРАТИВНО, що
# збирається викликати тул ("I will call read_attachment for all 6 files
# first.") — без structured tool_calls і без жодної JSON/XML-структури (тож
# _looks_like_leaked_tool_call це не ловить) — і просто ЗУПИНЯЄТЬСЯ, нічого
# не виконавши. Без цієї перевірки should_nudge бачить непорожній, "не
# просочений" текст і вважає крок валідно завершеним — користувач бачить
# "нічого не відбулось" (нуль тул-викликів, нуль відповіді по суті).
_INTENT_PHRASE_RE = re.compile(
    r"\b(?:I will|I['’]ll|I need to|I am going to|I['’]m going to|Let me)\s+call\b"
    r"|спочатку\s+(?:виклич\w*|прочита\w*)"
    r"|виклич\w*\s+(?:тул|read_|list_|run_|write_|edit_|web_)",
    re.IGNORECASE,
)
_TOOL_IDENT_RE = re.compile(r'\b[a-z]+_[a-z_]+\b')


def _announces_tool_intent(content: str) -> bool:
    """True, якщо текст ОГОЛОШУЄ намір викликати тул (фраза-намір + згадка
    snake_case ідентифікатора тула), але не є фактичним structured/просоченим
    викликом — отже нічого насправді не станеться, якщо прийняти це як кінець кроку."""
    return (bool(content) and bool(_INTENT_PHRASE_RE.search(content))
            and bool(_TOOL_IDENT_RE.search(content)))


def extract_intended_attachments(content: str, known_names: list[str]) -> list[str]:
    """Живий кейс (gather-фаза _run_tool_chat, важка багатофайлова задача):
    should_nudge дає лише ОДИН nudge на всю фазу збору (щоб не зациклитись), але
    слабка модель стабільно повторює саме _announces_tool_intent навіть ПІСЛЯ
    нього — другий раз should_nudge мовчить (already_nudged), цикл просто
    `break`-ається БЕЗ жодних прочитаних даних -> "(порожня відповідь)" у фіналі.

    Текстовий nudge тут принципово ненадійний (та сама причина, що й
    pending_continuation для пагінації) — модель отримала реальні імена файлів
    через attachments_note і просто ЦИТУЄ їх у своєму "I will call..." реченні.
    Раз імена вже відомі (known_names — фактичні файли на диску) — виконуємо
    намір моделі НАПРЯМУ: повертає підмножину known_names, згадану в content
    (порядок як у known_names), або весь known_names, якщо жодне ім'я текстуально
    не співпало (типовий випадок — "усіх N файлів" без перерахування)."""
    if not known_names:
        return []
    mentioned = [n for n in known_names if n in content]
    return mentioned or list(known_names)


def _extract_leaked_tool_call(content: str) -> dict | None:
    """Розпарсити просочений виклик тула з тексту (XML-теги чи голий JSON) у
    справжній tool_call dict — і ВИКОНАТИ дію одразу, замість ще одного nudge-раунду.
    Живий кейс: модель повторила ТОЙ САМИЙ просочений формат і після nudge (2/2),
    включно з малформованим зайвим '}' наприкінці — звичайний retry тут не зарадить,
    бо модель стабільно ламає формат саме так. Парсинг стійкий до:
    - обгортки <tool_call>/<tools> (чи без неї — голий JSON);
    - зайвих символів/дужок ПІСЛЯ валідного JSON-обʼєкта (рахуємо глибину дужок і
      зупиняємось на першому балансі, ігноруючи хвіст)."""
    if not content:
        return None
    text = re.sub(r'</?tool_call>|</?tools>', '', content, flags=re.IGNORECASE).strip()
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    end = None
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        obj = json.loads(text[start:end])
    except Exception:
        return None
    name = obj.get("name")
    args = obj.get("arguments", {})
    if not isinstance(name, str) or not name or not isinstance(args, dict):
        return None
    return {"function": {"name": name, "arguments": args}}


def recover_tool_calls(msg: dict) -> tuple[list[dict], bool]:
    """Повертає (tool_calls, recovered). Якщо модель віддала справжні structured
    tool_calls — повертає їх як є (recovered=False). Якщо ні, але контент містить
    розпарсюваний просочений виклик — повертає його як один tool_call
    (recovered=True), щоб виконати ту саму дію без додаткового раунду."""
    calls = msg.get("tool_calls") or []
    if calls:
        return calls, False
    if not limits.get().weak_model_fixes:
        return [], False   # сильна модель: текст лишається текстом (див. CRUTCHES.md A1)
    leaked = _extract_leaked_tool_call((msg.get("content") or "").strip())
    return ([leaked], True) if leaked else ([], False)


# Спільна для УСІХ tool-loop'ів (agent_loop.run_step, lca_web._run_tool_step,
# lca_web._run_tool_chat) перевірка "чи варто підштовхнути модель ще раз замість
# того, щоб прийняти цю відповідь як фінальну". Раніше кожен loop мав свою копію
# цієї перевірки — фікс одного й того ж бага (порожня/просочена відповідь без
# tool_calls тихо приймається як "крок завершено") довелось вносити в трьох місцях
# окремо. Differences між циклами (sync/async виклик моделі, continue циклу чи
# перехід в іншу фазу) лишаються в кожному місці — тут лише ОДНЕ рішення + текст.
NUDGE_TEXT = (
    "Виклич потрібний інструмент як справжній tool/function call (structured), НЕ "
    "як текст і НЕ в XML-тегах у відповіді."
)


def should_nudge(content: str, already_nudged: bool) -> bool:
    """True, якщо модель не виконала дію (content порожній, або спроба виклику тула
    просочилась як текст замість structured tool_calls — плутанина тегів
    <tool_call>/<tools>, бачено на qwen3-coder) і ще НЕ нюджили цього разу (рівно
    один nudge — щоб не зациклитись, якщо модель стабільно ламає формат)."""
    if already_nudged:
        return False
    if not content:
        return True
    return limits.get().weak_model_fixes and (
        _looks_like_leaked_tool_call(content) or _announces_tool_intent(content))


# Живий кейс: gemma4 (think=on) інколи НЕ розділяє content/thinking сама — Ollama
# довіряє цьому розділенню моделі (llm._stream_event лише читає вже готові поля
# message.content/message.thinking), а модель замість того запихає весь монолог
# роздумів (англомовний self-talk, часто у фенс-блоці) прямо в content. На відміну
# від should_nudge (порожній/просочений tool_call), тут content НЕ порожній — viewable
# текст просто є хаосом роздумів замість відповіді. Маркери — англійські службові
# фрази, які НЕ зʼявляються в нормальній українській відповіді користувачу.
_LEAK_REASONING_MARKERS = ("Wait,", "*Wait*", "Self-Correction", "*Actually*",
                            "*Correction:*", "Let me re-examine", "I need to re-examine")


def _looks_like_leaked_reasoning(body: str) -> bool:
    """3+ маркери self-talk у видимій відповіді -> модель просочила роздуми в content
    замість чистої відповіді (а не просто раз згадала слово "wait" в іншому контексті)."""
    return sum(body.count(m) for m in _LEAK_REASONING_MARKERS) >= 3


# Останній read_attachment повертає явну підказку "лишилось N символів, виклич
# read_attachment(name='X', offset=N)" (toolkit.h_read_attachment). Якщо модель після
# цього НЕ зробила той виклик, а написала прозою щось типу "продовжую читати" — текстовий
# nudge тут НЕ зарадить: живцем бачили модель, що 15+ разів підряд повторює ТОЧНО ту саму
# фразу, ігноруючи nudge. Маємо вже все потрібне (ім'я файлу + offset) з власної ж
# підказки — тож ВИКОНУЄМО продовження самі, без участі моделі: гарантований прогрес
# замість сподівання на співпрацю.
_PENDING_CONTINUATION_RE = re.compile(r"read_attachment\(name='([^']+)',\s*offset=(\d+)\)\]\s*$")


def pending_continuation(messages: list[dict]) -> dict | None:
    """Якщо останній РЕАЛЬНИЙ tool-результат в історії містить незавершену пагінацію
    read_attachment — повертає синтетичний tool_call (той самий формат, що й
    recover_tool_calls) для автопродовження. Йдемо назад, пропускаючи власні
    nudge-повідомлення ('user') — інакше після ПЕРШОГО nudge перевірка вже не бачить
    tool-результат і мовчки здається."""
    for m in reversed(messages):
        role = m.get("role")
        if role == "tool":
            match = _PENDING_CONTINUATION_RE.search((m.get("content") or "").strip())
            if not match:
                return None
            return {"function": {"name": "read_attachment",
                                 "arguments": {"name": match.group(1), "offset": int(match.group(2))}}}
        if role == "user":
            continue   # пропускаємо власні nudge-репліки, шукаємо далі назад
        return None   # assistant (реальний виклик уже відбувся) чи system — не pending
    return None


def _args(call: dict) -> dict:
    a = call.get("function", {}).get("arguments", {})
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            a = {}
    return a or {}


def run_step(step_text: str, ctx: ToolContext, client: OllamaClient | None = None,
             registry: ToolRegistry | None = None, context: str = "",
             max_iters: int | None = None, on_tool=None, stats_sink: list | None = None,
             stop_event=None) -> tuple[str, list[str]]:
    """Виконати один крок через tool-loop.
    max_iters=None → авто-оцінка моделлю (n*2); явне число скасовує оцінку.
    on_tool(name, args, result) — необовʼязковий колбек для UI-журналу.
    stats_sink — якщо передано, у нього додаються client.last_stats кожного виклику
    моделі (для лічильника токенів кроку).
    stop_event: threading.Event — якщо встановлено, цикл переривається між
    ітераціями (поточний частковий результат повертається).
    Повертає (фінальний_текст, журнал_дій)."""
    client = client or OllamaClient()
    registry = registry or default_registry()
    if max_iters is None:
        # Слабка локальна модель: ліміт за оцінкою. Сильна (сервер): довгу роботу НЕ обмежуємо —
        # стежимо за прогресом (LoopGuard), а hard_iters — лише запобіжник.
        max_iters = limits.get().hard_iters or _estimate_iters(step_text, context, client)
    guard = LoopGuard()
    user = (f"Контекст:\n{context}\n\n" if context else "") + f"Крок:\n{step_text}"
    user += research_report(step_text, context, ctx, registry)      # дослідницький крок → субагент кодом
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]
    log: list[str] = []
    nudged = False
    reads = ReadStreak()

    for _ in range(max_iters):
        if stop_event is not None and stop_event.is_set():
            return "зупинено користувачем", log
        msg = client.chat(messages, tools=registry.schema(), profile=config.EXECUTOR)
        if stats_sink is not None and client.last_stats:
            stats_sink.append(dict(client.last_stats))
        calls, recovered = recover_tool_calls(msg)
        if not calls:
            content = (msg.get("content") or "").strip()
            # Великий файл дочитується частинами (read_attachment offset) — якщо
            # лишився непрочитаний хвіст, а модель не продовжила виклик, ДОЧИТУЄМО
            # САМІ (текстовий nudge тут ненадійний — бачили живцем 15+ ідентичних
            # повторів "продовжую читати" без жодної дії). Див. pending_continuation.
            cont = pending_continuation(messages)
            if cont is not None:
                calls, recovered = [cont], True
            # НЕ повертаємо зламаний/порожній текст назад як assistant-повідомлення —
            # інакше модель копіює власну попередню відповідь і застрягає в тому
            # самому виводі навіть при повторі (перевірено живцем). Див. should_nudge.
            elif should_nudge(content, nudged):
                nudged = True
                logs.get("agent").info("nudge: порожня/просочена відповідь без tool_calls")
                messages.append({"role": "user", "content": NUDGE_TEXT})
                continue
            else:
                return content, log

        # recovered=True: викликали тул з розпарсеного просоченого тексту — НЕ ехо-имо
        # сам зламаний текст назад (той самий анти-анкоринг, що й для nudge).
        messages.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""),
                         "tool_calls": calls})
        for call in calls:
            name = call.get("function", {}).get("name", "")
            args = _args(call)
            result = registry.dispatch(name, args, ctx)
            log.append(f"{name}({', '.join(args)}) -> {result[:80]}")
            if on_tool:
                on_tool(name, args, result)
            messages.append({"role": "tool", "content": result})
            guard.observe(name, args, result)
            reads.observe(name)
        verdict = guard.check()
        if verdict:
            kind, reason = verdict
            logs.get("agent").warning("зациклення (%s): %s | останній результат: %s", kind, reason,
                                      guard.last_result[:120].replace(chr(10), " "))
            if kind == "stop":
                return guard.stuck_message(reason), log
            messages.append({"role": "user", "content": WARN_TEXT.format(reason=reason, last=guard.last_result[:300])})
        rv = reads.verdict()
        if rv == "force":
            out = finalize(messages, client)
            if out:
                return out, log
        elif rv == "warn":
            messages.append({"role": "user", "content": READ_NUDGE})
        ls = getattr(client, "last_stats", None) or {}
        win = limits.get().context_tokens
        if ls and win and ls.get("prompt", 0) >= COMPACT_RATIO * win:
            freed = compact_tool_results(messages)
            if freed and context_full(ls.get("prompt", 0), win):
                continue                                  # стиснули — даємо кроку шанс завершитись
        if ls and context_full(ls.get("prompt", 0), limits.get().context_tokens):
            logs.get("agent").warning("контекст майже заповнений: %s токенів", ls.get("prompt"))
            if log and not reads.acted:                # лише читали — віддаємо висновок із зібраного (після стиснення)
                compact_tool_results(messages, keep_last=2, head=300)
                out = finalize(messages, client)
                if out:
                    return out, log
            return (f"{CONTEXT_FULL_PREFIX} ({ls.get('prompt')} токенів). "
                    "Розбийте задачу на менші кроки."), log

    logs.get("agent").warning("крок досяг ліміту ітерацій (%d): %s", max_iters, step_text[:80])
    if log and not reads.acted:                    # лише читали: замість «невдачі» просимо висновок зі зібраного
        out = finalize(messages, client)
        if out:
            return out, log
    return ITER_LIMIT_MSG, log
