"""Логування Ігоря з ротацією — щоб за логами було видно, де що зламалось.

Файли: ~/.local-code-agent/logs/<name>.log, до 2 МБ кожен + 5 архівних (≈12 МБ на
процес). Два процеси пишуть у РІЗНІ файли (на Windows ротація одного файлу з двох
процесів ламається): `igor-launcher` (main.py: старт, URL, код виходу сервера) і
`igor` (Reflex-бекенд: запити до LLM, виклики тулів, кроки, винятки).

Що пишемо: метадані — модель, кількість повідомлень/токенів, тривалість, ретраї,
імена тулів і ключі аргументів, розміри результатів, помилки з traceback.
Чого НЕ пишемо за замовчуванням: вміст промптів/відповідей/файлів і API-ключ
(реальні проєкти розробників). Для налагодження: змінна IGOR_LOG_CONTENT=1 додає
обрізаний (200 симв.) вміст аргументів і результатів тулів. Ключ — ніколи.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".local-code-agent" / "logs"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5
_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured: set[str] = set()


def content_enabled() -> bool:
    return os.environ.get("IGOR_LOG_CONTENT", "") not in ("", "0", "false")


def clip(s, n: int = 200) -> str:
    """Обрізаний вміст — лише якщо ввімкнено IGOR_LOG_CONTENT, інакше лише довжина."""
    s = "" if s is None else str(s)
    return (s[:n].replace("\n", "\\n") + ("…" if len(s) > n else "")) if content_enabled() else f"<{len(s)} симв.>"


def setup(name: str = "igor", level: int | str | None = None) -> logging.Logger:
    """Ідемпотентно налаштувати логер `name` (файл із ротацією + перехоплення
    необроблених винятків). Рівень: аргумент > env IGOR_LOG_LEVEL > INFO."""
    log = logging.getLogger(name)
    if name in _configured:
        return log
    lvl = level or os.environ.get("IGOR_LOG_LEVEL", "INFO")
    log.setLevel(lvl if isinstance(lvl, int) else str(lvl).upper())
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter(_FMT))
        log.addHandler(h)
    except OSError:
        pass                                    # логування не має валити застосунок
    log.propagate = False
    _configured.add(name)

    prev_hook = sys.excepthook

    def _hook(et, ev, tb):
        log.critical("Необроблений виняток", exc_info=(et, ev, tb))
        prev_hook(et, ev, tb)

    sys.excepthook = _hook
    threading.excepthook = lambda a: log.critical(
        "Необроблений виняток у потоці %s", getattr(a.thread, "name", "?"),
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("=== старт процесу pid=%s python=%s ===", os.getpid(), sys.version.split()[0])
    return log


def get(sub: str = "") -> logging.Logger:
    """Логер підсистеми: igor.llm, igor.tools, igor.agent… (діти налаштованого `igor`)."""
    return logging.getLogger("igor" + (f".{sub}" if sub else ""))
