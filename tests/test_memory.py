"""Тест стану задачі (без Ollama): план зберігається в сесії й відновлюється, рендер компактний.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_memory
"""
from dataclasses import asdict

from agent.memory import Step, TaskState, render_for_planner, render_for_user, state_from_dict


def main() -> None:
    st = TaskState(task="Рефакторити parser.py")
    s1 = st.add_step("act", "Переписати get_app_list ітеративно", target="parser.py")
    st.add_step("act", "Повернути літерал BODY_1", target="parser.py")
    st.set_result(s1.id, "done", "рекурсію замінено на while, -8 рядків")
    st.facts["BODY_1"] = "parser.py:25"

    st2 = state_from_dict(asdict(st))                        # так план лежить у сесії (pending_plan) і відновлюється
    assert st2.task == st.task
    assert len(st2.steps) == 2
    assert st2.steps[0].status == "done"
    assert st2.steps[0].result.startswith("рекурсію")
    assert st2.facts["BODY_1"] == "parser.py:25"
    assert st2.next_pending().id == 2, "наступний pending має бути крок 2"

    rendered = render_for_planner(st2)
    assert "Задача:" in rendered
    assert "#1" in rendered and "#2" in rendered
    assert "BODY_1" in rendered and "[act]" not in rendered      # тип кроку моделі не показуємо (лише «примітка»)

    # показ користувачу: кожен крок — окремий рядок списку, «1) … 2) …» — підпункти
    ui = render_for_user(st2)
    lines = ui.splitlines()
    assert any(l.startswith("✅ **#1**") for l in lines) and any(l.startswith("⬜ **#2**") for l in lines), ui
    st2.steps[1].description = "Зробити: 1) перше 2) друге 3) третє"
    ui = render_for_user(st2)
    assert "    - перше" in ui and "    - друге" in ui and "    - третє" in ui, ui
    assert "\n\n" in ui                                  # порожні рядки між пунктами — інакше markdown склеїть

    # примітка без цілі — нічого виконувати, АЛЕ якщо в описі дієслово зміни — це реальна правка, виконуємо
    for kind in ("note", "inline"):                      # inline — тип зі старих збережених планів
        assert Step(1, kind, "Подумати над назвою").is_noop
        assert not Step(1, kind, "Додати імпорт mask_long на початку sessionmem.py").is_noop
        assert not Step(1, kind, "Виправити помилку у функції").is_noop and not Step(1, kind, "x", target="a.py").is_noop
    assert not Step(1, "act", "Подумати над назвою").is_noop and not Step(1, "llm", "Подумати").is_noop
    note_plan = TaskState(task="t")
    note_plan.add_step("note", "Подумати над назвою")
    assert "📝 **#1**" in render_for_user(note_plan) and "[примітка]" in render_for_planner(note_plan)
    print("OK: стан задачі відновлюється з сесії, рендер компактний, примітки не виконуються\n")
    print(rendered)


if __name__ == "__main__":
    main()
