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
    return sum(1 for s in state.steps if s.kind != "inline") >= threshold


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
