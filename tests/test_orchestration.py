"""agent/orchestration.py: розпізнавання підтвердження, розгортання, великий план, перевірка файлів."""
import tempfile
from pathlib import Path

from agent import orchestration as o
from agent.memory import TaskState


def main() -> None:
    # A: підтвердження
    for yes in ("роби", "Роби!", "давай", "так, роби", "давай роби це", "ок", "OK, do it", "зроби будь ласка",
                "погоджуюсь", "+", "Так."):
        assert o.is_confirmation(yes), yes
    for no in ("ні, не роби", "роби тести", "зроби окремий модуль для логування", "", "   ", "а що якщо інакше",
               "давай спочатку подумаємо ще раз про це", "так не можна"):
        assert not o.is_confirmation(no), no

    msgs = [
        {"role": "user", "content": "чи можна додати експорт у CSV?", "kind": "text"},
        {"role": "assistant", "content": "хід", "kind": "step"},
        {"role": "assistant", "content": "Так, можна: 1) модуль export.py 2) кнопка в UI 3) тести.", "kind": "answer"},
        {"role": "assistant", "content": "службова нотатка", "kind": "note"},
        {"role": "user", "content": "роби", "kind": "text"},
    ]
    last = o.last_assistant_text(msgs)
    assert last.startswith("Так, можна: 1) модуль export.py"), last
    assert o.last_assistant_text([{"role": "user", "content": "привіт"}]) == ""
    ex = o.expand_confirmation("роби", last)
    assert "модуль export.py" in ex and "«роби»" in ex and ex.startswith("Виконай те, що ти щойно запропонував")
    assert o.expand_confirmation("роби", "") is None                    # нічого підтверджувати
    assert o.expand_confirmation("роби тести", last) is None            # це нова задача, не згода

    # B: великий план = ≥4 кроків, що виконують дії (примітки не рахуємо)
    st = TaskState(task="t")
    for i in range(3):
        st.add_step("act", f"крок {i}")
    assert not o.is_big_plan(st)
    st.add_step("note", "нотатка")
    assert not o.is_big_plan(st)
    st.add_step("act", "тести")
    assert o.is_big_plan(st) and not o.is_big_plan(st, threshold=5)

    # примітка З файлом-ціллю — це правка, а не нотатка: виконується (регресія: «план без жодного запису»)
    s_note = st.steps[3]
    assert s_note.is_noop
    st.add_step("note", "додати EXPOSE 8080", "Dockerfile")
    s_edit = st.steps[-1]
    assert s_edit.target == "Dockerfile" and not s_edit.is_noop
    assert o.is_big_plan(st, threshold=5)

    # C: що змінив крок, перевірка синтаксису, вибір кроку для повтору
    assert o.touched_paths("write_file", {"path": "a.py"}) == ["a.py"]
    assert o.touched_paths("edit_file", {"path": "b.py", "instruction": "x"}) == ["b.py"]
    assert o.touched_paths("create_from_source", {"target": "c.py", "source": "s.py"}) == ["c.py"]
    assert o.touched_paths("read_file", {"path": "a.py"}) == [] and o.touched_paths("write_file", {}) == []

    root = Path(tempfile.mkdtemp())
    (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (root / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    (root / "notes.md").write_text("def broken(:", encoding="utf-8")
    rep = o.syntax_report(root, ["ok.py", "bad.py", "notes.md", "нема.py"])
    assert [p for p, _ in rep] == ["bad.py"] and "рядок" in rep[0][1], rep
    tb = {1: ["ok.py"], 2: ["bad.py"], 3: ["bad.py", "x.py"]}
    assert o.pick_step(tb, ["bad.py"]) == 3                     # останній, що чіпав файл
    assert o.pick_step(tb, ["інше.py"]) == 3                    # fallback — останній крок зі змінами
    assert o.pick_step({}, ["a.py"]) is None and o.pick_step({}, ["a.py"], fallback=2) == 2
    # фідбек b_0.0.7: «ок, тоді додай» має бути згодою; «додай тести» — новою задачею
    assert o.is_confirmation("ок, тоді додай") and o.is_confirmation("додай") and o.is_confirmation("так, додаємо")
    assert not o.is_confirmation("додай тести") and not o.is_confirmation("ні, не додавай")
    assert "РЕКОМЕНДОВАНИЙ" in o.expand_confirmation("ок, тоді додай", "Варіант 3 рекомендовано")

    # питання про можливість — обговорення, а ввічливе прохання — завдання
    assert o.looks_like_question("чи можна додати якийсь хелс-чек сервіс, аби при роботі в кластері... чи десь завис?")
    assert o.looks_like_question("Як це працює?") and o.looks_like_question("Is it possible to add logging?")
    assert not o.looks_like_question("чи можеш додати тести?") and not o.looks_like_question("Can you add tests?")
    assert not o.looks_like_question("додай тести?") and not o.looks_like_question("чи можна додати")   # без «?»
    assert o.wants_discussion("це було просто питання чи можна це зробити?")
    assert o.wants_discussion("просто питання без вимоги відразу писати код") and o.wants_discussion("Не пиши код поки")
    assert not o.wants_discussion("додай хелс-чек") and not o.wants_discussion("роби")

    # недавній діалог (тепер convo.dialogue_tail): без поточного повідомлення, без службових нотаток, з обрізанням
    from agent import convo
    dm = [{"role": "user", "content": "q1", "kind": "text"},
          {"role": "assistant", "content": "a1 " + "x" * 3000, "kind": "answer"},
          {"role": "assistant", "content": "нотатка", "kind": "note"},
          {"role": "user", "content": "q2", "kind": "text"}]
    rd = convo.dialogue_tail(dm)
    assert "Користувач: q1" in rd and "Асистент: a1" in rd and "…" in rd
    assert "q2" not in rd and "нотатка" not in rd
    assert convo.dialogue_tail([{"role": "user", "content": "лише поточне"}]) == ""
    assert o.touched_paths("replace_in_file", {"path": "r.py"}) == ["r.py"]
    print("OK: orchestration — згода/розгортання, великий план, змінені файли, синтаксис, вибір кроку")


if __name__ == "__main__":
    main()
