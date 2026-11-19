"""Перегляд плану після кроку (agent/stepreview.py): точний звіт про команди + рішення планувальника."""
import json

from agent import stepreview as R
from agent.memory import TaskState, render_for_planner, state_from_dict
from dataclasses import asdict


class Stub:
    def __init__(self, content):
        self.content, self.seen = content, None

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.seen = messages
        return {"content": self.content}


def make_plan():
    p = TaskState(task="зібрати проєкт")
    for t in ("встановити залежності", "запустити тести", "зібрати exe"):
        p.add_step("llm", t)
    return p


def main() -> None:
    # 1) звіт: команда + її ТОЧНИЙ вивід + код завершення; файли; прочитане
    rep = R.facts_report([
        ("read_file", {"path": "a.py"}, "вміст"),
        ("run_shell", {"command": "pytest -q"}, "3 passed in 0.1s"),
        ("run_shell", {"command": "pip install x"}, "ERROR: no matching distribution\n[код завершення: 1]"),
        ("write_file", {"path": "b.py", "content": "x"}, "записано b.py (1 символ)"),
    ])
    assert "$ pytest -q\n3 passed in 0.1s" in rep, rep
    assert "$ pip install x (код завершення 1)\nERROR: no matching distribution" in rep, rep
    assert "[код завершення" not in rep and "✎ write_file b.py" in rep and "Прочитано: a.py" in rep

    # 2) довгий вивід скорочується (початок+кінець), увесь звіт обмежений
    big = "\n".join(f"рядок {i}" for i in range(2000))
    rep = R.facts_report([("run_shell", {"command": "build"}, big)])
    assert "рядок 0" in rep and "рядок 1999" in rep and "пропущено" in rep and len(rep) < 1200
    many = R.facts_report([("run_shell", {"command": f"c{i}"}, "x" * 400) for i in range(30)])
    assert len(many) <= R.REPORT_MAX + 60 and "c29" in many

    # 3) парсер: сміття → continue; невідома дія → continue; revise розбирається
    assert R.parse_decision("не json").action == "continue"
    assert R.parse_decision('{"action": "щось"}').action == "continue"
    d = R.parse_decision(json.dumps({"action": "revise", "comment": "Тести впали: 1 failed.",
                                     "revise": [{"step_id": 3, "description": "спершу виправити тест"}, {"x": 1}],
                                     "add_steps": ["виправити тест", ""]}))
    assert d.action == "revise" and d.revise == {3: "спершу виправити тест"} and d.add_steps == ["виправити тест"]

    # 4) apply: виконані кроки не чіпаємо; невиконані міняємо; невдалий поточний -> pending; add_steps додає
    p = make_plan()
    p.set_result(1, "done", "ok")
    p.set_result(2, "failed", "тести впали")
    dec = R.Decision("revise", "c", {1: "ЗЛАМАТИ", 2: "інакше: pytest -x", 3: "зібрати exe після тестів"}, ["прибрати tmp"])
    assert R.apply_decision(p, dec, current_id=2, allow_change=True)
    assert p.steps[0].description == "встановити залежності"              # done не змінюється
    assert p.steps[1].description == "інакше: pytest -x" and p.steps[1].status == "pending"
    assert p.steps[2].description == "зібрати exe після тестів" and p.steps[3].description == "прибрати tmp"
    assert not R.apply_decision(make_plan(), dec, 1, allow_change=False)    # ліміт коригувань вичерпано
    assert not R.apply_decision(make_plan(), R.Decision("continue", "ок"), 1, True)

    # 5) review_step: модель отримує ТОЧНІ факти (команда й вивід), а не лише опис; JSON-відповідь розбирається
    p = make_plan()
    s = p.steps[1]
    rep = R.facts_report([("run_shell", {"command": "pytest -q"}, "1 failed, 2 passed\n[код завершення: 1]")])
    stub = Stub(json.dumps({"action": "continue", "comment": "Тест впав, але це очікувано — далі."}))
    dec = R.review_step(render_for_planner(p), s, False, "тести червоні", rep, stub)
    sent = stub.seen[-1]["content"]
    assert "$ pytest -q (код завершення 1)" in sent and "1 failed, 2 passed" in sent and "НЕ ЗАВЕРШЕНО" in sent
    assert dec.action == "continue" and "Тест впав" in dec.comment

    # 6) факти кроку видно в зрізі плану (наступні кроки й планувальник бачать, що саме виконано), і вони переживають збереження
    p.steps[1].report = rep
    assert "факти:" in render_for_planner(p) and "$ pytest -q" in render_for_planner(p)
    back = state_from_dict(asdict(p))
    assert back.steps[1].report == rep
    old = asdict(p)
    for st in old["steps"]:
        st.pop("report")                                                    # сесії старого формату
    assert state_from_dict(old).steps[1].report == ""
    # звірка узгодженості плану (перенесена з прототипу hierarchical): ok / вказаний крок / зіпсований JSON → не зациклюємось
    from agent.stepreview import check_consistency
    from agent.memory import TaskState

    class Cl:
        def __init__(self, c):
            self.c = c

        def chat(self, messages, tools=None, profile=None, fmt=None):
            return {"content": self.c}
    st = TaskState(task="t")
    st.add_step("llm", "a")
    assert check_consistency(st, Cl('{"ok": true}')) == {"ok": True, "step_id": None, "note": ""}
    r = check_consistency(st, Cl('{"ok": false, "step_id": 2, "note": "перероби"}'))
    assert r == {"ok": False, "step_id": 2, "note": "перероби"}
    assert check_consistency(st, Cl("не json"))["ok"] is True
    print("OK: stepreview — точні факти команд, парсинг/застосування рішень, збереження звіту")


if __name__ == "__main__":
    main()
