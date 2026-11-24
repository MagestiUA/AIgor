"""Контекст-памʼять розмови — компактний підсумок зробленого, що подається в
наступні запити (передусім планувальнику), плюс ДОСЛІВНИЙ хвіст свіжих реплік.
Підсумок оновлюється «ліниво» (compact_tail): лише коли хвіст переріс межу, його
найстаріші репліки стискаються одним викликом моделі; коли переростає бюджет сам
підсумок — стискаємо його (LLM). Повна розмова лишається у файлі сесії (recall_history).

Бюджети — limits.get() (summary, digest), залежать від вікна моделі.

Два РІЗНІ профілі стиснення:
  code/answer/shell-режими — `compact_tail`/`compress_summary` (kind="code"):
    орієнтовані на файли/функції/рішення — нерелевантно для аналітичних розмов.
  chat-режим — ті самі функції з kind="chat": фокус на тезах, фактах, ланцюжках
    подій/причин-наслідків розмови, без коду-специфічних рамок.

Окремо — `update_digest`: для ВЕЛИКИХ вкладень (більших за limits.inline), що читаються
частинами. Замість накопичення сирих шматків у контексті кожен прочитаний шматок
зливається в один дайджест, зберігаючи деталі й ланцюжки подій; сам файл лишається
на диску й читається тулом без втрат.
"""
from __future__ import annotations

import re

from . import config
from . import limits
from . import logs
from .llm import OllamaClient

def notify_compress(on_compress, what: str, before: int, after: int) -> None:
    """Повідомити, що відбулось СТИСНЕННЯ контексту (побажання розробників: Ігорь має
    казати про це): пишемо в лог і викликаємо колбек (UI показує нотатку в чаті)."""
    logs.get("context").info("стиснення: %s %d -> %d симв.", what, before, after)
    if on_compress:
        try:
            on_compress(what, before, after)
        except Exception:
            logs.get("context").exception("колбек стиснення впав")


BUDGET_CHARS = 32000          # ~10k токенів
COMPRESS_TO = BUDGET_CHARS // 2

_EPISODE_SYSTEM = {
    "code": (
        "You compress ONE turn of a coding session into 1-3 short factual bullet lines for "
        "future context. State concretely: which files were created/changed, which "
        "functions/classes were added or modified, key decisions, and what the user asked. "
        "Be terse — no fluff, no restating the whole code. Same language as the content."
    ),
    "chat": (
        "You compress ONE turn of an analytical discussion into factual bullet lines for "
        "future context. State concretely: what topic/claim was discussed, key facts and "
        "numbers mentioned, conclusions or open questions raised, and what the user asked. "
        "Preserve cause-and-effect / event chains, not just isolated facts — don't reduce a "
        "complex turn to 1-3 lines if it covered several distinct topics; use as many bullet "
        "lines as the content actually needs. Be terse but DON'T drop concrete details. Same "
        "language as the content."
    ),
}

_COMPRESS_SYSTEM = {
    "code": (
        "You condense a running summary of a coding session. KEEP every concrete fact: files "
        "changed/created, functions/classes added or modified, decisions made, and any open "
        "threads. Drop redundancy and small talk. Make it about half as long. Same language."
    ),
    "chat": (
        "You condense a running summary of an analytical discussion. KEEP every concrete fact, "
        "number, claim, conclusion, and open question — and KEEP cause-and-effect/event chains "
        "intact (don't flatten them into disconnected bullet points). Drop only redundancy and "
        "small talk. Make it about half as long. Same language."
    ),
}


# Профіль виклику для стиснення (модель одна на всі ролі — різняться лише промпти code/chat).
def _profile_for(kind: str) -> dict:
    return config.CHAT_EXECUTOR if kind == "chat" else config.EXECUTOR


_TITLE_SYSTEM = (
    "Give a short, specific title (3-6 words, no quotes, no trailing punctuation) for a chat "
    "that starts with the following user message. Same language as the message. Reply with "
    "ONLY the title, nothing else — no explanations, no labels."
)


def generate_title(user_text: str, client: OllamaClient | None = None, kind: str = "code") -> str:
    """Коротка осмислена назва чату (3-6 слів) за першим повідомленням — заміна
    дефолтного "Новий чат". think=off (_profile_for), без тулів, один дешевий
    виклик. Не критичний механізм — якщо модель видала сміття/echo/задовгий
    текст, повертаємо "" (caller лишає дефолтну назву, без retry)."""
    client = client or OllamaClient()
    msg = client.chat(
        [{"role": "system", "content": _TITLE_SYSTEM},
         {"role": "user", "content": user_text[:2000]}],
        profile=_profile_for(kind),
    )
    title = _strip_echoed_label((msg.get("content") or "").strip(), "Title", "Назва", "Назва чату")
    title = title.strip(" \"'`*#").splitlines()[0].strip() if title else ""
    if not title or _is_empty_echo(title) or len(title) > 80:
        return ""
    return title


def compress_summary(summary: str, client: OllamaClient | None = None, kind: str = "code") -> str:
    """Стиснути переповнений підсумок, зберігши конкретні факти. kind="code"|"chat"."""
    client = client or OllamaClient()
    msg = client.chat(
        [{"role": "system", "content": _COMPRESS_SYSTEM[kind]}, {"role": "user", "content": summary}],
        profile=_profile_for(kind),
    )
    return (msg.get("content") or "").strip()


def as_context(summary: str) -> str:
    """Блок підсумку для підстановки в запит (порожній рядок, якщо нема)."""
    if not summary:
        return ""
    from .history import HINT        # модель має ЗНАТИ, що деталі стисненої розмови можна дістати тулом
    return f"Контекст попередньої розмови (підсумок зробленого):\n{summary}\n({HINT})\n\n"


# ── «Лінивий» підсумок: останні репліки дослівно, стискаємо лише те, що випадає з вікна ─────────────────────────
# Раніше після КОЖНОГО запиту йшов окремий виклик моделі (summarize_turn), навіть коли вікно майже порожнє, а свіжі
# репліки потрапляли в контекст уже переказаними. Тепер (як автостиснення в Claude Code): підсумок покриває повідомлення
# до Session.summary_upto, усе після — «хвіст», що йде в контекст ДОСЛІВНО (dialogue_tail). Коли хвіст переростає межу
# (кількість/символи), найстаріші його репліки стискаються ОДНИМ викликом (compact_tail), а свіжі лишаються дослівно.
TAIL_KINDS = ("text", "answer", "plan", "step", "note")
NOTE_PREFIXES = ("⛔", "⚠", "💭")          # примітки, що несуть факт (невдача кроку, звірка, рішення планувальника)
PER_MSG, PER_STEP, PER_NOTE = 2500, 800, 300
TAIL_MAX, TAIL_KEEP = 12, 6                # код-режим: хвіст до 12 змістовних повідомлень; при стисненні лишаємо 6 свіжих


def _tail_line(m: dict) -> str:
    kind, c = m.get("kind", "text"), (m.get("content") or "").strip()
    if kind == "step":
        return "Крок виконано: " + (c if len(c) <= PER_STEP else c[:PER_STEP] + "…")
    if kind == "note":
        return "Примітка: " + c[:PER_NOTE]
    who = "Користувач" if m.get("role") == "user" else "Асистент"
    return f"{who}: {c if len(c) <= PER_MSG else c[:PER_MSG] + '…'}"


def tail_items(messages: list[dict], start: int = 0, kinds: tuple = TAIL_KINDS) -> list[tuple[int, dict]]:
    """Змістовні повідомлення з позиції start: репліки, плани, результати кроків, примітки-факти (kinds — які типи)."""
    out = []
    for i, m in enumerate(messages):
        if i < start or m.get("role") not in ("user", "assistant") or not (m.get("content") or "").strip():
            continue
        kind = m.get("kind", "text")
        if kind not in kinds or (kind == "note" and not m["content"].lstrip().startswith(NOTE_PREFIXES)):
            continue
        out.append((i, m))
    return out


def dialogue_tail(messages: list[dict], start: int = 0, skip_current: bool = True) -> str:
    """Дослівний хвіст розмови після підсумку (для контексту запиту). skip_current — без поточного повідомлення
    користувача (воно йде окремо як задача)."""
    msgs = list(messages)
    if skip_current:
        while msgs and msgs[-1].get("role") == "user":
            msgs.pop()
    items = tail_items(msgs, start)
    if not items:
        return ""
    return ("Недавня розмова дослівно (після підсумку; для розуміння поточного повідомлення):\n"
            + "\n\n".join(_tail_line(m) for _, m in items) + "\n\n")


_SPAN_SYSTEM = {
    "code": (
        "You compress SEVERAL consecutive turns of a coding session (user requests, assistant answers, executed steps) "
        "into short factual bullet lines for future context: which files were created/changed, functions/classes "
        "added or modified, key decisions, results of checks/tests, what the user asked, open threads. Be terse — no "
        "fluff, no restating code. Same language as the content."
    ),
    "chat": _EPISODE_SYSTEM["chat"].replace("ONE turn", "SEVERAL consecutive turns"),
}


def compact_tail(summary: str, messages: list[dict], upto: int, client, *, max_msgs: int, max_chars: int,
                 keep: int, budget: int, kind: str = "code", on_compress=None,
                 kinds: tuple = TAIL_KINDS) -> tuple[str, int]:
    """Якщо дослівний хвіст (після upto) задовгий — стиснути його найстаріші репліки (лишивши keep свіжих) ОДНИМ
    викликом і дописати в підсумок; підсумок понад budget — стиснути. Інакше — нічого не робити (без виклику моделі).
    -> (новий підсумок, новий upto). Збій/порожня відповідь моделі — нічого не змінюємо (спробуємо наступного разу)."""
    items = tail_items(messages, upto, kinds)
    chars = sum(len(_tail_line(m)) for _, m in items)
    if len(items) <= max_msgs and chars <= max_chars:
        return summary, upto
    drop = items[:-keep] if keep and len(items) > keep else (items if not keep else items[:1])
    if not drop:
        return summary, upto
    text = "\n\n".join(_tail_line(m) for _, m in drop)
    if len(text) > 60_000:                                  # сам запит стискача мусить вміститись
        text = text[:30_000] + "\n…\n" + text[-30_000:]
    client = client or OllamaClient()
    try:
        msg = client.chat([{"role": "system", "content": _SPAN_SYSTEM.get(kind, _SPAN_SYSTEM["code"])},
                           {"role": "user", "content": text}], profile=_profile_for(kind))
        ep = (msg.get("content") or "").strip()
    except Exception:                                       # noqa: BLE001 — підсумок не критичний
        logs.get("context").exception("стиснення хвоста розмови не вдалось")
        return summary, upto
    if not ep:
        return summary, upto
    new = (summary + "\n- " + ep).strip() if summary else "- " + ep
    if len(new) > budget:
        before = len(new)
        new = compress_summary(new, client, kind=kind) or new
        notify_compress(on_compress, "підсумок розмови", before, len(new))
    return new, drop[-1][0] + 1


# ── Дайджест великого вкладення (читання частинами) ─────────────────────────
DIGEST_BUDGET_CHARS = 96_000   # ~30k токенів (евристика ~3.2 симв./токен)

_DIGEST_SYSTEM = (
    "You maintain a running digest of a long document being read in sequential chunks. "
    "You get the CURRENT digest (empty on the first chunk) and the NEXT raw chunk of the "
    "document. Merge the chunk's content into the digest. Preserve concrete facts, names, "
    "numbers, claims, and ESPECIALLY chains of events / cause-and-effect / topic threads — "
    "don't just append a flat bullet list, keep the connective reasoning that links facts "
    "together. Drop only redundancy and filler. Output ONLY the updated digest (no preamble, "
    "no commentary about the task), same language as the document."
)


# Слабка модель часто ЛУНАЄ заголовок нашого ж промпту ("Поточний дайджест:",
# "Поточна нотатка:") на початку відповіді замість чистого тексту — бачено живцем,
# іноді обгорнутий у markdown ("**Поточна нотатка:**"). Прибираємо програмно
# (regex толерує провідні *#_> та крапку з пробілами), бо словами в промпті
# ("Output ONLY the digest") це не завжди стримує.
def _strip_echoed_label(text: str, *labels: str) -> str:
    s = text.lstrip()
    for label in labels:
        m = re.match(r'^[\s*#_>-]*' + re.escape(label) + r'[\s*:：]*', s, re.IGNORECASE)
        if m:
            return s[m.end():].lstrip()
    return text


# Якщо модель не мала чим заповнити дайджест/нотатку (напр. перший хід занадто
# куций) — вона іноді ЛУНАЄ наш власний плейсхолдер ("(порожньо)",
# "(порожньо — це перший шматок)") замість того, щоб лишити поле незмінним.
# Живий кейс: створився файл теми буквально з вмістом "(порожньо)" — сміття на
# диску. Розпізнаємо й трактуємо як "нічого корисного не повернула".
def _is_empty_echo(text: str) -> bool:
    t = text.strip().strip("()").strip()
    return not t or t.lower().startswith("порожньо")


def update_digest(digest: str, chunk: str, client: OllamaClient | None = None,
                  budget: int | None = None, profile: dict = config.EXECUTOR,
                  on_compress=None) -> str:
    """Злити новий шматок великого документа в дайджест (файл-памʼять). Якщо
    результат вийшов за бюджет — стиснути ще раз тим самим промптом (він і так
    зливає/стискає, тож повторний прохід над самим дайджестом природно коротшає).
    profile: режим виклику (типово EXECUTOR — без роздумів)."""
    client = client or OllamaClient()
    budget = budget or limits.get().digest
    user = (f"Поточний дайджест:\n{digest or '(порожньо — це перший шматок)'}\n\n"
            f"Новий шматок документа:\n{chunk}")
    msg = client.chat(
        [{"role": "system", "content": _DIGEST_SYSTEM}, {"role": "user", "content": user}],
        profile=profile,
    )
    out = _strip_echoed_label((msg.get("content") or "").strip(), "Поточний дайджест",
                              "Updated digest", "Digest")
    if _is_empty_echo(out):
        out = digest
    if len(out) > budget:
        before = len(out)
        msg2 = client.chat(
            [{"role": "system", "content": _DIGEST_SYSTEM},
             {"role": "user", "content": f"Поточний дайджест (СТИСНИ, забагато):\n{out}\n\n"
                                          "Новий шматок документа:\n(немає — лише стисни вище)"}],
            profile=profile,
        )
        out2 = _strip_echoed_label((msg2.get("content") or "").strip(), "Поточний дайджест",
                                   "Updated digest", "Digest")
        out = out if _is_empty_echo(out2) else out2
        notify_compress(on_compress, "дайджест файлу", before, len(out))
    return out
