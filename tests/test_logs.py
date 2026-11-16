"""Офлайн-тест agent/logs.py: файл у тимчасовій теці, ротація за розміром, вміст
не пишеться без IGOR_LOG_CONTENT, ключ не потрапляє в лог тулів.
LOG_DIR підміняється на тимчасову теку (не чіпаємо ~/.local-code-agent/logs)."""
import logging
import os
import tempfile
from pathlib import Path

from agent import logs
from agent.toolkit import ToolContext, default_registry


def main() -> None:
    real_dir, real_max = logs.LOG_DIR, logs.MAX_BYTES
    logs.LOG_DIR = Path(tempfile.mkdtemp()) / "logs"
    logs.MAX_BYTES = 2000                          # дрібний ліміт, щоб побачити ротацію
    saved_env = os.environ.pop("IGOR_LOG_CONTENT", None)
    name = "igor-test"
    try:
        log = logs.setup(name)
        assert (logs.LOG_DIR / f"{name}.log").exists()
        assert logs.setup(name) is log and len(log.handlers) == 1     # ідемпотентно

        # ротація: забагато записів -> з'являються архівні файли, основний не росте безмежно
        for i in range(200):
            log.info("рядок %d %s", i, "x" * 60)
        files = sorted(p.name for p in logs.LOG_DIR.iterdir())
        assert f"{name}.log.1" in files, files
        assert len(files) <= logs.BACKUPS + 1
        assert (logs.LOG_DIR / f"{name}.log").stat().st_size <= logs.MAX_BYTES + 200

        # clip: без IGOR_LOG_CONTENT — лише довжина; з ним — обрізаний вміст
        assert logs.clip("секрет-вміст") == "<12 симв.>"
        os.environ["IGOR_LOG_CONTENT"] = "1"
        assert logs.clip("a" * 300).endswith("…") and len(logs.clip("a" * 300)) == 201
        os.environ.pop("IGOR_LOG_CONTENT")

        # логер тулів (дитина "igor") пише метадані, але не вміст
        logging.getLogger("igor").handlers = list(log.handlers)       # підʼєднати файл тесту
        logging.getLogger("igor").setLevel(logging.INFO)
        root = Path(tempfile.mkdtemp())
        (root / "s.txt").write_text("СУПЕРСЕКРЕТНИЙ-ВМІСТ", encoding="utf-8")
        ctx = ToolContext(root=root, permissions={"edits": "off", "shell": "off"})
        default_registry().dispatch("read_file", {"path": "s.txt"}, ctx)
        default_registry().dispatch("read_file", {"path": "нема.txt"}, ctx)
        default_registry().dispatch("зламаний", {}, ctx)
        for h in log.handlers:
            h.flush()
        text = "\n".join(p.read_text(encoding="utf-8") for p in logs.LOG_DIR.iterdir())
        assert "тул read_file ок" in text and "невідомий інструмент: зламаний" in text
        assert "СУПЕРСЕКРЕТНИЙ-ВМІСТ" not in text
    finally:
        for h in list(logging.getLogger(name).handlers) + list(logging.getLogger("igor").handlers):
            h.close()
        logging.getLogger("igor").handlers = []
        logging.getLogger(name).handlers = []
        logs.LOG_DIR, logs.MAX_BYTES = real_dir, real_max
        if saved_env is not None:
            os.environ["IGOR_LOG_CONTENT"] = saved_env
    print("OK: logs — файл, ідемпотентність, ротація, clip, тули не пишуть вміст")


if __name__ == "__main__":
    main()
