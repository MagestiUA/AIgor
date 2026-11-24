"""Стан задачі — структурований, не «LLM-summary».

Ключова ідея: планувальник бачить КОМПАКТНИЙ зріз (план зі статусами + факти +
стислі результати кроків), а не сиру історію. Так контекст не роздувається й
переживає перезапуск (план, що очікує, лежить у сесії). Що зберігати — вирішує СХЕМА, а не модель (керована втрата).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

REPORT_IN_PLAN = 400      # скільки символів фактів кроку показуємо у зрізі плану


@dataclass
class Step:
    id: int
    kind: str                 # act | note (у старих збережених планах: llm/deterministic = act, inline = note)
    description: str
    target: str | None = None
    status: str = "pending"   # pending | done | failed
    result: str = ""          # СТИСЛИЙ результат (один рядок), не сирий вивід
    report: str = ""          # точні факти кроку: команда → вивід, змінені файли (agent/stepreview.facts_report)

    @property
    def is_note(self) -> bool:
        return self.kind in ("note", "inline")

    @property
    def is_noop(self) -> bool:
        """Примітка (`note`) без файлу-цілі — справді нічого виконувати. Якщо ж у неї є target, планувальник помилково
        назвав правку файлу приміткою — виконуємо (інакше крок мовчки помічався б виконаним, а файл лишався незмінним).
        Так само, якщо в описі є дієслово зміни («додати імпорт…», «виправити…») — виконуємо."""
        from .orchestration import wants_change
        return self.is_note and not (self.target or "").strip() and not wants_change(self.description)


@dataclass
class TaskState:
    task: str
    steps: list[Step] = field(default_factory=list)
    facts: dict = field(default_factory=dict)   # артефакти: BODY_1 -> parser.py:25
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def add_step(self, kind: str, description: str, target: str | None = None) -> Step:
        s = Step(id=len(self.steps) + 1, kind=kind, description=description, target=target)
        self.steps.append(s)
        self.updated = time.time()
        return s

    def set_result(self, step_id: int, status: str, result: str = "") -> None:
        for s in self.steps:
            if s.id == step_id:
                s.status, s.result = status, result
        self.updated = time.time()

    def next_pending(self) -> Step | None:
        return next((s for s in self.steps if s.status == "pending"), None)


def state_from_dict(data: dict) -> TaskState:
    """Відновити TaskState зі словника (план, що очікує виконання, зберігається в сесії — session.pending_plan)."""
    steps = [Step(**s) for s in data.get("steps", [])]
    rest = {k: v for k, v in data.items() if k != "steps"}
    return TaskState(steps=steps, **rest)


def render_for_planner(state: TaskState) -> str:
    """Компактний зріз для планувальника. БЕЗ сирих виходів — лише статуси,
    стислі результати й факти. Це й тримає контекст вузьким."""
    mark = {"done": "[x]", "failed": "[!]", "pending": "[ ]"}
    lines = [f"Задача: {state.task}", "", "План:"]
    for s in state.steps:
        tgt = f" ({s.target})" if s.target else ""
        lines.append(f"  {mark.get(s.status, '[ ]')} #{s.id}{' [примітка]' if s.is_noop else ''}{tgt}: {s.description}")
        if s.result:
            lines.append(f"        -> {s.result}")
        if s.report:                                     # точні команди й вивід — стисло, щоб не роздувати контекст
            rep = s.report if len(s.report) <= REPORT_IN_PLAN else s.report[:REPORT_IN_PLAN] + "…"
            lines.append("        факти: " + rep.replace("\n", "\n          "))
    if state.facts:
        lines.append("\nФакти/артефакти:")
        for k, v in state.facts.items():
            lines.append(f"  - {k}: {v}")
    return "\n".join(lines)


_SUBSTEP = re.compile(r"\s(?=\d{1,2}\)\s)")
_NUM = re.compile(r"^\d{1,2}\)\s*")


def render_for_user(state: TaskState) -> str:
    """План для показу в чаті (markdown): окремі пункти, вкладені «1) … 2) …» — підпункти.
    Одноразові переноси рядків markdown склеює, тому кожен пункт — власний рядок списку."""
    icon = {"done": "✅", "failed": "❌", "pending": "⬜"}
    out = [f"**Задача:** {state.task}", "", "**План:**", ""]
    for s in state.steps:
        tgt = f" · `{s.target}`" if s.target else ""
        head, *subs = _SUBSTEP.split(s.description.strip())
        mark = "📝" if s.is_noop else icon.get(s.status, "⬜")
        out.append(f"{mark} **#{s.id}**{tgt} — {head.strip()}")
        for sub in subs:
            out.append("    - " + _NUM.sub("", sub.strip()))
        if s.result:
            out.append(f"    - ↳ {s.result}")
        out.append("")
    if state.facts:
        out += ["**Факти/артефакти:**", ""] + [f"- {k}: {v}" for k, v in state.facts.items()]
    return "\n".join(out).rstrip()
