"""Класифікатор shell-команд: "safe" | "danger".

Використовується режимом shell="smart" — виконує безпечні команди автоматично,
зупиняє на підтвердження тільки справді небезпечні (rm -rf, format, системні теки).
"""
from __future__ import annotations

import re

# Шаблони, що вважаються небезпечними (перевіряються у нижньому регістрі).
# Кожен елемент — рядок-підрядок або re.Pattern.
DANGER_PATTERNS: list[str | re.Pattern] = [
    # Рекурсивне/форсоване видалення
    "rm -rf", "rm -fr", "rm --force",
    "del /s", "del /f",
    "rmdir /s", "rd /s",
    # Форматування носіїв
    "format ", "mkfs", "diskpart",
    # Системні директорії Windows
    r"c:\windows", r"c:\system32", r"c:\program files",
    "%systemroot%", "%windir%",
    # Системні директорії Linux/Mac
    "/etc/", "/usr/", "/bin/", "/sbin/", "/boot/", "/lib/",
    # Зміна прав/власника на системне
    re.compile(r"icacls\s+.*\\(system32|windows)\b", re.IGNORECASE),
    re.compile(r"chmod\s+.*\s+/etc/", re.IGNORECASE),
    re.compile(r"chown\s+.*\s+/etc/", re.IGNORECASE),
    # Мережеві деструктивні операції
    "netsh winsock reset", "netsh int ip reset",
    "iptables -f", "iptables --flush",
    # Зупинка/видалення сервісів
    "sc delete", "sc stop",
    "systemctl disable", "systemctl stop",
    "net stop", "taskkill /f",
    # Знищення дисків/розділів
    re.compile(r"dd\s+if=.*of=/dev/(sd|hd|nvme)", re.IGNORECASE),
    # PowerShell небезпечне
    re.compile(r"remove-item\s+.*-recurse\s+-force", re.IGNORECASE),
    re.compile(r"format-volume\b", re.IGNORECASE),
]


def classify(cmd: str) -> str:
    """Повернути "danger" якщо команда потенційно деструктивна, інакше "safe"."""
    low = cmd.lower().strip()
    for pattern in DANGER_PATTERNS:
        if isinstance(pattern, re.Pattern):
            if pattern.search(cmd):
                return "danger"
        else:
            if pattern.lower() in low:
                return "danger"
    return "safe"


# ── Читальні команди: пошук/перегляд без змін (для детектора «читання без дій», agent_loop.ReadStreak) ──
READ_VERBS = {
    "grep", "egrep", "fgrep", "rg", "ag", "findstr", "select-string", "sls", "ls", "dir", "get-childitem", "gci", "tree",
    "cat", "type", "get-content", "gc", "head", "tail", "wc", "sort", "uniq", "cut", "find", "where", "which",
    "get-command", "pwd", "echo", "write-output", "test-path", "stat", "file", "measure-object", "select-object",
    "foreach-object", "where-object", "format-table", "ft", "get-item", "get-itemproperty", "resolve-path",
}
READ_GIT = {"log", "grep", "diff", "show", "status", "ls-files", "blame", "branch", "rev-parse", "describe", "shortlog"}
_SAFE_REDIR = re.compile(r"\d?>\s*(&\d|\$null|/dev/null|nul)\b", re.IGNORECASE)
_WRITE_HINTS = re.compile(r"(-delete\b|-exec\b|\bsed\s+-i|\bremove-item\b|\bset-content\b|\badd-content\b|\bout-file\b|"
                          r"\btee\b|\bnew-item\b|\bmove-item\b|\bcopy-item\b|\brename-item\b)", re.IGNORECASE)


def is_read_command(cmd: str) -> bool:
    """Чи це лише пошук/перегляд (grep, ls, cat, git log…), без запису й без запуску програм. Усі сегменти
    конвеєра/послідовності (; && || |) мусять починатися з читального дієслова, а перенаправлень запису нема."""
    cmd = (cmd or "").strip()
    if not cmd or ">" in _SAFE_REDIR.sub("", cmd) or _WRITE_HINTS.search(cmd):
        return False
    for seg in re.split(r"\|\||&&|;|\|", cmd):
        words = seg.strip().lstrip("&").strip().split()
        if not words:
            continue
        w0 = words[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if w0 == "git":
            if len(words) < 2 or words[1].lower() not in READ_GIT:
                return False
        elif w0 not in READ_VERBS:
            return False
    return True
