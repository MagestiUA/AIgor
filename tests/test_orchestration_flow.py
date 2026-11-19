"""Керуючий процес код-режиму: реальні State._execute_steps / _verify_plan на заглушці стану.
Кроки «виконує» скриптова функція (модель не потрібна), сесії на диску не чіпаємо.

Перевіряємо: кроки бачать результати попередніх; невдалий крок зупиняє план і лишає його для
відновлення; відновлення пропускає виконані кроки; звірка ловить синтаксичну помилку й повторює
крок; успішна звірка нічого не повторює."""
import asyncio
import tempfile
import types
from pathlib import Path

import lca_web.lca_web as W
from agent import limits, projectmap
from agent.agent_loop import ITER_LIMIT_MSG
from agent.memory import TaskState

limits.FORCE = limits.LOCAL


class FakeState:
    """Мінімум State, потрібний _execute_steps/_verify_plan."""
    def __init__(self, root: Path, script):
        self.root, self.script = root, script
        self.current_id = "sid-test"
        self.status = ""
        self.context_summary = ""
        self.notes: list[str] = []
        self.pending_saved = None
        self.has_pending = False
        self._step_touched: list = []
        self._step_actions: list = []
        self.step_contexts: list[str] = []
        self.run_log: list[tuple[str, str]] = []
        self.planner = None            # заглушка моделі-планувальника; None → перегляд плану нейтральний («далі»)
        self.queued_text = ""
        for name in ("_step_context", "_verify_plan", "_execute_steps_inner"):
            setattr(self, name, types.MethodType(getattr(W.State, name), self))

    def _roots_note(self):
        return ""

    async def _review_after_step(self, plan, step, ok, final, client, may_change, user_notes=None):
        if self.planner is None:
            return W.review.Decision()
        return await W.State._review_after_step(self, plan, step, ok, final, self.planner, may_change, user_notes)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _exec_ctx(self, root, client):
        return object()

    def _attachments_note(self):
        return ""

    def _append(self, role, content, kind="text", **kw):
        self.notes.append(content)

    def _save_pending(self, plan):
        self.pending_saved = plan
        self.has_pending = True

    async def _run_tool_step(self, step_text, ctx, title="", context="", stop_event=None, checkpoint=None):
        self.step_contexts.append(context)
        self.run_log.append((title, step_text))
        final, touched = self.script(step_text, self)
        for rel, content in touched.items():
            (self.root / rel).write_text(content, encoding="utf-8")
        self._step_touched = list(touched)
        if step_text in getattr(self, "no_actions", ()):              # крок, де виконавець не викликав жодного тула
            self._step_actions = []
            return final, []
        self._step_actions = [("run_shell", {"command": f"cmd{len(self.run_log)}"}, f"вивід {len(self.run_log)}")]
        return final, (["write_file -> ok"] if touched or final else [])


def make_plan(n=3, statuses=None):
    plan = TaskState(task="додати експорт")
    for i in range(1, n + 1):
        plan.add_step("llm", f"крок {i}")
    for i, st in (statuses or {}).items():
        plan.set_result(i, st, f"результат {i}")
    return plan


def run(state, plan, verdicts=None):
    """Запустити реальний _execute_steps із підміненою звіркою (check_consistency)."""
    seq = iter(verdicts or [{"ok": True}])
    real = W.check_consistency
    W.check_consistency = lambda p, c: next(seq, {"ok": True})
    try:
        execute = types.MethodType(W.State._execute_steps, state)
        return asyncio.run(execute(plan, str(state.root), client=None))
    finally:
        W.check_consistency = real


def main() -> None:
    real_store = projectmap.STORE_DIR
    projectmap.STORE_DIR = Path(tempfile.mkdtemp()) / "projects"     # профіль користувача не чіпаємо
    try:
        _main()
    finally:
        projectmap.STORE_DIR = real_store


def _main() -> None:
    root = Path(tempfile.mkdtemp())

    # 1) усе добре: кроки бачать результати попередніх + змінені файли; звірка проходить без повторів
    def ok_script(text, st):
        n = len(st.run_log)
        return f"готово {n}", {f"mod{n}.py": f"x = {n}\n"}
    st = FakeState(root, ok_script)
    plan = make_plan(3)
    out = run(st, plan)
    assert [s.status for s in plan.steps] == ["done"] * 3 and len(st.run_log) == 3, st.run_log
    assert "результат" in st.step_contexts[1] or "готово 1" in st.step_contexts[1], st.step_contexts[1]
    assert "готово 1" in st.step_contexts[2] and "готово 2" in st.step_contexts[2]       # крок 3 бачить кроки 1–2
    assert "mod1.py" in st.step_contexts[2] and "mod2.py" in st.step_contexts[2]         # …і змінені файли
    assert any("Звірка пройшла" in n for n in st.notes) and st.notes[-1] == "Готово ✓"
    assert not st.has_pending and "Звірка: ок" in out

    # 2) крок 2 «зациклився» -> план зупиняється, крок 3 НЕ запускається, план лишається для відновлення
    def stuck_script(text, st):
        n = len(st.run_log)
        return (ITER_LIMIT_MSG, {}) if n == 2 else (f"готово {n}", {f"s{n}.py": "y = 1\n"})
    st = FakeState(root, stuck_script)
    plan = make_plan(3)
    run(st, plan)
    assert [s.status for s in plan.steps] == ["done", "failed", "pending"] and len(st.run_log) == 2
    assert st.has_pending and st.pending_saved is plan
    assert any("Крок #2 не завершено" in n for n in st.notes) and "Готово ✓" not in st.notes

    # 3) відновлення: виконані кроки пропускаються, продовжуємо з невдалого
    def resume_script(text, st):
        return f"виправлено {len(st.run_log)}", {f"r{len(st.run_log)}.py": "z = 1\n"}
    st = FakeState(root, resume_script)
    run(st, plan)
    assert len(st.run_log) == 2 and st.run_log[0][1] == "крок 2", st.run_log          # лише кроки 2 і 3
    assert [s.status for s in plan.steps] == ["done"] * 3 and not st.has_pending
    assert any("Продовжую план з кроку #2" in n for n in st.notes)

    # 4) звірка ловить синтаксичну помилку -> повтор того кроку з уточненням -> виправлено
    def syntax_script(text, st):
        if "Уточнення після перевірки" in text:
            return "виправив", {"broken.py": "def f():\n    return 1\n"}
        return "готово", {"broken.py": "def f(:\n    return 1\n"} if len(st.run_log) == 1 else {}
    st = FakeState(root, syntax_script)
    plan = make_plan(2)
    out = run(st, plan)
    assert len(st.run_log) == 3 and "Уточнення після перевірки" in st.run_log[2][1], st.run_log
    assert "синтаксична помилка" in " ".join(st.notes) and "Звірка: ок" in out   # після повтору звірка чиста
    assert not st.has_pending

    # 5) LLM-звірка каже «крок 1 суперечить»: повтор кроку 1, потім ок (обмежено 2 раундами)
    def consist_script(text, st):
        return f"готово {len(st.run_log)}", {f"c{len(st.run_log)}.py": "q = 1\n"}
    st = FakeState(root, consist_script)
    plan = make_plan(2)
    run(st, plan, verdicts=[{"ok": False, "step_id": 1, "note": "імена не збігаються"}, {"ok": True}])
    assert len(st.run_log) == 3 and st.run_log[2][1].startswith("крок 1") and "імена не збігаються" in st.run_log[2][1]
    assert any("Звірка пройшла" in n for n in st.notes)

    # 6) нескінченна «неузгодженість» не крутить вічно: максимум 2 повтори
    st = FakeState(root, consist_script)
    plan = make_plan(2)
    run(st, plan, verdicts=[{"ok": False, "step_id": 2, "note": "ще не так"}] * 5)
    assert len(st.run_log) == 2 + 2, st.run_log

    # 7) планувальник після кроку 1 коригує невиконаний крок 2 і додає крок; у чаті — його «роздум»;
    #    крок 1 має точний звіт (команда → вивід), його бачить крок 2
    import json

    class Planner:
        def __init__(self, answers):
            self.answers, self.calls = list(answers), []

        def chat(self, messages, tools=None, profile=None, fmt=None):
            self.calls.append(messages[-1]["content"])
            return {"content": json.dumps(self.answers.pop(0) if self.answers else {"action": "continue", "comment": ""})}

    st = FakeState(root, ok_script)
    st.planner = Planner([{"action": "revise", "comment": "Крок 1 готовий (cmd1 → вивід 1). Уточнюю крок 2.",
                           "revise": [{"step_id": 2, "description": "НОВИЙ крок 2"}], "add_steps": ["доданий крок"]}])
    plan = make_plan(2)
    run(st, plan)
    assert [t for _, t in st.run_log][:3] == ["крок 1", "НОВИЙ крок 2", "доданий крок"], st.run_log
    assert any(n.startswith("💭 Крок 1 готовий") for n in st.notes) and plan.steps[0].report.startswith("$ cmd1")
    assert "$ cmd1" in st.planner.calls[0] and "вивід 1" in st.planner.calls[0]       # планувальник бачив точні факти
    assert "$ cmd1" in st.step_contexts[1]                                           # виконавець кроку 2 — теж
    assert [s.status for s in plan.steps] == ["done"] * 3

    # 8) крок невдалий, планувальник переписав його — повтор за новим описом; безкінечні переписування обмежені
    def flaky(text, st):
        return (ITER_LIMIT_MSG, {}) if text == "крок 1" else ("ок", {})
    st = FakeState(root, flaky)
    st.planner = Planner([{"action": "revise", "comment": "Не вийшло, пробую інакше.",
                           "revise": [{"step_id": 1, "description": "крок 1 інакше"}]}])
    plan = make_plan(2)
    run(st, plan)
    assert [t for _, t in st.run_log][:2] == ["крок 1", "крок 1 інакше"] and plan.steps[0].status == "done"

    def always_fail(text, st):
        return ITER_LIMIT_MSG, {}
    st = FakeState(root, always_fail)
    st.planner = Planner([{"action": "revise", "comment": "ще раз", "revise": [{"step_id": 1, "description": f"спроба {i}"}]}
                          for i in range(10)])
    plan = make_plan(2)
    run(st, plan)
    assert len(st.run_log) == 1 + W.review.MAX_REPLANS and plan.steps[0].status == "failed" and st.has_pending, st.run_log

    # 9) збій/сміття від планувальника не ламає виконання (просто «далі»)
    st = FakeState(root, ok_script)
    st.planner = Planner([])
    st.planner.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("модель лежить"))
    plan = make_plan(2)
    run(st, plan)
    assert [s.status for s in plan.steps] == ["done"] * 2

    # 10) планувальник каже stop після успішного кроку — зупиняємось і лишаємо план для відновлення
    st = FakeState(root, ok_script)
    st.planner = Planner([{"action": "stop", "comment": "Потрібне ваше рішення: який порт?"}])
    plan = make_plan(3)
    run(st, plan)
    assert len(st.run_log) == 1 and st.has_pending and "💭 Потрібне ваше рішення: який порт?" in st.notes
    # 11) КОМЕНТАР КОРИСТУВАЧА по ходу виконання: приходить під час кроку 1 → планувальник бачить його поруч зі звітом
    #     виконавця й коригує крок 2 (навіть коли ліміт коригувань вичерпано); коментар лишається у фактах плану
    CID = "sid-test"

    def note_script(text, st):
        if text == "крок 1":
            W._LIVE_NOTES.setdefault(CID, []).append({"text": "Ти пишеш не в ту теку, використай out/", "seen": False})
        return f"готово {len(st.run_log)}", {}

    st = FakeState(root, note_script)
    st.planner = Planner([{"action": "revise", "comment": "Врахував: пишу в out/.",
                           "revise": [{"step_id": 2, "description": "крок 2 у теку out/"}]}])
    plan = make_plan(3)
    run(st, plan)
    sent = st.planner.calls[0]
    assert "КОМЕНТАР КОРИСТУВАЧА" in sent and "не в ту теку" in sent and "$ cmd1" in sent      # і коментар, і факти виконавця
    assert [t for _, t in st.run_log][:2] == ["крок 1", "крок 2 у теку out/"], st.run_log
    assert any(n.startswith("💭 Врахував") for n in st.notes)
    assert "не в ту теку" in plan.facts["коментарі користувача"]
    assert CID not in W._LIVE_NOTES and CID not in W._PLAN_ACTIVE and not st.queued_text

    # 12) коментар під час ОСТАННЬОГО кроку (раніше перегляду не було б) → планувальник додає крок-виправлення
    def last_script(text, st):
        if text == "крок 1":
            W._LIVE_NOTES.setdefault(CID, []).append({"text": "додай ще README", "seen": False})
        return "ок", {}

    st = FakeState(root, last_script)
    st.planner = Planner([{"action": "revise", "comment": "Додаю README.", "add_steps": ["створити README"]}])
    plan = make_plan(1)
    run(st, plan)
    assert [t for _, t in st.run_log] == ["крок 1", "створити README"] and plan.steps[1].status == "done"

    # 13) коментар, що прийшов ПІСЛЯ останньої перевірки (під час звірки), не губиться: іде у звичайну чергу
    def verdicts():
        W._LIVE_NOTES.setdefault(CID, []).append({"text": "і ще одне пізніше", "seen": False})
        yield {"ok": True}

    st = FakeState(root, ok_script)
    plan = make_plan(1)
    real = W.check_consistency
    g = verdicts()
    W.check_consistency = lambda p, c: next(g, {"ok": True})
    try:
        asyncio.run(types.MethodType(W.State._execute_steps, st)(plan, str(st.root), client=None))
    finally:
        W.check_consistency = real
    assert st.queued_text == "і ще одне пізніше" and CID not in W._LIVE_NOTES and CID not in W._PLAN_ACTIVE

    # 14) крок БЕЗ жодної дії («виправ помилки, якщо є» — помилок нема): планувальник бачив факти й прийняв → не «невдача»
    def idle_script(text, st):
        return "Помилок немає, виправляти нічого.", {}

    st = FakeState(root, idle_script)
    st.no_actions = {"крок 2"}
    st.planner = Planner([{"action": "continue", "comment": ""},                       # після кроку 1
                          {"action": "continue", "comment": "Крок 2 не потребував дій — йдемо далі."}])
    plan = make_plan(3)
    run(st, plan)
    assert [s.status for s in plan.steps] == ["done"] * 3 and not st.has_pending, [s.status for s in plan.steps]
    assert not any("не завершено" in n for n in st.notes)

    # 15) без пояснення планувальника (збій/сміття) крок без дій лишається невдалим — вигадане виконання не проходить
    st = FakeState(root, idle_script)
    st.no_actions = {"крок 2"}
    st.planner = Planner([{"action": "continue", "comment": ""}, {"action": "continue", "comment": ""}])
    plan = make_plan(3)
    run(st, plan)
    assert [s.status for s in plan.steps] == ["done", "failed", "pending"] and st.has_pending

    # 16) а ліміт/зациклення планувальник «прийняти» не може навіть із поясненням
    def limit_script(text, st):
        return ITER_LIMIT_MSG, {}

    st = FakeState(root, limit_script)
    st.no_actions = {"крок 1"}
    st.planner = Planner([{"action": "continue", "comment": "ну нехай."}])
    plan = make_plan(2)
    run(st, plan)
    assert plan.steps[0].status == "failed" and st.has_pending
    print("OK: orchestration_flow — результати між кроками, зупинка/відновлення, звірка і обмежені повтори")


if __name__ == "__main__":
    main()
