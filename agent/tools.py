"""Детерміновані інструменти: файли, валідація, бекап, diff.

Усе, що НЕ потребує LLM. Executor викликає це напряму, без моделі.
run_shell з allow-list — у M2.
"""
from __future__ import annotations

import ast
import difflib
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config


def read_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_file(path: str | Path, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


BACKUP_ROOT = Path.home() / ".local-code-agent" / "backups"


def backup_file(path: str | Path) -> str | None:
    """Знімок перед записом. Повертає шлях бекапу або None, якщо файлу нема."""
    p = Path(path)
    if not p.exists():
        return None
    # Бекапи — НЕ поруч із файлом: живий кейс (Cerberus3) — за один прогін у корені
    # проєкту накопичилось десятки *.bak.<час>. Тепер у теці користувача, за хешем теки.
    import hashlib
    d = BACKUP_ROOT / hashlib.sha1(str(p.resolve().parent).encode("utf-8")).hexdigest()[:8]
    d.mkdir(parents=True, exist_ok=True)
    bak = d / f"{p.name}.bak.{time.time_ns()}"
    shutil.copy2(p, bak)
    return str(bak)


# ── Збереження стилю файлу при перезаписі ────────────────────────────────────
# Живий кейс (Cerberus3): агент переписав Cerberus.py повністю, і всі 1023 рядки з
# відступом-табом стали пробілами — diff на 2000 рядків замість реальних ~640.
# Модель по замовчуванню пише 4 пробіли; стиль проєкту вона не знає й не питає.
def detect_indent(text: str) -> str | None:
    """'tab' | 'space' | None — яким відступом переважно починаються рядки."""
    tabs = spaces = 0
    for ln in text.splitlines():
        if ln.startswith("\t"):
            tabs += 1
        elif ln.startswith("  "):
            spaces += 1
    if not tabs and not spaces:
        return None
    return "tab" if tabs >= spaces else "space"


def _string_content_lines(src: str) -> set[int] | None:
    """Номери рядків (з 1), що лежать ВСЕРЕДИНІ багаторядкових рядкових літералів —
    їхній вміст чіпати не можна. None, якщо код не токенізується."""
    import io
    import tokenize
    inside: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                inside.update(range(tok.start[0] + 1, tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return inside


def _indent_unit(src: str) -> int:
    widths = []
    for ln in src.splitlines():
        n = len(ln) - len(ln.lstrip(" "))
        if n and ln.strip() and not ln.startswith("\t"):
            widths.append(n)
    if not widths:
        return 4
    for u in (4, 2, 3, 8):
        if all(w % u == 0 for w in widths):
            return u
    return min(widths)


def _reindent(src: str, to: str) -> str | None:
    """Python-код: ведучі відступи рядків коду пробіли<->таби; вміст багаторядкових
    рядків не чіпаємо. None — якщо токенізація не вдалась (тоді нічого не міняємо)."""
    inside = _string_content_lines(src)
    if inside is None:
        return None
    unit = _indent_unit(src)
    out = []
    for i, ln in enumerate(src.split("\n"), 1):
        if i in inside or not ln.strip():
            out.append(ln)
            continue
        if to == "tab":
            n = len(ln) - len(ln.lstrip(" "))
            out.append(("\t" * (n // unit) + " " * (n % unit) + ln[n:]) if n else ln)
        else:
            n = len(ln) - len(ln.lstrip("\t"))
            out.append((" " * (unit * n) + ln[n:]) if n else ln)
    return "\n".join(out)


def match_style(old: str, new: str, path: str = "") -> tuple[str, str]:
    """Привести новий вміст до стилю старого (відступи, закінчення рядків).
    Повертає (текст, примітка для моделі/логу; порожня — якщо нічого не міняли)."""
    notes = []
    if "\r\n" in old and "\r\n" not in new and "\n" in new:
        new = new.replace("\n", "\r\n")
        notes.append("закінчення рядків приведено до CRLF як в оригіналі")
    o, n = detect_indent(old), detect_indent(new.replace("\r\n", "\n"))
    if o and n and o != n:
        if path.endswith(".py"):
            body = new.replace("\r\n", "\n")
            fixed = _reindent(body, "tab" if o == "tab" else "space")
            if fixed is not None:
                new = fixed.replace("\n", "\r\n") if "\r\n" in new else fixed
                notes.append(f"відступи приведено до {'табів' if o == 'tab' else 'пробілів'} як в оригіналі")
            else:
                notes.append(f"УВАГА: стиль відступів змінено ({o} → {n}), автоматично привести не вдалось")
        else:
            notes.append(f"УВАГА: стиль відступів змінено ({o} → {n}) — збережи стиль оригіналу")
    return new, "; ".join(notes)


def validate_python(src: str) -> tuple[bool, str]:
    """Чи валідний синтаксис. Executor викликає перед записом результату LLM."""
    try:
        ast.parse(src)
        return True, ""
    except SyntaxError as e:
        return False, f"{e.msg} (рядок {e.lineno})"


def unified_diff(old: str, new: str, path: str = "file") -> str:
    """Unified-diff для прев'ю змін (шар безпеки / GUI)."""
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


# ── run_shell з allow-list ──────────────────────────────────────────────────
@dataclass
class ShellResult:
    allowed: bool
    returncode: int
    stdout: str
    stderr: str


import re as _re
_VENV_PREFIX = _re.compile(
    r'^["\']?\.venv[/\\][Ss]cripts[/\\]|^["\']?\.venv[/\\]bin[/\\]',
    _re.IGNORECASE,
)


def is_allowed(command: str, allow) -> bool:
    c = command.strip()
    # Перевіряємо і оригінальну команду, і без .venv-префіксу
    # (.venv\Scripts\pip install → pip install, .venv\Scripts\python → python)
    c_stripped = _VENV_PREFIX.sub("", c)
    return any(
        c == a or c.startswith(a + " ") or
        c_stripped == a or c_stripped.startswith(a + " ")
        for a in allow
    )


def run_shell(command: str, cwd: str | None = None, timeout: int | None = None,
              allow=None, allow_all: bool = False) -> ShellResult:
    """Виконати команду. За замовчуванням лише префікси з allow-list; allow_all=True
    (режим 'ask' після підтвердження) знімає це обмеження. shell=False, тож
    ланцюжки (`a; b`, `a && b`) не інтерпретуються — захист від ін'єкцій."""
    allow = config.ALLOWED_SHELL if allow is None else allow
    timeout = config.SHELL_TIMEOUT if timeout is None else timeout
    if not allow_all and not is_allowed(command, allow):
        return ShellResult(False, -1, "", f"заблоковано allow-list: {command!r}")
    try:
        # posix=False зберігає зворотні слеші (потрібно для Windows-шляхів C:\...),
        # але лишає лапки В токенах. Знімаємо парні обгорткові лапки, щоб
        # `git commit -m "msg"` дав чисте 'msg', а не '"msg"', і щоб лапкові шляхи
        # розпакувались (бекслеші всередині лишаються).
        args = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a
                for a in shlex.split(command, posix=False)]
        # Вивід дочірнього процесу читаємо як UTF-8 (а не в локальній кодовій сторінці —
        # на Windows cp1251 давала «РЅР° РїРѕСЂС‚Сѓ» замість кирилиці) і просимо Python-діти писати UTF-8.
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout, shell=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))      # без чорних вікон у збірці без консолі
        return ShellResult(True, p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return ShellResult(True, -1, "", f"timeout {timeout}s")
    except FileNotFoundError as e:
        return ShellResult(True, -1, "", f"не знайдено: {e}")
