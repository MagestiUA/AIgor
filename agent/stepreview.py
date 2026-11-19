"""Перегляд плану планувальником ПІСЛЯ кожного кроку.

Схема: планувальник складає план і віддає виконавцю по ОДНОМУ кроку; виконавець виконує скільки
треба команд і повертає звіт; планувальник дивиться на звіт і вирішує: іти далі, скоригувати
невиконані кроки / додати крок або зупинитись і спитати користувача. Коротке пояснення («що
сталося і що далі») показуємо в чаті.

ГОЛОВНЕ: планувальник отримує не «опис своїми словами», а ТОЧНІ ФАКТИ про дії (`facts_report`):
яку команду виконано і що саме вона вивела; які файли змінено. Звіт збирається кодом із
фактичних викликів тулів, а не зі слів моделі — інакше нечіткий опис («запустив тести, начебто
ок») змушує перепланувати крок, який насправді вдався (або навпаки).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import config
from .memory import render_for_planner

REPORT_MAX = 2400            # загальний ліміт звіту про один крок (символів)
OUT_HEAD, OUT_TAIL = 500, 300   # скільки початку/кінця виводу команди лишаємо
MAX_REPLANS = 3              # скільки разів за один запуск плану можна коригувати план
READ_TOOLS = ("read_file", "list_dir", "read_attachment", "project_note")
WRITE_TOOLS = ("write_file", "edit_file", "create_from_source")

_RC = re.compile(r"\[код завершення:\s*(-?\d+)\]\s*$")


def _clip_output(text: str) -> str:
    t = (text or "").strip()
    if len(t) <= OUT_HEAD + OUT_TAIL + 40:
        return t or "(порожній вивід)"
    return f"{t[:OUT_HEAD]}\n… [пропущено {len(t) - OUT_HEAD - OUT_TAIL} симв.] …\n{t[-OUT_TAIL:]}"


def facts_report(actions: list[tuple[str, dict, str]]) -> str:
    """Точний звіт про дії кроку: кожна команда shell з її виводом, змінені файли, прочитані файли.
    actions: [(назва_тулу, аргументи, результат)] у порядку виконання."""
    lines: list[str] = []
    read: list[str] = []
    for name, args, result in actions:
        a = args or {}
        if name == "run_shell":
            out = (result or "").strip()
            m = _RC.search(out)
            rc = f" (код завершення {m.group(1)})" if m else ""
            if m:
                out = out[:m.start()].strip()
            lines.append(f"$ {a.get('command', '')}{rc}\n{_clip_output(out)}")
        elif name in WRITE_TOOLS:
            path = a.get("path") or a.get("target") or "?"
            head = (result or "").strip().splitlines()[0][:160] if (result or "").strip() else ""
            lines.append(f"✎ {name} {path}: {head}")
        elif name in READ_TOOLS:
            read.append(str(a.get("path") or a.get("name") or "."))
        elif name == "web_search":
            lines.append(f"🔎 web_search: {a.get('query', '')} → {_clip_output(result)[:300]}")
        else:
            lines.append(f"{name}({', '.join(a)}) → {_clip_output(result)[:300]}")
    if read:
        lines.append("Прочитано: " + ", ".join(dict.fromkeys(read))[:300])
    rep = "\n\n".join(lines)
    if len(rep) > REPORT_MAX:                           # хвіст важливіший: останні команди — найсвіжіший стан
        rep = "… [початок звіту скорочено]\n" + rep[-REPORT_MAX:]
    return rep


@dataclass
class Decision:
    action: str = "continue"                         # continue | revise | stop
    comment: str = ""                                # 1–2 речення для чату
    revise: dict[int, str] = field(default_factory=dict)    # step_id -> новий опис (лише невиконані / цей крок)
    add_steps: list[str] = field(default_factory=list)      # нові кроки в кінець плану


REVIEW_SYSTEM = (
    "You are the PLANNER. An executor just finished ONE step of your plan and reported EXACT FACTS: "
    "the commands it ran with their real output and the files it changed. Judge by those facts, not "
    "by the executor's wording. Reply ONLY as JSON: "
    '{"action": "continue"|"revise"|"stop", "comment": "...", "revise": [{"step_id": N, "description": "..."}], '
    '"add_steps": ["..."]}. '
    "continue = the plan stays as is. revise = change descriptions of NOT-yet-done steps (and, if the "
    "step just FAILED, you may re-describe that same step to try a different approach) and/or append "
    "new steps in add_steps. stop = you need input from the user or the task is impossible; explain why "
    "in comment. Never touch steps already marked done. Be conservative: if the facts show the step did "
    "what it was asked (e.g. exit code 0, expected output), choose continue — do not re-plan because of "
    "wording. If a USER COMMENT is given, it has priority: the user watched the work live and is correcting "
    "you — adapt the plan to it (revise pending steps / add steps), and acknowledge it in comment. "
    "If the step was STOPPED (iteration limit, loop detected, context full), it was too big or the approach is wrong: "
    "choose revise and replace that step with 2-4 SMALL steps, each ONE concrete action with a checkable result "
    "(one command / one file), and do not repeat the approach that looped. "
    "If the status says the step is STILL IN PROGRESS (interim check after several actions): continue = the "
    "executor is on track (comment may give it a short hint); revise = the approach is wrong or the step is too "
    "big - re-describe THIS step (smaller / different approach) and/or add steps; stop = you need the user. "
    "comment: 1-2 short sentences in the user's language, first person, saying what happened "
    "(cite the command and its key output when it matters) and what you do next."
)

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue", "revise", "stop"]},
        "comment": {"type": "string"},
        "revise": {"type": "array", "items": {"type": "object", "properties": {
            "step_id": {"type": "integer"}, "description": {"type": "string"}},
            "required": ["step_id", "description"]}},
        "add_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "comment"],
}


def build_review_prompt(plan_text: str, step, ok: bool, final: str, report: str,
                        user_notes: list[str] | None = None) -> str:
    p = (f"{plan_text}\n\nЩойно виконано крок #{step.id}: {step.description}\n"
         f"Статус за оцінкою виконавця: {'КРОК ЩЕ ТРИВАЄ — проміжна перевірка' if ok is None else ('ВИКОНАНО' if ok else 'НЕ ЗАВЕРШЕНО')}\n"
         f"Підсумок виконавця: {(final or '—')[:600]}\n\n"
         f"ТОЧНІ ФАКТИ (команди та їх вивід, змінені файли):\n{report or '(дій не було)'}")
    if user_notes:
        p += ("\n\nКОМЕНТАР КОРИСТУВАЧА (написаний ПІД ЧАС виконання; має ПРІОРИТЕТ над твоїми припущеннями — "
              "виправ план відповідно; якщо він каже, що вже зроблене не так, додай крок-виправлення):\n"
              + "\n".join(f"- {n[:1500]}" for n in user_notes))
    return p


def parse_decision(content: str) -> Decision:
    """Розібрати відповідь моделі; будь-який непорядок → Decision('continue') без коментаря."""
    try:
        d = json.loads(content or "")
    except Exception:
        return Decision()
    if not isinstance(d, dict):
        return Decision()
    action = d.get("action") if d.get("action") in ("continue", "revise", "stop") else "continue"
    rev: dict[int, str] = {}
    for r in d.get("revise") or []:
        try:
            rev[int(r["step_id"])] = str(r["description"]).strip()
        except Exception:
            continue
    adds = [str(s).strip() for s in (d.get("add_steps") or []) if str(s).strip()]
    return Decision(action=action, comment=str(d.get("comment") or "").strip()[:500],
                    revise={k: v for k, v in rev.items() if v}, add_steps=adds[:4])


def review_step(plan_text: str, step, ok: bool, final: str, report: str, client,
                user_notes: list[str] | None = None) -> Decision:
    msg = client.chat([{"role": "system", "content": REVIEW_SYSTEM},
                       {"role": "user", "content": build_review_prompt(plan_text, step, ok, final, report, user_notes)}],
                      profile=config.EXECUTOR, fmt=REVIEW_SCHEMA)
    return parse_decision(msg.get("content") or "")


def apply_decision(plan, dec: Decision, current_id: int, allow_change: bool) -> bool:
    """Застосувати корективи до плану. Повертає True, якщо план змінився.
    Змінювати можна лише невиконані кроки (status != done). Якщо allow_change=False
    (вичерпано ліміт коригувань) — нічого не міняємо."""
    if not allow_change or dec.action != "revise":
        return False
    changed = False
    for sid, text in dec.revise.items():
        st = next((s for s in plan.steps if s.id == sid), None)
        if st is None or st.status == "done":
            continue
        st.description = text
        if sid == current_id and st.status == "failed":
            st.status = "pending"                    # повторимо цей крок за новим описом
        changed = True
    for text in dec.add_steps:
        plan.add_step("llm", text)
        changed = True
    return changed


# ── Звірка узгодженості виконаного плану (після всіх кроків) ───────────────────
CONSISTENCY_SYSTEM = (
    "You review an executed plan (compact slice: steps, statuses, short results). Decide "
    "if all steps are mutually consistent and the task is complete. Reply ONLY as JSON: "
    "{\"ok\": true} if consistent; otherwise {\"ok\": false, \"step_id\": N, \"note\": "
    "\"what to fix\"} naming ONE step to redo with a short clarification (user's language). "
    "Be conservative: report a problem ONLY if a step clearly failed or two steps contradict "
    "each other; do not ask to redo a step just to polish it."
)

CONSISTENCY_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "step_id": {"type": ["integer", "null"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["ok"],
}


def check_consistency(state, client) -> dict:
    """Звірити узгодженість виконаного плану. -> {ok, step_id, note}."""
    msg = client.chat(
        [{"role": "system", "content": CONSISTENCY_SYSTEM},
         {"role": "user", "content": render_for_planner(state)}],
        profile=config.EXECUTOR, fmt=CONSISTENCY_SCHEMA,
    )
    try:
        d = json.loads(msg.get("content") or "")
        if isinstance(d, dict):
            return {"ok": bool(d.get("ok")), "step_id": d.get("step_id"),
                    "note": str(d.get("note") or "")}
    except Exception:
        pass
    return {"ok": True, "step_id": None, "note": ""}          # не змогли розпарсити -> не зациклюємось
