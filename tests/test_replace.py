"""Тул replace_in_file: пряма точкова заміна без вкладеного виклику моделі. Тимчасова тека, моделі немає."""
import tempfile
from pathlib import Path

from agent import limits
from agent.orchestration import touched_paths
from agent.stepreview import facts_report
from agent.toolkit import ToolContext, default_registry

limits.FORCE = limits.production()


def main() -> None:
    d = Path(tempfile.mkdtemp())
    reg = default_registry()
    auto = ToolContext(root=d, permissions={"edits": "auto", "shell": "off"})
    f = d / "m.py"
    f.write_bytes(b"def a():\n    return 1\n\n\ndef b():\n    return 1\n")

    # точний єдиний збіг
    r = reg.dispatch("replace_in_file", {"path": "m.py", "old": "def a():\n    return 1", "new": "def a():\n    return 2"}, auto)
    assert r.startswith("замінено в m.py") and "+    return 2" in r, r
    assert f.read_bytes() == b"def a():\n    return 2\n\n\ndef b():\n    return 1\n"
    # неоднозначний збіг — відмова з порадою
    r = reg.dispatch("replace_in_file", {"path": "m.py", "old": "    return", "new": "    yield"}, auto)
    assert "трапляється 2 разів" in r and b"yield" not in f.read_bytes(), r
    # відступи моделі не збіглися (2 пробіли замість 4) — збіг цілих рядків без країв, стиль файлу зберігся
    r = reg.dispatch("replace_in_file", {"path": "m.py", "old": "def b():\n  return 1\n", "new": "def b():\n  return 3\n"}, auto)
    assert r.startswith("замінено"), r
    assert f.read_bytes().endswith(b"def b():\n    return 3\n"), f.read_bytes()
    # не знайдено — підказка «схоже місце», файл не змінено
    before = f.read_bytes()
    r = reg.dispatch("replace_in_file", {"path": "m.py", "old": "def bb():\n    return 3", "new": "x"}, auto)
    assert r.startswith("помилка: old не знайдено") and "Схоже місце" in r and f.read_bytes() == before, r
    # синтаксична помилка після заміни — відмова
    r = reg.dispatch("replace_in_file", {"path": "m.py", "old": "def a():", "new": "def a(:"}, auto)
    assert "синтаксична помилка" in r and f.read_bytes() == before, r
    # видалення фрагмента (new порожній) і порожній old
    r = reg.dispatch("replace_in_file", {"path": "m.py", "old": "\n\ndef b():\n    return 3\n", "new": ""}, auto)
    assert r.startswith("замінено") and b"def b" not in f.read_bytes(), r
    assert "old порожній" in reg.dispatch("replace_in_file", {"path": "m.py", "old": " ", "new": "x"}, auto)
    assert "однакові" in reg.dispatch("replace_in_file", {"path": "m.py", "old": "def a", "new": "def a"}, auto)
    assert "write_file" in reg.dispatch("replace_in_file", {"path": "new.py", "old": "a", "new": "b"}, auto)
    # CRLF-файл лишається CRLF; модель передає \n
    c = d / "w.txt"
    c.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert reg.dispatch("replace_in_file", {"path": "w.txt", "old": "two\nthree", "new": "2\n3"}, auto).startswith("замінено")
    assert c.read_bytes() == b"one\r\n2\r\n3\r\n", c.read_bytes()
    # режим «правки: ask» без підтвердження — відхилено, файл не змінено
    ask = ToolContext(root=d, permissions={"edits": "ask", "shell": "off"}, confirm=lambda diff: False)
    assert reg.dispatch("replace_in_file", {"path": "w.txt", "old": "one", "new": "1"}, ask).startswith("відхилено")
    assert c.read_bytes() == b"one\r\n2\r\n3\r\n"
    # поза межами проєкту — заборонено
    assert "поза межами" in reg.dispatch("replace_in_file", {"path": str(d.parent / "x.txt"), "old": "a", "new": "b"}, auto)
    # оркестрація бачить змінений файл, планувальник — факт правки
    assert touched_paths("replace_in_file", {"path": "m.py"}) == ["m.py"]
    assert "✎ replace_in_file m.py" in facts_report([("replace_in_file", {"path": "m.py"}, "замінено в m.py:\n...")])
    assert "replace_in_file" in reg.names()
    print("OK: replace_in_file — точний/нечіткий збіг, неоднозначність, синтаксис, видалення, CRLF, дозволи, межі")


if __name__ == "__main__":
    main()
