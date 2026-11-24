"""Нотатки сесії на диску (етап 4 перебудови): знахідки й рішення живуть у файлах, а в контекст іде лише ІНДЕКС.

Навіщо: усе, що виконавець з'ясував (де що лежить, які обмеження, висновки субагента), не треба тягнути в кожен запит —
досить короткого переліку «назва: суть у рядок». Потрібну нотатку модель читає тулом recall(name); нове записує
тулом remember(name, text). Звіт субагента-дослідника зберігається автоматично, тож наступні кроки не досліджують те саме
вдруге. Нотатки — файли ~/.local-code-agent/session-memory/<sid>/<slug>.md (у профілі, не в репозиторії); розмір і
кількість обмежені; видаляються разом із чатом.

Довгі значення (токени, ключі, base64, великі літерали) у нотатках НЕ переказуємо: такий текст замінюється на посилання
(той самий принцип, що в субагента), а справжнє значення беруть із джерела інструментами edit_file/create_from_source.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from .literals import mask_long

ROOT = Path.home() / ".local-code-agent" / "session-memory"
MAX_NOTES = 40            # на сесію (старіші витісняються)
NOTE_CHARS = 6000         # розмір однієї нотатки
INDEX_CHARS = 900         # бюджет індексу в контексті
GIST = 110                # довжина «суті» одного рядка індексу

def _dir(sid: str) -> Path:
    return ROOT / re.sub(r"[^A-Za-z0-9_-]+", "_", sid or "")


def note_dir(sid: str) -> Path:
    """Тека нотаток сесії (модель може шукати в ній search(pattern, path=…) — нотатки звичайні .md-файли)."""
    return _dir(sid)


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", (name or "").strip().lower(), flags=re.UNICODE).strip("-")
    return s[:60] or "note"


def _clean(text: str) -> str:
    return mask_long((text or "").strip())[:NOTE_CHARS]


def save_note(sid: str, name: str, text: str) -> str:
    """Записати (або замінити) нотатку. Повертає її ім'я-слаг. Порожній sid/текст — нічого не робить ('')."""
    body = _clean(text)
    if not sid or not body:
        return ""
    d = _dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    slug = _slug(name)
    (d / f"{slug}.md").write_text(f"# {(name or slug).strip()[:80]}\n{body}\n", encoding="utf-8")
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime)
    for old in files[:max(0, len(files) - MAX_NOTES)]:           # витісняємо найстаріші
        old.unlink(missing_ok=True)
    return slug


def _find(sid: str, name: str) -> Path | None:
    d = _dir(sid)
    if not sid or not d.is_dir():
        return None
    slug = _slug(name)
    exact = d / f"{slug}.md"
    if exact.is_file():
        return exact
    cands = [p for p in d.glob("*.md") if slug in p.stem or p.stem in slug]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def read_note(sid: str, name: str) -> str | None:
    p = _find(sid, name)
    return p.read_text(encoding="utf-8") if p else None


def list_notes(sid: str) -> list[tuple[str, str, int]]:
    """[(слаг, суть, розмір)] від найновіших."""
    d = _dir(sid)
    if not sid or not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        gist = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
        out.append((p.stem, gist[:GIST], p.stat().st_size))
    return out


def index_block(sid: str, budget: int = INDEX_CHARS) -> str:
    """Короткий перелік нотаток для контексту ('' якщо нотаток нема)."""
    notes = list_notes(sid)
    if not notes:
        return ""
    head = ("Нотатки цієї сесії (з'ясоване раніше; щоб прочитати — recall(name), щоб додати — remember(name, text); "
            f"файли нотаток: {note_dir(sid)}):")
    lines, used = [], len(head)
    for slug, gist, size in notes:
        ln = f"- {slug}: {gist}" if gist else f"- {slug}"
        if used + len(ln) + 1 > budget:
            lines.append(f"- … ще {len(notes) - len(lines)} (recall за назвою)")
            break
        lines.append(ln)
        used += len(ln) + 1
    return head + "\n" + "\n".join(lines)


def clear(sid: str) -> None:
    if sid:
        shutil.rmtree(_dir(sid), ignore_errors=True)


def rename(old_sid: str, new_sid: str) -> None:
    """Перенести нотатки старого id у новий (чат до створення сесії)."""
    src, dst = _dir(old_sid), _dir(new_sid)
    if not old_sid or not new_sid or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.md"):
        f.replace(dst / f.name)
    shutil.rmtree(src, ignore_errors=True)
