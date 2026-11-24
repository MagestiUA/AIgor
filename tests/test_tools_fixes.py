"""Регресії з першого прогону на Cerberus3: UTF-8 у run_shell, бекапи не в корені
проєкту, ліміт ітерацій = невдача кроку. BACKUP_ROOT підміняється на тимчасову теку
(не чіпаємо ~/.local-code-agent)."""
import sys
import tempfile
from pathlib import Path

from agent import limits, tools
from agent.agent_loop import ITER_LIMIT_MSG, run_step
from agent.toolkit import ToolContext

limits.FORCE = limits.production()


class LoopForever:
    def chat(self, messages, tools=None, profile=None, fmt=None):
        return {"tool_calls": [{"function": {"name": "list_dir", "arguments": {}}}]}


def main() -> None:
    # run_shell віддає кирилицю без мохібаке (раніше: «РџСЂРёРІС–С‚»)
    r = tools.run_shell(f'"{sys.executable}" -c "print(\'Привіт, світе\')"', allow_all=True)
    assert r.allowed and r.returncode == 0 and "Привіт, світе" in r.stdout, (r.stdout, r.stderr)

    # бекап лягає в BACKUP_ROOT, а не поруч із файлом
    real_root = tools.BACKUP_ROOT
    tools.BACKUP_ROOT = Path(tempfile.mkdtemp()) / "backups"
    try:
        proj = Path(tempfile.mkdtemp())
        f = proj / "a.py"; f.write_text("x = 1\n", encoding="utf-8")
        bak = Path(tools.backup_file(f))
        assert bak.is_file() and tools.BACKUP_ROOT in bak.parents
        assert bak.read_text(encoding="utf-8") == "x = 1\n"
        assert [p.name for p in proj.iterdir()] == ["a.py"], "у корені проєкту не має бути .bak"
        assert tools.backup_file(proj / "нема.py") is None
    finally:
        tools.BACKUP_ROOT = real_root

    # вичерпаний ліміт ітерацій повертає маркер, за яким крок вважається невдалим
    ctx = ToolContext(root=Path(tempfile.mkdtemp()), permissions={"edits": "auto", "shell": "off"})
    final, log = run_step("безкінечно", ctx, client=LoopForever(), max_iters=3)
    assert final == ITER_LIMIT_MSG and len(log) == 3
    print("OK: tools fixes — UTF-8 run_shell, бекапи поза проєктом, маркер ліміту ітерацій")


if __name__ == "__main__":
    main()
