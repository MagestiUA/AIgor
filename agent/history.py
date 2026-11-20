"""Пошук у повній історії розмови (тул `recall_history`).

Підсумок розмови стискає історію й гублює деталі (точна команда, рядок помилки, що саме сказав користувач). Повна
розмова при цьому лежить у файлі сесії — цей модуль дає моделі змогу дістати з неї потрібні уривки на вимогу,
не тримаючи їх у контексті.
"""
from __future__ import annotations

import re

MAX_HITS = 5            # скільки уривків повертаємо за один пошук
SNIPPET = 700           # довжина уривка навколо збігу
FULL_LIMIT = 6000       # скільки символів віддаємо при читанні одного повідомлення за номером
STEM = 5                # довжина «кореня» слова для грубого врахування відмінків/закінчень (укр.)
_WORD = re.compile(r"\w{3,}", re.UNICODE)
ROLE = {"user": "користувач", "assistant": "Ігорь"}
KINDS = ("text", "answer", "plan")


def _words(text: str) -> list[str]:
    return [w[:STEM] for w in _WORD.findall((text or "").lower())]


def _turns(messages: list[dict]) -> list[tuple[int, dict]]:
    """Змістовні репліки з їхніми НОМЕРАМИ (позиція в повній історії, з 1)."""
    out = []
    for i, m in enumerate(messages, 1):
        if m.get("role") in ROLE and m.get("kind", "text") in KINDS and (m.get("content") or "").strip():
            out.append((i, m))
    return out


def _snippet(content: str, stems: list[str]) -> str:
    low = content.lower()
    pos = min((p for p in (low.find(s) for s in stems) if p >= 0), default=0)
    start = max(0, pos - SNIPPET // 4)
    chunk = content[start:start + SNIPPET].strip()
    return ("…" if start else "") + chunk + ("…" if start + SNIPPET < len(content) else "")


def search(messages: list[dict], query: str = "", number: int | None = None, limit: int = MAX_HITS) -> str:
    """number -> повний текст повідомлення №N; query -> найрелевантніші уривки; порожньо -> останні репліки."""
    turns = _turns(messages)
    if not turns:
        return "історія розмови порожня"
    if number:
        for i, m in turns:
            if i == number:
                c = m["content"].strip()
                return f"#{i} [{ROLE[m['role']]}]:\n" + (c if len(c) <= FULL_LIMIT else c[:FULL_LIMIT] + "\n…(обрізано)")
        return f"повідомлення №{number} нема (є {turns[0][0]}…{turns[-1][0]})"
    stems = list(dict.fromkeys(_words(query)))
    if not stems:
        pick = turns[-limit:]
        return "Останні репліки (номер читається повністю через number):\n" + "\n\n".join(
            f"#{i} [{ROLE[m['role']]}]: {m['content'].strip()[:SNIPPET // 2]}" for i, m in pick)
    scored = []
    for i, m in turns:
        ws = set(_words(m["content"]))
        hit = sum(1 for s in stems if s in ws)
        if hit:
            scored.append((hit, i, m))
    if not scored:
        return f"у розмові нічого не знайдено за «{query}». Спробуй інші слова (імена файлів, фрагмент помилки)."
    scored.sort(key=lambda t: (-t[0], -t[1]))                 # більше збігів, потім новіші
    top = sorted(scored[:limit], key=lambda t: t[1])
    body = "\n\n".join(f"#{i} [{ROLE[m['role']]}]: {_snippet(m['content'].strip(), stems)}" for _, i, m in top)
    return f"Знайдено {len(scored)}, показано {len(top)} (повний текст — recall_history з number):\n\n{body}"


HINT = ("Повна розмова збережена: якщо в підсумку бракує деталей (точна команда, текст помилки, що саме просив користувач) — "
        "не вгадуй, а знайди їх тулом recall_history(query=…) або прочитай повністю за номером (number=N).")
