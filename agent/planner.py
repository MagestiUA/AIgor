"""Planner — вирішує, що робити із запитом, і ріже задачу на кроки.

Один структурований виклик (deliberate) замість двох: раніше спершу окремий класифікатор наміру (intent), потім
планувальник. Тепер планувальник сам обирає дію: answer (питання/обговорення — нічого не змінюємо) | shell (просто
запустити) | clarify / choose (уточнити в користувача) | plan (кроки). Швидкі однозначні випадки («роби», «просто
питання», лог помилки в режимі auto) вирішує код без моделі — intent.quick_route.

Типи кроків — лише два: act (крок щось робить: читає, створює, змінює, запускає) і note (примітка без дії — не
виконується). Колишні llm/deterministic виконання не розрізняло (усі йшли в той самий tool-loop), тож модель лише
витрачала увагу на непотрібну класифікацію; старі значення в збережених планах читаються як act.

Надійність структури — через JSON-схему (Ollama format / vLLM guided decoding); _parse_steps рятує обрізаний JSON.
"""
from __future__ import annotations

import json
import re

from . import config
from .llm import OllamaClient
from .memory import TaskState

KINDS = ("act", "note")
_LEGACY_KIND = {"llm": "act", "deterministic": "act", "inline": "note"}


def _parse_steps(content: str) -> list[dict]:
    """Дістати кроки з відповіді. Стійко до обірваного JSON: якщо весь обʼєкт не
    парситься — вихоплюємо окремі step-обʼєкти (вони пласкі)."""
    try:
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            return data["steps"]
    except Exception:
        pass
    steps = []
    for m in re.findall(r"\{[^{}]*\}", content):
        try:
            obj = json.loads(m)
            if isinstance(obj, dict) and obj.get("description"):
                steps.append(obj)
        except Exception:
            pass
    return steps

STEP_RULES = (
    "Each step is an object {\"kind\":..,\"description\":..,\"target\":..}: kind is 'act' (the step DOES something — "
    "reads, creates or edits files, runs commands) or 'note' (a remark that needs NO action at all; notes are skipped, "
    "never executed — ANY step that touches a file or runs something, even a one-line config edit, is 'act'); "
    "target is the file path the step touches, or null. Large opaque data literals (protobufs, payloads, keys) must be "
    "copied/moved with create_from_source or edit_file (they keep the bytes) — never retyped. Write each 'description' "
    "in the SAME language as the user's task."
)

SYSTEM = (
    "You are the planning module of a coding agent that runs on a small local model. "
    "Decompose the user's task into a SHORT list of coarse steps (aim 2-6; do NOT "
    "over-split into micro-steps). " + STEP_RULES + " Reply ONLY as JSON: {\"steps\":[...]}."
)

_STEP_ITEM = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "description": {"type": "string"},
        "target": {"type": ["string", "null"]},
    },
    "required": ["kind", "description"],
}

# JSON-schema для format -> жорстка структура відповіді.
SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": _STEP_ITEM}},
    "required": ["steps"],
}


# ── Планувальник-діалог (#2 + #5): уточнення й вибір рішень НА РІВНІ планувальника ──
DELIB_SYSTEM = (
    "You are the planning module of a coding agent on a small local model. Decide how "
    "to proceed with the user's task and reply ONLY as JSON.\n"
    "- If the task is AMBIGUOUS (e.g. it does not clearly say WHICH file to change, or "
    "the target is unclear) -> action=\"clarify\": ask ONE short question and give "
    "concrete clickable options (candidate file names from the project structure).\n"
    "- If there are 2-3 genuinely different sensible APPROACHES and the choice matters "
    "-> action=\"choose\": ask which approach; options = the approaches (label + short "
    "detail).\n"
    "- Otherwise -> action=\"plan\": output the final steps. " + STEP_RULES + " "
    "Resolve ALL ambiguity HERE — the executor will NOT ask anything.\n"
    "Only ask when it genuinely helps; do NOT invent questions for clear tasks. Include "
    "a short \"reasoning\" (one sentence). Write reasoning/question/options/descriptions "
    "in the SAME language as the user's task."
)

# Маршрутизація в тому самому виклику (замість окремого класифікатора наміру intent.classify_intent).
ROUTE_RULES = (
    "\nFIRST decide what the user wants:\n"
    "- action=\"answer\": a question, explanation, analysis or discussion of the code/project — NOTHING is to be "
    "changed ('how does X work?', 'why does it fail?', 'can we add X?', 'чи можна додати X?' are discussions). Also "
    "when the user wants to DISCUSS an approach before any change. No steps.\n"
    "- action=\"shell\": the user only wants to RUN something and see the result (tests, a script, git status, a "
    "build). No steps.\n"
    "- otherwise the user wants changes -> clarify / choose / plan as above. Set wait_for_approval=true when the user "
    "asked to see or agree on the plan first ('спершу план', 'let's plan', 'don't code yet').\n"
    "A short confirmation ('do it', 'роби') right after the assistant proposed changes means plan."
)

AUTONOMY_RULE = (
    "\nAUTONOMOUS MODE is ON (the user allowed edits without confirmation): the user expects problems to be FIXED, "
    "not just explained. If the message reports a problem (an error log, traceback, failing behavior, 'what is wrong?', "
    "'why does it not work?', attached log files), choose plan (find the cause, fix it, verify). Use 'answer' only for "
    "neutral explanations ('how does X work?') where nothing is reported as broken."
)

_OPTIONS = {
    "type": ["array", "null"],
    "items": {
        "type": "object",
        "properties": {"label": {"type": "string"}, "detail": {"type": "string"}},
        "required": ["label"],
    },
}


def delib_schema(route: bool = False) -> dict:
    actions = ["answer", "shell", "clarify", "choose", "plan"] if route else ["clarify", "choose", "plan"]
    props = {
        "action": {"type": "string", "enum": actions},
        "reasoning": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "options": _OPTIONS,
        "steps": {"type": ["array", "null"], "items": _STEP_ITEM},
    }
    if route:
        props["wait_for_approval"] = {"type": "boolean"}
    return {"type": "object", "properties": props, "required": ["action"]}


DELIB_SCHEMA = delib_schema(False)


def _parse_decision(content: str) -> dict:
    """Розпарсити рішення планувальника. Стійко до обриву: якщо обʼєкт не парситься —
    вертаємо план із врятованих кроків."""
    try:
        d = json.loads(content)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"action": "plan", "steps": _parse_steps(content), "_salvaged": True}


def norm_kind(kind) -> str:
    """Тип кроку до act|note (старі llm/deterministic → act, inline → note; невідоме → act)."""
    k = str(kind or "act")
    return k if k in KINDS else _LEGACY_KIND.get(k, "act")


def _steps_to_state(task: str, steps: list[dict]) -> TaskState:
    state = TaskState(task=task)
    for st in steps or []:
        desc = str(st.get("description", "")).strip()
        if desc:
            state.add_step(norm_kind(st.get("kind")), desc, st.get("target"))
    return state


def deliberate(task: str, context: str = "", client: OllamaClient | None = None,
               project_doc: str | None = None, history: list | None = None,
               images: list[str] | None = None, route: bool = False, autonomous: bool = False,
               has_attachments: bool = False) -> dict:
    """Один хід планувальника-діалогу. Повертає dict:
      action: 'answer' | 'shell' (лише з route=True) | 'clarify' | 'choose' | 'plan'
      reasoning: коротке пояснення (мова користувача)
      question: текст питання (для clarify/choose)
      options:  [{label, detail}] — клікабельні кандидати/підходи
      state:    TaskState (лише для action='plan'), інакше None
      wait:     користувач просив спершу показати план (лише з route=True)
    route=True — той самий виклик ще й визначає тип запиту (замість окремого класифікатора наміру).
    history — попередні уточнення [{question, answer}], щоб наступний хід їх врахував."""
    client = client or OllamaClient()
    history = history or []
    user = ""
    if project_doc:
        user += f"Контекст проекту (AGENT.md):\n{project_doc}\n\n"
    user += f"Задача:\n{task}"
    if context:
        user += f"\n\nСтруктура/контекст:\n{context}"
    if history:
        qa = "\n".join(f"- Запитав: {h.get('question', '')}\n  Відповідь: {h.get('answer', '')}"
                       for h in history)
        user += f"\n\nВже уточнено з користувачем:\n{qa}"
    if route and has_attachments:
        user += "\n\n[До повідомлення прикріплено файли — можливо логи.]"

    umsg = {"role": "user", "content": user}
    if images:                                           # зображення користувача (скриншот, макет) — планувальнику теж
        umsg["images"] = list(images)
    system = DELIB_SYSTEM + (ROUTE_RULES if route else "") + (AUTONOMY_RULE if route and autonomous else "")
    msg = client.chat(
        [{"role": "system", "content": system}, umsg],
        profile=config.EXECUTOR, fmt=delib_schema(route),
    )
    d = _parse_decision(msg.get("content") or "")
    allowed = {"answer", "shell", "clarify", "choose", "plan"} if route else {"clarify", "choose", "plan"}
    action = d.get("action") if d.get("action") in allowed else "plan"
    if route and d.get("_salvaged") and not d.get("steps"):
        action = "answer"                                # нерозбірлива відповідь маршрутизатора → безпечне «лише відповісти»

    options = []
    for o in (d.get("options") or []):
        if isinstance(o, dict) and o.get("label"):
            options.append({"label": str(o["label"]).strip(), "detail": str(o.get("detail", "")).strip()})

    state = None
    if action == "plan":
        state = _steps_to_state(task, d.get("steps") or [])
        if not state.steps:                              # порожній план -> звичайний make_plan
            state = make_plan(task, context, client, project_doc, images=images)

    return {"action": action, "reasoning": str(d.get("reasoning", "")).strip(),
            "question": str(d.get("question", "")).strip(), "options": options, "state": state,
            "wait": bool(d.get("wait_for_approval")) if route else False}


def make_plan(task: str, context: str = "", client: OllamaClient | None = None,
              project_doc: str | None = None, images: list[str] | None = None) -> TaskState:
    client = client or OllamaClient()
    user = ""
    if project_doc:
        user += f"Контекст проекту (AGENT.md):\n{project_doc}\n\n"
    user += f"Задача:\n{task}"
    if context:
        user += f"\n\nКонтекст:\n{context}"
    # think=OFF для структурованого виводу: думання плутає/обриває JSON у gemma.
    umsg = {"role": "user", "content": user}
    if images:
        umsg["images"] = list(images)
    msg = client.chat(
        [{"role": "system", "content": SYSTEM}, umsg],
        profile=config.EXECUTOR, fmt=SCHEMA,
    )

    return _steps_to_state(task, _parse_steps(msg.get("content") or ""))
