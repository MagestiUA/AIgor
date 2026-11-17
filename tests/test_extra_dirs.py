"""Додаткові теки проєкту: тули читають/пишуть у них за абсолютним шляхом, решта диска закрита."""
import tempfile
from pathlib import Path

from agent import session as sess
from agent.toolkit import ToolContext, default_registry


def main() -> None:
    base = Path(tempfile.mkdtemp())
    root, extra, other = base / "proj", base / "copy", base / "other"
    for d in (root, extra, other):
        d.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (other / "secret.txt").write_text("s", encoding="utf-8")
    reg = default_registry()
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"}, extra_roots=[extra])

    # запис і читання в додатковій теці за абсолютним шляхом
    out = reg.dispatch("write_file", {"path": str(extra / "b.py"), "content": "y = 2\n"}, ctx)
    assert (extra / "b.py").read_text(encoding="utf-8") == "y = 2\n", out
    assert "y = 2" in reg.dispatch("read_file", {"path": str(extra / "b.py")}, ctx)
    # головна тека працює як раніше; третя тека закрита
    assert "x = 1" in reg.dispatch("read_file", {"path": "a.py"}, ctx)
    assert "поза межами" in reg.dispatch("read_file", {"path": str(other / "secret.txt")}, ctx)
    assert "поза межами" in reg.dispatch("read_file", {"path": str(extra / ".." / "other" / "secret.txt")}, ctx)
    # без додаткових тек доступу до extra нема
    plain = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"})
    assert "поза межами" in reg.dispatch("read_file", {"path": str(extra / "b.py")}, plain)

    # сесія зберігає extra_dirs
    s = sess.new_session("t", root)
    s.extra_dirs = [str(extra)]
    sess.save_session(s, base / "sessions")
    assert sess.load_session(s.id, base / "sessions").extra_dirs == [str(extra)]
    print("OK: extra_dirs — доступ лише до кореня й доданих тек, сесія зберігає список")


if __name__ == "__main__":
    main()
