"""Керуючий процес код-режиму: реальний agent.plan_runner.PlanRunner (ядро агента, без UI) на заглушці PlanUI.
Кроки «виконує» скриптова функція (модель не потрібна), сесії на диску не чіпаємо.

Перевіряємо: кроки бачать результати попередніх; невдалий крок зупиняє план і лишає його для
відновлення; відновлення пропускає виконані кроки; звірка ловить синтаксичну помилку й повторює
крок; успішна звірка нічого не повторює; перегляд планувальником і коментарі користувача по ходу; обгортка
State._execute_steps відправляє пізні коментарі у звичайну чергу."""
import asyncio
import json
import tempfile
import types
from pathlib import Path

import lca_web.state as W
from agent import limits, projectmap
from agent import plan_runner as PR
from agent import stepreview as review
from agent.agent_loop import ITER_LIMIT_MSG
from agent.memory import TaskState

limits.FORCE = limits.production()
CID = "sid-test"


class FakeUI:
    """PlanUI: повідомлення — у notes, очікуваний план — у pending_saved, крок — скрипт."""
    def __init__(self, root: Path, script):
        self.root, self.script = root, script
        self.stop_event = None
        self.notes: list[str] = []
        self.pending_saved = None
        self.has_pending = False
        self.step_contexts: list[str] = []
        self.run_log: list[tuple[str, str]] = []
        self.no_actions: set = set()

    async def status(self, text):
        pass

    async def say(self, content, kind="note"):
        self.notes.append(content)

    async def save_pending(self, plan):
        self.pending_saved = plan
        self.has_pending = True

    def take_live_notes(self):
        return W._take_live_notes(CID)

    async def run_step(self, step_text, *, title, context, checkpoint=None):
        self.step_contexts.append(context)
        self.run_log.append((title, step_text))
        final, touched = self.script(step_text, self)
        for rel, content in touched.items():
            (self.root / rel).write_text(content, encoding="utf-8")
        if step_text in self.no_actions:                          # крок, де виконавець не викликав жодного тула
            return PR.StepRun(final, [], list(touched), [])
        actions = [("run_shell", {"command": f"cmd{len(self.run_log)}"}, f"вивід {len(self.run_log)}")]
        return PR.StepRun(final, ["write_file -> ok"] if touched or final else [], list(touched), actions)


def make_plan(n=3, statuses=None):
    plan = TaskState(task="додати експорт")
    for i in range(1, n + 1):
        plan.add_step("act", f"крок {i}")
    for i, st in (statuses or {}).items():
        plan.set_result(i, st, f"результат {i}")
    return plan


def run(ui, plan, verdicts=None, client=None, autonomous=False):
    """Запустити реальний PlanRunner.execute із підміненою звіркою (check_consistency)."""
    seq = iter(verdicts or [{"ok": True}])
    real = PR.check_consistency
    PR.check_consistency = lambda p, c: next(seq, {"ok": True})
    try:
        env = PR.PlanEnv(root=str(ui.root), autonomous=autonomous)
        return asyncio.run(PR.PlanRunner(ui, client, env).execute(plan))
    finally:
        PR.check_consistency = real


class Planner:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.calls.append(messages[-1]["content"])
        return {"content": json.dumps(self.answers.pop(0) if self.answers else {"action": "continue", "comment": ""})}


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
    def ok_script(text, ui):
        n = len(ui.run_log)
        return f"готово {n}", {f"mod{n}.py": f"x = {n}\n"}
    ui = FakeUI(root, ok_script)
    plan = make_plan(3)
    out = run(ui, plan)
    assert [s.status for s in plan.steps] == ["done"] * 3 and len(ui.run_log) == 3, ui.run_log
    assert "готово 1" in ui.step_contexts[1], ui.step_contexts[1]
    assert "САМОСТІЙНИХ ПРАВОК" not in ui.step_contexts[1]                          # режим ask — без нотатки автономності
    # контекст кроку: стабільне на початку, змінне (план із результатами) — наприкінці (кеш префікса)
    c = ui.step_contexts[2]
    assert c.index("Project root") < c.index("Загальна задача") and c.rstrip().endswith("змінені файли: mod1.py, mod2.py"), c[-200:]
    # режим «правки: auto»: у контексті кроку з'являється вказівка діяти самостійно (виправляти, а не лише описувати)
    ui_auto = FakeUI(root, ok_script)
    run(ui_auto, make_plan(2), autonomous=True)
    assert all("САМОСТІЙНИХ ПРАВОК" in c and "ВИПРАВ" in c for c in ui_auto.step_contexts), ui_auto.step_contexts[0][:200]
    assert "готово 1" in ui.step_contexts[2] and "готово 2" in ui.step_contexts[2]       # крок 3 бачить кроки 1–2
    assert "mod1.py" in ui.step_contexts[2] and "mod2.py" in ui.step_contexts[2]         # …і змінені файли
    assert any("Звірка пройшла" in n for n in ui.notes) and ui.notes[-1] == "Готово ✓"
    assert any(n.startswith("**Підсумок виконання**") for n in ui.notes)                 # що зроблено — не лише «Готово»
    assert not ui.has_pending and "Звірка: ок" in out

    # 2) крок 2 «зациклився» -> план зупиняється, крок 3 НЕ запускається, план лишається для відновлення
    def stuck_script(text, ui):
        n = len(ui.run_log)
        return (ITER_LIMIT_MSG, {}) if n == 2 else (f"готово {n}", {f"s{n}.py": "y = 1\n"})
    ui = FakeUI(root, stuck_script)
    plan = make_plan(3)
    run(ui, plan)
    assert [s.status for s in plan.steps] == ["done", "failed", "pending"] and len(ui.run_log) == 2
    assert ui.has_pending and ui.pending_saved is plan
    assert any("Крок #2 не завершено" in n for n in ui.notes) and "Готово ✓" not in ui.notes

    # 3) відновлення: виконані кроки пропускаються, продовжуємо з невдалого
    def resume_script(text, ui):
        return f"виправлено {len(ui.run_log)}", {f"r{len(ui.run_log)}.py": "z = 1\n"}
    ui = FakeUI(root, resume_script)
    run(ui, plan)
    assert len(ui.run_log) == 2 and ui.run_log[0][1] == "крок 2", ui.run_log          # лише кроки 2 і 3
    assert [s.status for s in plan.steps] == ["done"] * 3 and not ui.has_pending
    assert any("Продовжую план з кроку #2" in n for n in ui.notes)

    # 4) звірка ловить синтаксичну помилку -> повтор того кроку з уточненням -> виправлено
    def syntax_script(text, ui):
        if "Уточнення після перевірки" in text:
            return "виправив", {"broken.py": "def f():\n    return 1\n"}
        return "готово", {"broken.py": "def f(:\n    return 1\n"} if len(ui.run_log) == 1 else {}
    ui = FakeUI(root, syntax_script)
    out = run(ui, make_plan(2))
    assert len(ui.run_log) == 3 and "Уточнення після перевірки" in ui.run_log[2][1], ui.run_log
    assert "синтаксична помилка" in " ".join(ui.notes) and "Звірка: ок" in out   # після повтору звірка чиста
    assert not ui.has_pending

    # 5) LLM-звірка каже «крок 1 суперечить»: повтор кроку 1, потім ок (обмежено 2 раундами)
    def consist_script(text, ui):
        return f"готово {len(ui.run_log)}", {f"c{len(ui.run_log)}.py": "q = 1\n"}
    ui = FakeUI(root, consist_script)
    run(ui, make_plan(2), verdicts=[{"ok": False, "step_id": 1, "note": "імена не збігаються"}, {"ok": True}])
    assert len(ui.run_log) == 3 and ui.run_log[2][1].startswith("крок 1") and "імена не збігаються" in ui.run_log[2][1]
    assert any("Звірка пройшла" in n for n in ui.notes)

    # 6) нескінченна «неузгодженість» не крутить вічно: максимум 2 повтори
    ui = FakeUI(root, consist_script)
    run(ui, make_plan(2), verdicts=[{"ok": False, "step_id": 2, "note": "ще не так"}] * 5)
    assert len(ui.run_log) == 2 + 2, ui.run_log

    # 7) планувальник після кроку 1 коригує невиконаний крок 2 і додає крок; у чаті — його «роздум»;
    #    крок 1 має точний звіт (команда → вивід), його бачить крок 2
    ui = FakeUI(root, ok_script)
    planner = Planner([{"action": "revise", "comment": "Крок 1 готовий (cmd1 → вивід 1). Уточнюю крок 2.",
                        "revise": [{"step_id": 2, "description": "НОВИЙ крок 2"}], "add_steps": ["доданий крок"]}])
    plan = make_plan(2)
    run(ui, plan, client=planner)
    assert [t for _, t in ui.run_log][:3] == ["крок 1", "НОВИЙ крок 2", "доданий крок"], ui.run_log
    assert any(n.startswith("💭 Крок 1 готовий") for n in ui.notes) and plan.steps[0].report.startswith("$ cmd1")
    assert "$ cmd1" in planner.calls[0] and "вивід 1" in planner.calls[0]       # планувальник бачив точні факти
    assert "$ cmd1" in ui.step_contexts[1]                                       # виконавець кроку 2 — теж
    assert [s.status for s in plan.steps] == ["done"] * 3 and plan.steps[2].kind == "act"

    # 8) крок невдалий, планувальник переписав його — повтор за новим описом; безкінечні переписування обмежені
    def flaky(text, ui):
        return (ITER_LIMIT_MSG, {}) if text == "крок 1" else ("ок", {})
    ui = FakeUI(root, flaky)
    plan = make_plan(2)
    run(ui, plan, client=Planner([{"action": "revise", "comment": "Не вийшло, пробую інакше.",
                                   "revise": [{"step_id": 1, "description": "крок 1 інакше"}]}]))
    assert [t for _, t in ui.run_log][:2] == ["крок 1", "крок 1 інакше"] and plan.steps[0].status == "done"

    def always_fail(text, ui):
        return ITER_LIMIT_MSG, {}
    ui = FakeUI(root, always_fail)
    plan = make_plan(2)
    run(ui, plan, client=Planner([{"action": "revise", "comment": "ще раз",
                                   "revise": [{"step_id": 1, "description": f"спроба {i}"}]} for i in range(10)]))
    assert len(ui.run_log) == 1 + review.MAX_REPLANS and plan.steps[0].status == "failed" and ui.has_pending, ui.run_log

    # 9) збій/сміття від планувальника не ламає виконання (просто «далі»)
    class Down:
        def chat(self, *a, **k):
            raise RuntimeError("модель лежить")
    ui = FakeUI(root, ok_script)
    plan = make_plan(2)
    run(ui, plan, client=Down())
    assert [s.status for s in plan.steps] == ["done"] * 2

    # 10) планувальник каже stop після успішного кроку — зупиняємось і лишаємо план для відновлення
    ui = FakeUI(root, ok_script)
    run(ui, make_plan(3), client=Planner([{"action": "stop", "comment": "Потрібне ваше рішення: який порт?"}]))
    assert len(ui.run_log) == 1 and ui.has_pending and "💭 Потрібне ваше рішення: який порт?" in ui.notes

    # 11) КОМЕНТАР КОРИСТУВАЧА по ходу виконання: приходить під час кроку 1 → планувальник бачить його поруч зі звітом
    #     виконавця й коригує крок 2 (навіть коли ліміт коригувань вичерпано); коментар лишається у фактах плану
    def note_script(text, ui):
        if text == "крок 1":
            W._LIVE_NOTES.setdefault(CID, []).append({"text": "Ти пишеш не в ту теку, використай out/", "seen": False})
        return f"готово {len(ui.run_log)}", {}
    ui = FakeUI(root, note_script)
    planner = Planner([{"action": "revise", "comment": "Врахував: пишу в out/.",
                        "revise": [{"step_id": 2, "description": "крок 2 у теку out/"}]}])
    plan = make_plan(3)
    run(ui, plan, client=planner)
    sent = planner.calls[0]
    assert "КОМЕНТАР КОРИСТУВАЧА" in sent and "не в ту теку" in sent and "$ cmd1" in sent      # і коментар, і факти виконавця
    assert [t for _, t in ui.run_log][:2] == ["крок 1", "крок 2 у теку out/"], ui.run_log
    assert any(n.startswith("💭 Врахував") for n in ui.notes)
    assert "не в ту теку" in plan.facts["коментарі користувача"]
    assert CID not in W._LIVE_NOTES

    # 12) коментар під час ОСТАННЬОГО кроку (раніше перегляду не було б) → планувальник додає крок-виправлення
    def last_script(text, ui):
        if text == "крок 1":
            W._LIVE_NOTES.setdefault(CID, []).append({"text": "додай ще README", "seen": False})
        return "ок", {}
    ui = FakeUI(root, last_script)
    plan = make_plan(1)
    run(ui, plan, client=Planner([{"action": "revise", "comment": "Додаю README.", "add_steps": ["створити README"]}]))
    assert [t for _, t in ui.run_log] == ["крок 1", "створити README"] and plan.steps[1].status == "done"

    # 13) обгортка UI (State._execute_steps): коментар, що прийшов ПІСЛЯ останньої перевірки, не губиться — іде у чергу
    class Wrap:
        current_id, queued_text = CID, ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def _execute_steps_inner(self, plan, root_, client):
            W._LIVE_NOTES.setdefault(CID, []).append({"text": "і ще одне пізніше", "seen": False})
            assert CID in W._PLAN_ACTIVE                                        # поки план іде — коментар піде планувальнику
            return "ок"
    w = Wrap()
    asyncio.run(types.MethodType(W.State._execute_steps, w)(make_plan(1), str(root), None))
    assert w.queued_text == "і ще одне пізніше" and CID not in W._LIVE_NOTES and CID not in W._PLAN_ACTIVE

    # 14) крок БЕЗ жодної дії («виправ помилки, якщо є» — помилок нема): планувальник бачив факти й прийняв → не «невдача»
    def idle_script(text, ui):
        return "Помилок немає, виправляти нічого.", {}
    ui = FakeUI(root, idle_script)
    ui.no_actions = {"крок 2"}
    plan = make_plan(3)
    run(ui, plan, client=Planner([{"action": "continue", "comment": ""},                       # після кроку 1
                                  {"action": "continue", "comment": "Крок 2 не потребував дій — йдемо далі."}]))
    assert [s.status for s in plan.steps] == ["done"] * 3 and not ui.has_pending, [s.status for s in plan.steps]
    assert not any("не завершено" in n for n in ui.notes)

    # 15) без пояснення планувальника (збій/сміття) крок без дій лишається невдалим — вигадане виконання не проходить
    ui = FakeUI(root, idle_script)
    ui.no_actions = {"крок 2"}
    plan = make_plan(3)
    run(ui, plan, client=Planner([{"action": "continue", "comment": ""}, {"action": "continue", "comment": ""}]))
    assert [s.status for s in plan.steps] == ["done", "failed", "pending"] and ui.has_pending

    # 16) а ліміт/зациклення планувальник «прийняти» не може навіть із поясненням
    def limit_script(text, ui):
        return ITER_LIMIT_MSG, {}
    ui = FakeUI(root, limit_script)
    ui.no_actions = {"крок 1"}
    plan = make_plan(2)
    run(ui, plan, client=Planner([{"action": "continue", "comment": "ну нехай."}]))
    assert plan.steps[0].status == "failed" and ui.has_pending

    # 17) примітка (note) не виконується; стоп між кроками лишає план для відновлення
    ui = FakeUI(root, ok_script)
    plan = make_plan(2)
    plan.add_step("note", "просто примітка")
    run(ui, plan)
    assert len(ui.run_log) == 2 and plan.steps[2].status == "done"
    import threading
    ui = FakeUI(root, ok_script)
    ui.stop_event = threading.Event()
    ui.stop_event.set()
    plan = make_plan(2)
    run(ui, plan)
    assert ui.run_log == [] and ui.has_pending and ui.notes[-1].startswith("⛔ Зупинено")
    print("OK: orchestration_flow — PlanRunner: результати між кроками, зупинка/відновлення, звірка й обмежені повтори, "
          "перегляд планувальником, коментарі по ходу, примітки, стоп")


if __name__ == "__main__":
    main()
