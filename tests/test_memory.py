"""Тест дискового стану задачі (без Ollama).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_memory
"""
import tempfile

from agent.memory import TaskState, load, render_for_planner, render_for_user, save


def main() -> None:
    st = TaskState(task="Рефакторити parser.py")
    s1 = st.add_step("llm", "Переписати get_app_list ітеративно", target="parser.py")
    st.add_step("deterministic", "Повернути літерал BODY_1", target="parser.py")
    st.set_result(s1.id, "done", "рекурсію замінено на while, -8 рядків")
    st.facts["BODY_1"] = "parser.py:25"

    d = tempfile.mkdtemp()
    p = save(st, root=d)
    assert p.exists()

    st2 = load(root=d)
    assert st2.task == st.task
    assert len(st2.steps) == 2
    assert st2.steps[0].status == "done"
    assert st2.steps[0].result.startswith("рекурсію")
    assert st2.facts["BODY_1"] == "parser.py:25"
    assert st2.next_pending().id == 2, "наступний pending має бути крок 2"

    rendered = render_for_planner(st2)
    assert "Задача:" in rendered
    assert "#1" in rendered and "#2" in rendered
    assert "BODY_1" in rendered

    # показ користувачу: кожен крок — окремий рядок списку, «1) … 2) …» — підпункти
    ui = render_for_user(st2)
    lines = ui.splitlines()
    assert any(l.startswith("✅ **#1**") for l in lines) and any(l.startswith("⬜ **#2**") for l in lines), ui
    st2.steps[1].description = "Зробити: 1) перше 2) друге 3) третє"
    ui = render_for_user(st2)
    assert "    - перше" in ui and "    - друге" in ui and "    - третє" in ui, ui
    assert "\n\n" in ui                                  # порожні рядки між пунктами — інакше markdown склеїть

    print("OK: стан задачі зберігається/завантажується, рендер компактний\n")
    print(rendered)


if __name__ == "__main__":
    main()
