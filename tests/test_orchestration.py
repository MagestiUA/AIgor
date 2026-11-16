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
    assert "модуль export.py" in o.context_for_intent(last) and o.context_for_intent("") == ""
    assert o.context_for_intent("x" * 5000).endswith("…")

    # B: великий план = ≥4 кроків, що виконують дії (inline не рахуємо)
    st = TaskState(task="t")
    for i in range(3):
        st.add_step("llm", f"крок {i}")
    assert not o.is_big_plan(st)
    st.add_step("inline", "нотатка")
    assert not o.is_big_plan(st)
    st.add_step("deterministic", "тести")
    assert o.is_big_plan(st) and not o.is_big_plan(st, threshold=5)

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
    print("OK: orchestration — згода/розгортання, великий план, змінені файли, синтаксис, вибір кроку")


if __name__ == "__main__":
    main()
