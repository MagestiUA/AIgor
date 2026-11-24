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
    """Вміст файлу З ОРИГІНАЛЬНИМИ закінченнями рядків (newline=""). Звичайний read_text перетворює \\r\\n на \\n,
    а write_text на Windows — \\n на \\r\\n: через це кожен записаний Ігорем файл мовчки ставав CRLF, а match_style
    ніколи не бачив CRLF в оригіналі. Для показу моделі нормалізуйте через text_for_model()."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def text_for_model(text: str) -> str:
    """Текст для моделі/пошуку: лише \\n (моделі пишуть \\n; стиль оригіналу повертає match_style при записі)."""
    return text.replace("\r\n", "\n")


def write_file(path: str | Path, content: str) -> None:
    """Записати рівно ті символи, що передано (без перетворення \\n → \\r\\n на Windows)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


CRLF_SUFFIXES = (".bat", ".cmd")         # нові файли цих типів — з CRLF (cmd.exe спотикається на LF у мітках/goto)


def new_file_style(path: str | Path, content: str) -> str:
    """Закінчення рядків для НОВОГО файлу: як написала модель (\\n), окрім .bat/.cmd."""
    if str(path).lower().endswith(CRLF_SUFFIXES) and "\r\n" not in content:
        return content.replace("\n", "\r\n")
    return content


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


# Живий кейс (фідбек b_0.0.1): модель, якій промпт каже «Windows, PowerShell», слала `cd x && ...`,
# `type`, `Get-ChildItem`, а виконання БЕЗ оболонки (shell=False) давало на кожну однакове
# безіменне «не знайдено: [WinError 2]» — агент не розумів причини й крутився. Для режимів, де
# команду дозволено (smart/ask/auto), тепер запускаємо справжній PowerShell.
PS_PRELUDE = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; $ProgressPreference='SilentlyContinue'; "
PS_EPILOGUE = "; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"


def ps_chain(command: str) -> str:
    """Windows PowerShell 5.1 не розуміє `a && b` (це з PowerShell 7), а моделі пишуть його постійно.
    Переписуємо верхньорівневі `&&` (ПОЗА лапками — `adb shell "cd /sdcard && ls"` лишається як є)
    у `a; if ($?) { b }`. `||` не чіпаємо."""
    parts, buf, quote, i = [], [], "", 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif command.startswith("&&", i):
            parts.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    if len(parts) == 1 or not all(parts):
        return command
    out = parts[-1]
    for part in reversed(parts[:-1]):
        out = f"{part}; if ($?) {{ {out} }}"
    return out


def _run_powershell(command: str, cwd: str | None, timeout: int) -> "ShellResult":
    """Справжній PowerShell: stdin закрито (ssh/adb не зависають на запиті пароля/підтвердження
    ключа — одразу помилка), при таймауті вбиваємо ВСЕ ДЕРЕВО (інакше `adb logcat`/`ssh` лишаються
    сиротами) і віддаємо частковий вивід.

    Вивід беремо через ТИМЧАСОВІ ФАЙЛИ, а не канали: перший `adb ...` запускає фонового демона
    (`adb fork-server`), що успадковує канали й тримає їх відкритими — `communicate()` тоді висить,
    поки не вичерпається таймаут. Файл не потребує EOF: чекаємо лише завершення самого PowerShell."""
    import tempfile
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    nowin = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", PS_PRELUDE + ps_chain(command) + PS_EPILOGUE],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, env=env, creationflags=nowin)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=nowin)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fo.seek(0)
        fe.seek(0)
        out = fo.read().decode("utf-8", errors="replace")
        err = fe.read().decode("utf-8", errors="replace")
    if timed_out:
        tail = (out or err).strip()[-1500:]
        return ShellResult(True, -1, "", f"timeout {timeout}s — команду та її дочірні процеси зупинено."
                                         + (" Частковий вивід: " + tail if tail else ""))
    return ShellResult(True, proc.returncode, out, err)


def run_shell(command: str, cwd: str | None = None, timeout: int | None = None,
              allow=None, allow_all: bool = False, use_shell: bool = False) -> ShellResult:
    """Виконати команду. За замовчуванням лише префікси з allow-list; allow_all=True
    (режим 'ask' після підтвердження) знімає це обмеження. shell=False, тож
    ланцюжки (`a; b`, `a && b`) не інтерпретуються — захист від ін'єкцій."""
    allow = config.ALLOWED_SHELL if allow is None else allow
    timeout = config.SHELL_TIMEOUT if timeout is None else timeout
    if not allow_all and not is_allowed(command, allow):
        return ShellResult(False, -1, "", f"заблоковано allow-list: {command!r}")
    if use_shell and os.name == "nt":
        try:
            return _run_powershell(command, cwd, timeout)
        except FileNotFoundError:
            pass                                   # немає powershell.exe — падаємо на запуск без оболонки
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
        prog = args[0] if args else command.split()[0] if command.split() else command
        return ShellResult(True, -1, "", f"не знайдено програму {prog!r} ({e}). Команда виконується БЕЗ оболонки: вбудовані команди (cd, dir, type) та оператори (&&, |, >) недоступні — виклич програму напряму (python, git, pip) або використай інший тул (read_file, list_dir, write_file).")
