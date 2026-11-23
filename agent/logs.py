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

import contextvars
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".local-code-agent" / "logs"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5
_FMT = "%(asctime)s %(levelname)-7s [%(sid)s] %(name)s: %(message)s"
# id поточного чату — потрапляє в кожен рядок логу (contextvars переходять у asyncio.to_thread),
# щоб за логом можна було відокремити події одного чату від іншого.
session_var: contextvars.ContextVar[str] = contextvars.ContextVar("igor_sid", default="-")


class _SidFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.sid = session_var.get()[:8]
        return True


def app_version() -> str:
    """Версія збірки: файл VERSION у корені (портативна збірка) або короткий git-хеш."""
    root = Path(__file__).resolve().parent.parent
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=3,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.stdout.strip() or "dev"
    except (OSError, subprocess.SubprocessError):
        return "dev"
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
        h.addFilter(_SidFilter())
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
    log.info("=== старт процесу pid=%s python=%s ігорь=%s ОС=%s ===", os.getpid(),
             sys.version.split()[0], app_version(), platform.platform())
    return log


def get(sub: str = "") -> logging.Logger:
    """Логер підсистеми: igor.llm, igor.tools, igor.agent… (діти налаштованого `igor`)."""
    return logging.getLogger("igor" + (f".{sub}" if sub else ""))


def raw_file_logger(name: str) -> logging.Logger:
    """Логер «як є» (без часу/рівня) з ротацією — для рядків stdout/stderr дочірнього
    процесу (Reflex/Granian), які лаунчер перенаправляє в igor-server.log. Тут видно
    збої старту й traceback-и самого фреймворку, які не проходять через igor.log."""
    log = logging.getLogger(name)
    if name in _configured:
        return log
    log.setLevel(logging.INFO)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(h)
    except OSError:
        pass
    log.propagate = False
    _configured.add(name)
    return log


DIAG_DIR = Path.home() / ".local-code-agent" / "diagnostics"


def network_report(llm_base_url: str = "", fetch=None, resolve=None) -> dict:
    """Перевірка мережі для діагностики (фідбек: брандмауер/антивірус можуть блокувати
    вихідні з'єднання нового .exe): DNS і HTTPS до DuckDuckGo (веб-пошук) та /health
    LLM-сервера. Ключі НЕ використовуються. fetch/resolve підміняються в тестах."""
    import socket
    from urllib.parse import urlparse
    if fetch is None:
        import requests
        fetch = lambda url: requests.get(url, timeout=6).status_code      # noqa: E731
    resolve = resolve or socket.gethostbyname

    def probe(name: str, call):
        t0 = time.time()
        try:
            res = call()
            return {"ok": True, "result": str(res), "сек": round(time.time() - t0, 2)}
        except Exception as e:                                         # noqa: BLE001
            return {"ok": False, "помилка": f"{type(e).__name__}: {str(e)[:200]}", "сек": round(time.time() - t0, 2)}

    rep = {"dns_duckduckgo": probe("dns", lambda: resolve("duckduckgo.com")),
           "https_duckduckgo": probe("https", lambda: fetch("https://duckduckgo.com/"))}
    if llm_base_url:
        u = urlparse(llm_base_url)
        host = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
        rep["llm_health"] = probe("llm", lambda: fetch(host + "/health"))
    return rep


def make_diagnostics(settings_path: Path | None = None, net_fetch=None, net_resolve=None) -> Path:
    """Архів для передачі розробнику: усі логи + налаштування БЕЗ ключа + версії/ОС.
    Вмісту чатів і файлів проєктів там немає."""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    out = DIAG_DIR / f"igor-diag-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    info = {"ігорь": app_version(), "python": sys.version, "ОС": platform.platform(),
            "створено": time.strftime("%Y-%m-%d %H:%M:%S"), "рівень_логів": os.environ.get("IGOR_LOG_LEVEL", "INFO"),
            "вміст_у_логах": content_enabled()}
    try:
        import reflex
        info["reflex"] = getattr(reflex, "__version__", "?")
    except Exception:
        pass
    settings: dict = {}
    sp = settings_path or (Path.home() / ".local-code-agent" / "settings.json")
    try:
        settings = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if settings.get("llm_api_key"):
        settings["llm_api_key"] = "<прихований>"
    for e in settings.get("llm_servers") or []:                  # ключі збережених серверів теж не віддаємо
        if isinstance(e, dict) and e.get("api_key"):
            e["api_key"] = "<прихований>"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=2))
        z.writestr("settings.json", json.dumps(settings, ensure_ascii=False, indent=2))
        try:
            from .backend import build_base_url
            base = build_base_url(settings.get("llm_host", ""), settings.get("llm_port", ""))
            z.writestr("network.json", json.dumps(network_report(base, net_fetch, net_resolve),
                                                   ensure_ascii=False, indent=2))
        except Exception as e:                                         # noqa: BLE001
            z.writestr("network.json", json.dumps({"помилка": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        if LOG_DIR.is_dir():
            for p in sorted(LOG_DIR.iterdir()):
                if p.is_file():
                    z.write(p, f"logs/{p.name}")
    return out
