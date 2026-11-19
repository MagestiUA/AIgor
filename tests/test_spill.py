"""Великі виводи тулів: голова+хвіст у контекст, повний вивід у файл, який модель може дочитати read_file."""
import tempfile
import time
from pathlib import Path

from agent import spill
from agent.toolkit import ToolContext, default_registry


def main() -> None:
    real = spill.SPILL_ROOT
    spill.SPILL_ROOT = Path(tempfile.mkdtemp()) / "tool-output"           # ніколи не чіпаємо реальний профіль
    try:
        # 1) короткий текст — як є
        assert spill.spill("коротко") == "коротко" and not spill.SPILL_ROOT.exists()
        # 2) довгий: голова і ХВІСТ у контексті (помилка в кінці не губиться), повний вивід у файлі
        big = "HEAD-" + "x" * 20000 + "-ПОМИЛКА У КІНЦІ"
        out = spill.spill(big, "shell")
        assert out.startswith("HEAD-") and out.endswith("-ПОМИЛКА У КІНЦІ") and len(out) < 3200
        files = list(spill.SPILL_ROOT.glob("*.txt"))
        assert len(files) == 1 and files[0].read_text(encoding="utf-8") == big and str(files[0]) in out
        # 3) старі файли чистяться
        old = files[0]
        t = time.time() - 9 * 86400
        import os
        os.utime(old, (t, t))
        spill.spill("y" * 5000, "b")
        assert not old.exists()
        # 4) run_shell реально використовує це, а read_file дозволяє дочитати файл-вивід (поза корінь проєкту)
        root = Path(tempfile.mkdtemp())
        ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "auto"})
        reg = default_registry()
        res = reg.dispatch("run_shell", {"command": "python -c \"print('A'*5000); print('КІНЕЦЬ-ВИВОДУ')\""}, ctx)
        assert "КІНЕЦЬ-ВИВОДУ" in res and "пропущено" in res and len(res) < 3200, res[:200]
        path = next(l for l in res.split() if l.endswith(".txt") and "tool-output" in l)
        full = reg.dispatch("read_file", {"path": path}, ctx)
        assert "КІНЕЦЬ-ВИВОДУ" in full and full.count("A") >= 5000
        # 5) файл поза проєктом і поза текою виводів — як і раніше заборонено
        other = Path(tempfile.mkdtemp()) / "x.txt"
        other.write_text("секрет", encoding="utf-8")
        assert "заборонено" in reg.dispatch("read_file", {"path": str(other)}, ctx)
        # 6) помилка команди: stderr не губиться, коли є і stdout
        res = reg.dispatch("run_shell", {"command": "python -c \"import sys; print('out'); sys.stderr.write('BOOM'); sys.exit(3)\""}, ctx)
        assert "out" in res and "BOOM" in res and "[код завершення: 3]" in res, res
    finally:
        spill.SPILL_ROOT = real
    print("OK: spill — голова+хвіст у контексті, повний вивід у файлі, чистка, доступ read_file, stderr")


if __name__ == "__main__":
    main()
