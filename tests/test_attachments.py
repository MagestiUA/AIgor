"""Офлайн-тест agent/attachments.py (диск + метадані).
Запуск: .venv\\Scripts\\python.exe -m tests.test_attachments
"""
import shutil
import tempfile
from pathlib import Path

import agent.attachments as _A
_A.ATTACHMENTS_ROOT = Path(tempfile.mkdtemp()) / "attachments"     # реальний профіль не чіпаємо

from agent.attachments import (
    classify, process, session_dir, save, remove, clear, rename_session,
    list_saved, attachments_note, total_size, read_combined,
)

SID = "_test_session_xyz"
SID2 = "_test_session_xyz2"


from agent import limits
limits.FORCE = limits.production()   # не залежати від налаштувань користувача


def main():
    # classify
    assert classify("main.py", b"print('hello')") == "text"
    assert classify("data.bin", bytes(range(256))) == "binary"
    assert classify("README.md", b"# Hello") == "text"
    assert classify("image.png", b"\x89PNG\r\n") == "binary"

    # process: нормальний текст
    r = process("foo.py", b"x = 1\n")
    assert r["error"] == ""
    assert r["_content"] == "x = 1\n"
    assert not r["truncated"]

    # process: завеликий / бінарний → error, без _content
    lim = limits.get()
    assert process("big.txt", b"x" * (lim.max_file + 1))["error"] != ""
    r3 = process("img.png", b"\x89PNG\r\n\x1a\n")
    assert r3["error"] != ""
    assert "_content" not in r3

    # process: обрізання до limits.file_chars
    r4 = process("long.py", ("a" * (lim.file_chars + 500)).encode())
    assert r4["error"] == "" and r4["truncated"]
    assert len(r4["_content"]) == lim.file_chars

    # ── Диск: save / list_saved / remove / clear ──
    clear(SID); clear(SID2)
    try:
        p = save(SID, "main.py", "print(1)\n")
        assert p.is_file() and p.read_text(encoding="utf-8") == "print(1)\n"
        assert p.parent == session_dir(SID)
        save(SID, "utils.py", "x = 2\n")

        meta = list_saved(SID)
        names = {m["name"] for m in meta}
        assert names == {"main.py", "utils.py"}
        assert all(m["error"] == "" for m in meta)

        # traversal-захист: лише базове ім'я
        ev = save(SID, "../evil.py", "bad")
        assert ev.parent == session_dir(SID) and ev.name == "evil.py"
        remove(SID, "evil.py")

        # attachments_note: імена файлів + інструкція read_file/search, без вмісту
        note = attachments_note(SID, meta)
        assert "main.py" in note and "utils.py" in note
        assert "read_file(path)" in note and "search(" in note   # стеримо модель на правильні тули
        assert "print(1)" not in note            # вмісту немає
        assert attachments_note(SID, []) == ""
        # error-файли не потрапляють у note
        assert attachments_note(SID, [{"name": "x", "error": "бінарний"}]) == ""

        # remove одного файлу
        remove(SID, "utils.py")
        assert {m["name"] for m in list_saved(SID)} == {"main.py"}

        # rename_session: міграція _tmp → реальна сесія
        rename_session(SID, SID2)
        assert not session_dir(SID).exists()
        assert {m["name"] for m in list_saved(SID2)} == {"main.py"}

        # list_saved на відсутній теці → []
        assert list_saved("nope_nonexistent") == []

        # total_size / read_combined: для inline-фічі ("обробити кілька малих
        # вкладень за один LLM-виклик замість тул-циклу на кожен файл окремо")
        save(SID2, "b.txt", "BBBB")
        # (не хардкодимо байти "print(1)\n" — write_text транслює \n у платформний
        # лінкінд, тож звіряємось проти суми, а не літералу)
        assert total_size(SID2, ["main.py", "b.txt"]) == \
            total_size(SID2, ["main.py"]) + total_size(SID2, ["b.txt"]) > 0
        assert total_size(SID2, ["nope.txt"]) == 0
        combined = read_combined(SID2, ["main.py", "b.txt"])
        assert "=== main.py ===\nprint(1)\n" in combined
        assert "=== b.txt ===\nBBBB" in combined
        assert combined.index("main.py") < combined.index("b.txt")   # порядок як у names
        assert read_combined(SID2, ["nope.txt"]) == ""   # неіснуючий файл -> пропускаємо
        assert total_size(SID2, ["main.py", "b.txt"]) < limits.get().inline
        remove(SID2, "b.txt")

        # read_attachment: нечіткий пошук за іменем у теці вкладень
        from agent.toolkit import ToolContext, h_read_attachment
        ctx = ToolContext(root=session_dir(SID2), attachments_dir=session_dir(SID2))
        assert "print(1)" in h_read_attachment({"name": "main.py"}, ctx)   # exact
        assert "print(1)" in h_read_attachment({"name": "MAIN.PY"}, ctx)   # case-insensitive
        assert "print(1)" in h_read_attachment({"name": "main"}, ctx)      # підрядок
        # повний шлях теж зводиться до базового імені
        assert "print(1)" in h_read_attachment(
            {"name": str(session_dir(SID2) / "main.py")}, ctx)
        assert "не знайдено" in h_read_attachment({"name": "zzz.py"}, ctx)
        # без attachments_dir → зрозуміле повідомлення
        assert "немає" in h_read_attachment({"name": "x"}, ToolContext(root=session_dir(SID2)))
        # тепер вкладення читає звичайний read_file — за іменем (нечітко), навіть коли корінь — інша тека проєкту
        from agent.toolkit import default_registry
        proj = Path(tempfile.mkdtemp())
        (proj / "a.py").write_text("A = 1\n", encoding="utf-8")
        pctx = ToolContext(root=proj, attachments_dir=session_dir(SID2))
        reg = default_registry()
        assert "print(1)" in reg.dispatch("read_file", {"path": "MAIN"}, pctx)            # ім'я вкладення
        assert "A = 1" in reg.dispatch("read_file", {"path": "a.py"}, pctx)              # файл проєкту
        assert "print(1)" in reg.dispatch("read_file", {"path": str(session_dir(SID2) / "main.py")}, pctx)
        miss = reg.dispatch("read_file", {"path": "zzz.py"}, pctx)
        assert "не знайдено" in miss and "main.py" in miss                                # підказка: що є
        assert "print(1)" in reg.dispatch("read_attachment", {"name": "main"}, pctx)      # стара назва працює
        assert "read_attachment" not in reg.names() and "read_attachment" not in str(reg.schema())
    finally:
        clear(SID); clear(SID2)

    assert not session_dir(SID).exists()

    print("OK: attachments — classify, process, save/list_saved/remove/clear, "
          "rename_session, attachments_note, read_attachment (нечіткий пошук), "
          "traversal-захист, total_size/read_combined (inline-бюджет)")


if __name__ == "__main__":
    main()
