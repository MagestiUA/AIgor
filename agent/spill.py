"""Великі виводи тулів — у файл, а в контекст лише голова + хвіст і шлях.

Раніше `run_shell` різав вивід до ПЕРШИХ 2000 символів: помилка (зазвичай у кінці) губилась, а решту модель
не могла дістати. Тепер повний вивід лягає у ~/.local-code-agent/tool-output/, у контекст іде початок і кінець, а модель
за потреби шукає в ньому (search) або дочитує (read_file(path, start_line=…)) — тека входить до дозволених.
Тека живе в профілі користувача (не в репозиторії, щоб dev-watcher не перезапускався), старі файли чистимо.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

SPILL_ROOT = Path.home() / ".local-code-agent" / "tool-output"
HEAD, TAIL = 1200, 1200          # скільки символів початку/кінця лишаємо в контексті
KEEP_DAYS = 7


def _prune(now: float) -> None:
    try:
        for p in SPILL_ROOT.glob("*.txt"):
            if now - p.stat().st_mtime > KEEP_DAYS * 86400:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def spill(text: str, label: str = "out", head: int = HEAD, tail: int = TAIL) -> str:
    """Короткий текст повертаємо як є; довгий зберігаємо у файл і віддаємо голову+хвіст із вказівкою, де решта."""
    if len(text) <= head + tail + 200:
        return text
    now = time.time()
    try:
        SPILL_ROOT.mkdir(parents=True, exist_ok=True)
        _prune(now)
        name = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{re.sub(r'[^A-Za-z0-9_-]+', '_', label)[:20]}-{int(now * 1000) % 100000}.txt"
        path = SPILL_ROOT / name
        path.write_text(text, encoding="utf-8")
    except OSError:
        return text[:head] + f"\n…[пропущено {len(text) - head - tail} симв.]…\n" + text[-tail:]
    return (f"{text[:head]}\n…[пропущено {len(text) - head - tail} з {len(text)} симв.; повний вивід у файлі "
            f"{path} — знайти потрібне: search(pattern, path='{path}'), дочитати: read_file(path='{path}', "
            f"start_line=N)]…\n{text[-tail:]}")
