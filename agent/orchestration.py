"""Керуючий процес код-режиму: чиста логіка (без UI і моделі), яку використовує lca_web.

Фідбек розробників: LLM відповів «так, це можна зробити», розробник написав «роби» — і
з цього мала вийти купа викликів, але:
  * `classify_intent("роби")` не бачив розмови, тож вгадував; планувальник отримував лише
    слово «роби» та стислий підсумок, а не саму пропозицію, яку схвалили;
  * кроки не бачили результатів одне одного, фінальної звірки не було, а невдалий крок
    не зупиняв решту.
Тут: (A) розпізнавання підтвердження й розгортання в схвалену пропозицію, (B) поріг
«великого плану», (C) що змінили кроки + перевірка синтаксису змінених файлів, вибір
кроку для повтору, (D) допомога для зупинки/відновлення плану.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import tools

# ── A: підтвердження ─────────────────────────────────────────────────────────
_CONFIRM = {
    "роби", "зроби", "робимо", "давай", "го", "так", "ага", "угу", "ок", "окей", "оке", "добре",
    "добро", "виконуй", "виконай", "приступай", "починай", "розпочинай", "реалізуй", "погоджуюсь",
    "згоден", "згодна", "підходить", "супер", "+", "yes", "ok", "okay", "go", "sure", "proceed",
    "do", "it", "ahead", "implement",
    "додай", "додавай", "впроваджуй", "реалізовуй", "пиши", "кодь", "додаємо", "реалізуємо",
}
_FILLERS = {"будь", "ласка", "будьласка", "це", "його", "її", "все", "тоді", "і", "та", "ну", "пліз",
            "please", "then", "that", "now", "зараз", "вже", "оце", "цей", "це,"}
MAX_CONFIRM_WORDS = 5


def is_confirmation(text: str) -> bool:
    """Коротка згода-команда («роби», «давай, роби це», «так», «ok, do it»). Будь-яке слово
    поза словником згоди (напр. «ні, не роби», «роби тести») — це НЕ підтвердження."""
    words = re.sub(r"[^\w\s+]", " ", (text or "").lower()).split()
    if not words or len(words) > MAX_CONFIRM_WORDS:
        return False
    core = [w for w in words if w not in _FILLERS]
    return bool(core) and all(w in _CONFIRM for w in core)


def last_assistant_text(messages: list[dict]) -> str:
    """Повний текст останньої змістовної відповіді асистента ПЕРЕД поточним повідомленням
    користувача (відповідь/план/текст; службові кроки й нотатки пропускаємо)."""
    msgs = list(messages)
    while msgs and msgs[-1].get("role") == "user":          # поточне повідомлення (і чергу) відкидаємо
        msgs.pop()
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("kind", "text") in ("answer", "text", "plan") \
                and (m.get("content") or "").strip():
            return m["content"].strip()
    return ""


def expand_confirmation(text: str, last_assistant: str) -> str | None:
    """«роби» -> задача з ПОВНИМ текстом схваленої пропозиції; None, якщо це не підтвердження
    або нічого підтверджувати."""
    if not last_assistant or not is_confirmation(text):
        return None
    return (f"Виконай те, що ти щойно запропонував (користувач підтвердив: «{text.strip()}»). "
            "Якщо пропозиція містила кілька варіантів — обирай РЕКОМЕНДОВАНИЙ (позначений як "
            "рекомендований/найкращий) і НЕ перепитуй, який саме. "
            f"Твоя попередня відповідь, яку схвалено:\n\n{last_assistant}")


def context_for_intent(last_assistant: str, n: int = 1200) -> str:
    """Контекст для класифікатора наміру: що було перед короткою реплікою користувача."""
    if not last_assistant:
        return ""
    tail = last_assistant if len(last_assistant) <= n else last_assistant[:n] + "…"
    return f"Попередня відповідь асистента (для розуміння короткої репліки користувача):\n{tail}"


# ── B: великий план ──────────────────────────────────────────────────────────
BIG_PLAN_STEPS = 4


def is_big_plan(state, threshold: int = BIG_PLAN_STEPS) -> bool:
    """План, що потребує обов'язкового схвалення: ≥threshold кроків, які щось виконують."""
    return sum(1 for s in state.steps if not s.is_noop) >= threshold


# ── C: що змінили кроки і чи це живе ────────────────────────────────────────
def touched_paths(name: str, args: dict) -> list[str]:
    a = args or {}
    if name in ("write_file", "edit_file"):
        return [a["path"]] if a.get("path") else []
    if name == "create_from_source":
        return [a["target"]] if a.get("target") else []
    return []


def syntax_report(root: str | Path, paths: list[str]) -> list[tuple[str, str]]:
    """[(файл, помилка)] для змінених .py, що не компілюються (відсутні файли пропускаємо)."""
    bad: list[tuple[str, str]] = []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        p = Path(root) / rel
        if not p.is_file():
            continue
        try:
            ok, err = tools.validate_python(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not ok:
            bad.append((rel, err))
    return bad


def pick_step(touched_by_step: dict[int, list[str]], bad_paths: list[str], fallback: int | None = None) -> int | None:
    """Який крок повторити для проблемних файлів: ОСТАННІЙ, що їх чіпав."""
    hits = [sid for sid, files in touched_by_step.items() if any(f in files for f in bad_paths)]
    if hits:
        return max(hits)
    return fallback if fallback is not None else (max(touched_by_step) if touched_by_step else None)


# ── питання vs завдання ─────────────────────────────────────────────────────
# Фідбек: «чи можна додати хелс-чек? ... просто питання без вимоги відразу писати код» —
# класифікатор видав «план». Питання про можливість/пояснення — це обговорення, не завдання.
_Q_START = {"чи", "як", "що", "де", "коли", "чому", "навіщо", "скільки", "який", "яка", "яке", "які",
            "хто", "кому", "is", "can", "how", "what", "why", "where", "when", "should", "does", "do", "are"}
# «чи можеш/зможеш додати…», «can you…» — ввічливе ПРОХАННЯ зробити, не питання
_POLITE_REQUEST = ("чи можеш", "чи зможеш", "чи міг би", "чи могла б", "чи могли б", "можеш ", "зможеш ",
                   "can you", "could you", "would you", "will you")


def looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if not t.endswith("?"):
        return False
    low = t.lower()
    if any(low.startswith(p) for p in _POLITE_REQUEST):
        return False
    words = re.findall(r"[\w']+", low)
    return bool(words) and words[0] in _Q_START


# ── недавній діалог для режиму «аналіз» і планувальника ─────────────────────
def recent_dialogue(messages: list[dict], n: int = 6, per: int = 2500) -> str:
    """Кілька останніх реплік (користувач/асистент: відповіді, плани, тексти) ПЕРЕД поточним
    повідомленням. Живий кейс: без цього режим «аналіз» не бачив попереднього плану й питання
    і відповів не на те (плутав із попередньою задачею), а ще перечитував усі файли."""
    msgs = list(messages)
    while msgs and msgs[-1].get("role") == "user":
        msgs.pop()
    keep = [m for m in msgs if m.get("role") in ("user", "assistant")
            and m.get("kind", "text") in ("text", "answer", "plan") and (m.get("content") or "").strip()][-n:]
    if not keep:
        return ""
    lines = []
    for m in keep:
        who = "Користувач" if m["role"] == "user" else "Асистент"
        c = m["content"].strip()
        lines.append(f"{who}: {c if len(c) <= per else c[:per] + '…'}")
    return "Недавня розмова (для розуміння поточного повідомлення):\n" + "\n\n".join(lines) + "\n\n"


# Прямі вказівки «лише обговорюємо» (фідбек: «це було просто питання … без вимоги відразу писати код»)
_DISCUSS_MARKERS = ("просто питання", "лише питання", "тільки питання", "без вимоги", "не пиши код",
                    "поки не пиши", "не треба код", "без коду", "лише обговор", "тільки обговор",
                    "just a question", "don't code", "do not code", "no code yet", "just asking")


# Дієслова зміни/запуску: крок, де вони є, НЕ «тривіальний» і не суто дослідницький.
CHANGE_RE = re.compile(
    r"\b(виправ\w*|зміни|змінити|додай|додати|створи|створити|напиши|написати|видали|видалити|замін\w+|"
    r"реалізуй|онови|оновити|перейменуй|запусти|встанови|імпортуй|імпортув\w+|вставити|вставь|прибери|прибрати|"
    r"перепиши|переписати|fix|edit|create|write|implement|add|remove|run|install|delete|rename|import)\b", re.I)


def wants_change(text: str) -> bool:
    return bool(CHANGE_RE.search(text or ""))


# ── режим «правки: auto» — Ігорь діє самостійно ───────────────────────────────
_ERR_MARKERS = re.compile(
    r"Traceback \(most recent call last\)|\b[A-Z][A-Za-z]*(?:Error|Exception)\b|\bFAILED\b|npm ERR!|error TS\d+|"
    r"\bCannot find module\b|\bSegmentation fault\b|\bpanic:|File \"[^\"]+\", line \d+|"
    r"^\s*at \S+ ?\(.*:\d+|\bE/AndroidRuntime\b|\bFATAL EXCEPTION\b", re.MULTILINE)


def looks_like_error_log(text: str) -> bool:
    """Чи повідомлення схоже на лог помилки/трасування (Traceback, XxxError, FAILED, стек викликів…)."""
    t = text or ""
    return len(_ERR_MARKERS.findall(t)) >= 1 and (len(t.splitlines()) >= 3 or len(t) > 200)


def explicit_no_change(text: str) -> bool:
    """Користувач прямо просить НЕ змінювати код (лише обговорення)."""
    low = (text or "").lower()
    return any(m in low for m in _DISCUSS_MARKERS) or any(m in low for m in ("не змінюй", "не виправляй", "лише поясни", "тільки поясни"))


AUTONOMY_NOTE = (
    "РЕЖИМ САМОСТІЙНИХ ПРАВОК (правки: auto). Користувач дозволив вносити зміни без підтвердження й очікує РЕЗУЛЬТАТ, а не "
    "опис. Якщо повідомлення — лог помилки, повідомлення про збій чи «що не так / чому не працює»: з'ясуй причину, ВИПРАВ її "
    "(правка файлів), а потім перевір виправлення (запусти тест або команду, якщо є) і коротко звітуй, що змінено. Не "
    "зупиняйся на поясненні причини й не питай дозволу на очевидне виправлення. Лише якщо потрібен вибір, який може зробити "
    "лише користувач, — запитай.")


def wants_discussion(text: str) -> bool:
    """Питання про можливість/пояснення АБО явне «просто обговорюємо, не пиши код»."""
    low = (text or "").lower()
    return looks_like_question(text) or any(m in low for m in _DISCUSS_MARKERS)
