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

    # read_file великого файлу обрізає й підказує create_from_source
    (root / "big.py").write_text("# c\n" * 5000, encoding="utf-8")
    big = reg.dispatch("read_file", {"path": "big.py"}, ctx)
    assert "offset=" in big and "create_from_source" in big and "з 20000]" in big
    # ...і дочитується частинами через offset до кінця (живий кейс Cerberus3: без offset
    # модель не могла прочитати файл > 8000 символів і вигорала на run_shell)
    from agent import limits
    cap = limits.get().read_chars
    got, off = "", 0
    while off < 20000:
        part = reg.dispatch("read_file", {"path": "big.py", "offset": off}, ctx)
        body = part.split("]\n", 1)[1] if part.startswith("[символи") else part
        body = body.split("\n\n…[файл триває")[0]
        got += body
        off += cap
    assert got == "# c\n" * 5000, len(got)
    # REMOTE-ліміт: файл ~43k символів віддається цілком за один виклик
    limits.FORCE = limits.REMOTE
    try:
        (root / "mid.py").write_text("x = 1\n" * 7000, encoding="utf-8")
        assert reg.dispatch("read_file", {"path": "mid.py"}, ctx) == "x = 1\n" * 7000
    finally:
        limits.FORCE = limits.LOCAL

    # read_attachment: пагінація через offset для великих файлів
    att_dir = root / ".att"
    att_dir.mkdir()
    (att_dir / "paste.txt").write_text("x" * 70_000, encoding="utf-8")
    ctx_att = ToolContext(root=root, permissions={"edits": "auto", "shell": "allowlist"},
                          attachments_dir=att_dir)
    p1 = reg.dispatch("read_attachment", {"name": "paste.txt"}, ctx_att)
    assert "0-32000 з 70000" in p1 and "offset=32000" in p1, p1
    p2 = reg.dispatch("read_attachment", {"name": "paste.txt", "offset": 32000}, ctx_att)
    assert "32000-64000 з 70000" in p2 and "offset=64000" in p2, p2
    p3 = reg.dispatch("read_attachment", {"name": "paste.txt", "offset": 64000}, ctx_att)
    assert "64000-70000 з 70000" in p3 and "лишилось" not in p3, p3   # останній шматок — без "лишилось"

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
