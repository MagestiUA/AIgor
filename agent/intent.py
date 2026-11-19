"""Класифікація наміру задачі: edit | answer | shell.

Дозволяє агенту не лише правити файли, а й відповідати на питання та запускати
команди. Швидкий виклик (EXECUTOR, think off) + format=json для жорсткої структури.
При нерозбірливій відповіді -> 'answer' (безпечний, read-only дефолт).
"""
from __future__ import annotations

import json

from . import config
from .llm import OllamaClient
from .orchestration import explicit_no_change, looks_like_error_log, wants_discussion

MODES = {"edit", "answer", "shell", "plan"}

SYSTEM = (
    "Classify the user's request into exactly one mode:\n"
    "- 'edit': modify, refactor, create, or delete code or files right away.\n"
    "- 'plan': the user wants to plan or discuss the approach FIRST, without making "
    "changes yet (e.g. 'let's plan', 'how would you approach', 'don't code yet').\n"
    "- 'answer': explain, analyze, or ask about the code/project; NO changes made.\n"
    "- 'shell': run a command (tests, list files, build, lint).\n"
    "Questions about feasibility or explanations ('can we add X?', 'чи можна додати X?', "
    "'how would this work?') are 'answer' — the user is discussing, NOT asking to change anything.\n"
    "If the user message is a short confirmation (e.g. 'do it', 'роби') right after the "
    "assistant proposed changes, classify it as 'edit'.\n"
    'Reply ONLY as JSON: {"mode": "edit|plan|answer|shell"}.'
)

SCHEMA = {
    "type": "object",
    "properties": {"mode": {"type": "string", "enum": ["edit", "plan", "answer", "shell"]}},
    "required": ["mode"],
}


AUTONOMY_RULE = (
    "\nAUTONOMOUS MODE is ON (the user allowed edits without confirmation): the user expects problems to be FIXED, "
    "not just explained. If the message reports a problem (an error log, traceback, failing behavior, 'what is wrong?', "
    "'why does it not work?', attached log files), classify it as 'edit'. Use 'answer' only for neutral explanations "
    "('how does X work?') where nothing is reported as broken."
)


def classify_intent(task: str, client: OllamaClient | None = None, context: str = "", autonomous: bool = False,
                    has_attachments: bool = False) -> str:
    """context — що було перед повідомленням (остання відповідь асистента): без нього коротке
    «роби» класифікується навмання (фідбек розробників).
    autonomous — режим правок «auto»: лог помилки/повідомлення про збій = виправляти, а не лише пояснювати."""
    if autonomous and looks_like_error_log(task) and not explicit_no_change(task):
        return "edit"                             # лог помилки в режимі auto — виправляємо, без виклику моделі
    # питання про можливість/«просто питання» — обговорення, без виклику моделі (в auto з прикріпленим логом — питаємо модель)
    if wants_discussion(task) and not (autonomous and has_attachments and not explicit_no_change(task)):
        return "answer"
    client = client or OllamaClient()
    user = (context + "\n\nПовідомлення користувача:\n" if context else "") + task
    if has_attachments:
        user += "\n\n[До повідомлення прикріплено файли — можливо логи.]"
    msg = client.chat(
        [{"role": "system", "content": SYSTEM + (AUTONOMY_RULE if autonomous else "")}, {"role": "user", "content": user}],
        profile=config.EXECUTOR, fmt=SCHEMA,
    )
    try:
        mode = json.loads(msg.get("content") or "{}").get("mode", "")
    except Exception:
        mode = ""
    return mode if mode in MODES else "answer"


CMD_SYSTEM = (
    "The user wants to run a command. Output ONLY the exact single command line to run, "
    "no markdown, no explanation. Examples: pytest -q | python main.py | git status"
)


def extract_command(task: str, client: OllamaClient | None = None) -> str:
    """Витягти конкретну команду з NL-запиту (для shell-режиму). run_shell усе одно
    перевірить її по allow-list."""
    client = client or OllamaClient()
    msg = client.chat(
        [{"role": "system", "content": CMD_SYSTEM}, {"role": "user", "content": task}],
        profile=config.EXECUTOR,
    )
    lines = (msg.get("content") or "").strip().splitlines()
    return lines[0].strip().strip("`").strip() if lines else ""
