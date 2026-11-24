"""Пошук по файлах (тул `search`) — замість grep/Select-String у консолі.

Навіщо: у кодових агентів пошук — основний спосіб знайти потрібні 20 рядків замість читати файли цілком. Без нього
модель шукала через run_shell у PowerShell 5.1 (лапки, `&&`, відсутній grep), а субагент-дослідник не міг шукати
взагалі й читав файли один за одним. Тут — чистий Python, однаково на будь-якій ОС:
  * регулярний вираз (некоректний — шукаємо як звичайний текст);
  * «розумний регістр»: без великих літер у шаблоні — без урахування регістру (як ripgrep --smart-case);
  * glob-фільтр за іменем/шляхом ("*.py", "src/*.ts", кілька через кому);
  * службові теки (.git, node_modules, .venv…) і бінарні/великі файли пропускаємо.
Вивід — `шлях:рядок: текст`, далі модель читає місце через read_file(path, start_line=…, limit=…).
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .project import SKIP_DIRS

MAX_HITS = 60            # скільки збігів показуємо
COUNT_CAP = 500          # далі не рахуємо (досить знати, що «багато»)
MAX_LINE = 220           # довжина рядка у виводі
MAX_FILE_BYTES = 2_000_000
MAX_FILES = 20_000


def _is_binary(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return True


def _globs(glob: str) -> list[str]:
    return [g.strip() for g in (glob or "").replace(";", ",").split(",") if g.strip()]


def _match_glob(rel: str, globs: list[str]) -> bool:
    if not globs:
        return True
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.lstrip("*/"))
               for g in globs)


def _iter_files(base: Path):
    if base.is_file():
        yield base
        return
    n = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git"))
        for f in sorted(filenames):
            n += 1
            if n > MAX_FILES:
                return
            yield Path(dirpath) / f


def compile_pattern(pattern: str, ignore_case: bool | None = None) -> tuple[re.Pattern, str]:
    """-> (скомпільований вираз, примітка). ignore_case=None — розумний регістр."""
    if ignore_case is None:
        ignore_case = pattern == pattern.lower()
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags), ""
    except re.error:
        return re.compile(re.escape(pattern), flags), " (вираз некоректний — шукаю як звичайний текст)"


def search(base: str | Path, pattern: str, glob: str = "", ignore_case: bool | None = None,
           rel_to: str | Path | None = None, max_hits: int = MAX_HITS) -> str:
    """Знайти рядки за шаблоном у файлі або теці base. rel_to — від чого рахувати показані шляхи (корінь проєкту)."""
    pattern = pattern or ""
    if not pattern.strip():
        return "помилка: порожній шаблон пошуку"
    base = Path(base)
    if not base.exists():
        return f"не знайдено: {base}"
    rx, note = compile_pattern(pattern, ignore_case)
    globs = _globs(glob)
    root = Path(rel_to).resolve() if rel_to else (base if base.is_dir() else base.parent).resolve()
    hits: list[str] = []
    total, files_hit = 0, 0
    for p in _iter_files(base):
        try:
            rp = p.resolve()
            rel = rp.relative_to(root).as_posix() if (rp == root or root in rp.parents) else str(rp)
        except (OSError, ValueError):
            continue
        if not _match_glob(rel, globs):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES or _is_binary(p):
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                found_here = False
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        total += 1
                        found_here = True
                        if len(hits) < max_hits:
                            s = line.strip()
                            hits.append(f"{rel}:{i}: {s if len(s) <= MAX_LINE else s[:MAX_LINE] + '…'}")
                        if total >= COUNT_CAP:
                            break
                files_hit += found_here
        except OSError:
            continue
        if total >= COUNT_CAP:
            break
    if not hits:
        where = f" у {glob}" if globs else ""
        return (f"збігів за «{pattern}»{where} не знайдено{note}. Спробуй інше слово, частину імені або "
                "простіший вираз; структуру тек показує list_dir.")
    more = f", показано перші {len(hits)}" if total > len(hits) else ""
    many = "≥" if total >= COUNT_CAP else ""
    head = f"Знайдено {many}{total} збігів у {files_hit} файлах{more}{note}. Прочитати місце: read_file(path, start_line=N, limit=40)."
    return head + "\n" + "\n".join(hits)
