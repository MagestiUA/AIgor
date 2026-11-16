"""Збереження стилю файлу при перезаписі (tools.match_style + write_file).
Живий кейс Cerberus3: усі табові відступи стали пробілами після повного переписування."""
import ast
import tempfile
from pathlib import Path

from agent import limits, tools
from agent.toolkit import ToolContext, default_registry

limits.FORCE = limits.LOCAL

TAB_SRC = (
    "class A:\n"
    "\tdef f(self, x):\n"
    '\t\t"""Док.\n'
    "    рядок з пробілами всередині докстрінга\n"
    '\t\t"""\n'
    "\t\tif x:\n"
    "\t\t\treturn foo(1,\n"
    "\t\t\t           2)\n"
    "\t\treturn 0\n"
)
# те, що пише модель: 4 пробіли, той самий зміст
SPACE_SRC = (
    "class A:\n"
    "    def f(self, x):\n"
    '        """Док.\n'
    "    рядок з пробілами всередині докстрінга\n"
    '        """\n'
    "        if x:\n"
    "            return foo(1,\n"
    "                       2)\n"
    "        return 0\n"
)


def main() -> None:
    assert tools.detect_indent(TAB_SRC) == "tab" and tools.detect_indent(SPACE_SRC) == "space"
    assert tools.detect_indent("x = 1\n") is None

    # таби -> пробіли моделі повертаються до табів; вміст докстрінга НЕ чіпається;
    # синтаксичне дерево те саме
    fixed, note = tools.match_style(TAB_SRC, SPACE_SRC, "a.py")
    assert "табів" in note, note
    assert "\n    рядок з пробілами всередині докстрінга\n" in fixed          # docstring цілий
    assert fixed.splitlines()[1] == "\tdef f(self, x):" and fixed.splitlines()[5] == "\t\tif x:"
    assert ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(SPACE_SRC))

    # зворотний напрямок і збіг стилів
    back, note2 = tools.match_style(SPACE_SRC, TAB_SRC, "a.py")
    assert "пробілів" in note2 and ast.dump(ast.parse(back)) == ast.dump(ast.parse(TAB_SRC))
    same, note3 = tools.match_style(SPACE_SRC, SPACE_SRC, "a.py")
    assert same == SPACE_SRC and note3 == ""

    # CRLF зберігається
    crlf, n4 = tools.match_style("a = 1\r\nb = 2\r\n", "a = 1\nb = 3\n", "a.txt")
    assert crlf == "a = 1\r\nb = 3\r\n" and "CRLF" in n4

    # не-Python: лише попередження, текст не чіпаємо
    txt, n5 = tools.match_style("a:\n\tb\n", "a:\n    b\n", "c.yaml")
    assert txt == "a:\n    b\n" and "УВАГА" in n5

    # не токенізується (битий Python) -> нічого не міняємо, попередження
    broken = "def f(:\n    '''unterminated\n"
    out, n6 = tools.match_style("class A:\n\tx = 1\n", broken, "b.py")
    assert out == broken and "УВАГА" in n6

    # через write_file: перезапис tab-файлу «пробільним» кодом лишає таби
    root = Path(tempfile.mkdtemp())
    (root / "m.py").write_text(TAB_SRC, encoding="utf-8", newline="")
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"})
    real = tools.BACKUP_ROOT
    tools.BACKUP_ROOT = Path(tempfile.mkdtemp()) / "bak"          # бекапи не в ~/.local-code-agent
    try:
        res = default_registry().dispatch("write_file", {"path": "m.py", "content": SPACE_SRC}, ctx)
    finally:
        tools.BACKUP_ROOT = real
    assert "табів" in res, res
    assert (root / "m.py").read_text(encoding="utf-8") == fixed
    # новий файл пишеться як є
    default_registry().dispatch("write_file", {"path": "new.py", "content": SPACE_SRC}, ctx)
    assert (root / "new.py").read_text(encoding="utf-8") == SPACE_SRC
    print("OK: style — таби/пробіли/CRLF зберігаються, docstring не чіпається, AST незмінний, write_file")


if __name__ == "__main__":
    main()
