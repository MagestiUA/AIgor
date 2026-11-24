"""Офлайн-тест реєстру інструментів (детерміновані тули, без моделі).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_toolkit
"""
import tempfile
from pathlib import Path

from agent.toolkit import ToolContext, default_registry


from agent import limits
limits.FORCE = limits.LOCAL   # не залежати від налаштувань користувача


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "a.py").write_text("X = 1\n", encoding="utf-8")
    reg = default_registry()
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "allowlist"})

    sch = reg.schema()
    assert any(t["function"]["name"] == "run_shell" for t in sch)
    assert any(t["function"]["name"] == "create_from_source" for t in sch)

    assert "X = 1" in reg.dispatch("read_file", {"path": "a.py"}, ctx)
    assert "записано" in reg.dispatch("write_file", {"path": "b.txt", "content": "hi"}, ctx)
    assert (root / "b.txt").read_text(encoding="utf-8") == "hi"

    lst = reg.dispatch("list_dir", {}, ctx)
    assert "a.py" in lst and "b.txt" in lst

    assert "Python" in reg.dispatch("run_shell", {"command": "python --version"}, ctx)

    # allowlist блокує деструктив
    assert "заблоковано" in reg.dispatch("run_shell", {"command": "del b.txt"}, ctx)

    # shell off
    ctx_off = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"})
    assert "вимкнена" in reg.dispatch("run_shell", {"command": "python --version"}, ctx_off)

    # write_file захист: порожній path і тека
    assert "не вказано path" in reg.dispatch("write_file", {"content": "x"}, ctx)
    assert "не вказано path" in reg.dispatch("write_file", {"path": "  ", "content": "x"}, ctx)
    (root / "sub").mkdir()
    assert "це тека" in reg.dispatch("write_file", {"path": "sub", "content": "x"}, ctx)
    # write_file створює батьківські теки
    assert "записано" in reg.dispatch("write_file", {"path": "deep/n/c.txt", "content": "y"}, ctx)
    assert (root / "deep" / "n" / "c.txt").read_text(encoding="utf-8") == "y"

    # read_file великого файлу віддає частину за РЯДКАМИ і підказує, як читати далі (і create_from_source)
    (root / "big.py").write_text("".join(f"# рядок {i}\n" for i in range(1, 3001)), encoding="utf-8")
    big = reg.dispatch("read_file", {"path": "big.py"}, ctx)
    assert big.startswith("[big.py: рядки 1-") and "з 3000]" in big, big[:80]
    assert "start_line=" in big and "create_from_source" in big and "search(" in big
    # ...і дочитується частинами до кінця (живий кейс Cerberus3: модель не могла дочитати файл і вигорала на run_shell)
    import re as _re
    got, nxt = [], 1
    for _ in range(50):
        part = reg.dispatch("read_file", {"path": "big.py", "start_line": nxt}, ctx)
        head, body = part.split("\n", 1)
        a, b = map(int, _re.search(r"рядки (\d+)-(\d+)", head).groups())
        assert a == nxt
        got += body.split("\n\n…[файл триває")[0].split("\n")
        if b >= 3000:
            break
        nxt = b + 1
    assert got == [f"# рядок {i}" for i in range(1, 3001)], (len(got), got[:2], got[-2:])
    # точкове читання: start_line + limit (як після search)
    part = reg.dispatch("read_file", {"path": "big.py", "start_line": 120, "limit": 3}, ctx)
    assert part.startswith("[big.py: рядки 120-122 з 3000]") and "# рядок 121" in part and "# рядок 123" not in part.split("…")[0]
    assert "start_line=123" in part
    assert "лише 3000 рядків" in reg.dispatch("read_file", {"path": "big.py", "start_line": 5000}, ctx)
    # старий offset (символи) лишається для дуже довгих рядків
    assert reg.dispatch("read_file", {"path": "big.py", "offset": 5}, ctx).startswith("[big.py: символи 5-")
    # search: файл:рядок: текст; розумний регістр; glob; некоректний вираз — як текст; поза межами — заборонено
    (root / "pkg").mkdir()
    (root / "pkg" / "m.py").write_text("def Load_Config():\n    return 1\n# TODO: прибрати\n", encoding="utf-8")
    (root / "pkg" / "n.txt").write_text("load_config у тексті\n", encoding="utf-8")
    s = reg.dispatch("search", {"pattern": "load_config"}, ctx)
    assert "pkg/m.py:1: def Load_Config():" in s and "pkg/n.txt:1:" in s, s
    assert "pkg/n.txt" not in reg.dispatch("search", {"pattern": "load_config", "glob": "*.py"}, ctx)
    assert "не знайдено" in reg.dispatch("search", {"pattern": "Load_config"}, ctx)        # є велика літера → з регістром
    assert "m.py:3:" in reg.dispatch("search", {"pattern": "TODO|FIXME", "path": "pkg"}, ctx)
    assert "як звичайний текст" in reg.dispatch("search", {"pattern": "def Load_Config(("}, ctx)
    assert "pkg/m.py:1:" in reg.dispatch("search", {"pattern": "Load_Config("}, ctx)      # дужка без пари — як текст
    assert "заборонено" in reg.dispatch("search", {"pattern": "x", "path": str(root.parent)}, ctx)
    from agent import limits
    # REMOTE-ліміт: файл ~43k символів віддається цілком за один виклик
    limits.FORCE = limits.REMOTE
    try:
        (root / "mid.py").write_text("x = 1\n" * 7000, encoding="utf-8")
        assert reg.dispatch("read_file", {"path": "mid.py"}, ctx) == "x = 1\n" * 7000
    finally:
        limits.FORCE = limits.LOCAL

    # вкладення в ОДИН довгий рядок (вставлений текст): частинами за символами, з підказкою автопродовження
    from agent.agent_loop import pending_continuation
    att_dir = root / ".att"
    att_dir.mkdir()
    (att_dir / "paste.txt").write_text("x" * 70_000, encoding="utf-8")
    ctx_att = ToolContext(root=root, permissions={"edits": "auto", "shell": "allowlist"},
                          attachments_dir=att_dir)
    p1 = reg.dispatch("read_file", {"path": "paste.txt"}, ctx_att)
    assert "символи 0-32000 з 70000" in p1 and "offset=32000" in p1 and "лишилось" in p1, p1[:120]
    cont = pending_continuation([{"role": "tool", "content": p1}])
    assert cont["function"]["arguments"] == {"path": "paste.txt", "offset": 32000}, cont
    p2 = reg.dispatch("read_attachment", {"name": "paste.txt", "offset": 32000}, ctx_att)    # стара назва теж
    assert "32000-64000 з 70000" in p2 and "offset=64000" in p2, p2[:120]
    p3 = reg.dispatch("read_file", {"path": "paste.txt", "offset": 64000}, ctx_att)
    assert "64000-70000 з 70000" in p3 and "лишилось" not in p3, p3[:120]   # останній шматок — без "лишилось"

    # невідомий тул
    assert "невідомий" in reg.dispatch("nope", {}, ctx)

    # захист від path traversal: вихід за корінь (.. і абсолютний) заборонено
    up = reg.dispatch("read_file", {"path": "../../../etc/passwd"}, ctx)
    assert "поза межами" in up, up
    out_abs = reg.dispatch("write_file",
                           {"path": str(Path(tempfile.gettempdir()) / "lca_escape.txt"),
                            "content": "x"}, ctx)
    assert "поза межами" in out_abs, out_abs
    assert not (Path(tempfile.gettempdir()) / "lca_escape.txt").exists()
    # легітимні шляхи всередині кореня (зокрема через підтеку) — працюють
    assert "записано" in reg.dispatch("write_file", {"path": "ok/nested.txt", "content": "z"}, ctx)
    assert "a.py" in reg.dispatch("list_dir", {"path": "."}, ctx)

    print("OK: реєстр — схема, read/write/list/run_shell, дозволи, захист write/read, "
          "traversal-захист, невідомий тул")
    print(f"  інструменти: {reg.names()}")


if __name__ == "__main__":
    main()
