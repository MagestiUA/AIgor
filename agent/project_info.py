"""Інформація про робочу теку проєкту для бічної панелі: гілка git і зміни (як у
панелі Claude Code: «Changes +N −M»). Лише читання; будь-яка помилка/відсутність
git → порожній результат, UI просто не показує секцію."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str, timeout: float = 4.0) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def git_info(root: str | Path, max_files: int = 12) -> dict:
    """{"is_repo", "branch", "add", "del", "files": [{path, add, del}], "more": N}.
    Зміни = tracked відносно HEAD (numstat) + нові untracked файли (рядки не рахуємо)."""
    empty = {"is_repo": False, "branch": "", "add": 0, "del": 0, "files": [], "more": 0}
    if not root:
        return empty
    r = Path(root)
    if not r.is_dir():
        return empty
    branch = _git(r, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:                       # репозиторій без жодного коміту (unborn branch)
        branch = _git(r, "symbolic-ref", "--short", "HEAD")
    if branch is None:
        return empty
    files: list[dict] = []
    total_add = total_del = 0
    numstat = _git(r, "diff", "HEAD", "--numstat") or _git(r, "diff", "--numstat") or ""
    for ln in numstat.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        a = int(parts[0]) if parts[0].isdigit() else 0         # '-' для бінарних
        d = int(parts[1]) if parts[1].isdigit() else 0
        files.append({"path": parts[2], "add": a, "del": d})
        total_add += a
        total_del += d
    untracked = (_git(r, "ls-files", "--others", "--exclude-standard") or "").splitlines()
    for path in untracked:
        files.append({"path": path, "add": 0, "del": 0, "new": True})
    return {"is_repo": True, "branch": branch.strip(), "add": total_add, "del": total_del,
            "files": files[:max_files], "more": max(0, len(files) - max_files)}
