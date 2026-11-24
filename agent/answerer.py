"""Контекст для режиму аналізу коду (відповіді на питання, без правок): AGENT.md + структура проєкту + згадані в
питанні файли + read-only джерела. Сама відповідь — у спільному циклі з тулами читання (lca_web: _answer_reply →
_run_read_loop), тож модель дочитує решту сама (search / read_file).
"""
from __future__ import annotations

import re
from pathlib import Path

from .project import load_project_doc, scan_structure
from .tools import read_file as _read_raw, text_for_model


def read_file(p) -> str:
    return text_for_model(_read_raw(p))


FILE_RE = re.compile(r"[\w./\\-]+\.[A-Za-z]{1,6}")


def build_context(root: str | Path, reference_files=(), question: str = "",
                  per_file: int = 6000) -> str:
    """Контекст для відповіді: довідка + структура + згадані файли + джерела."""
    root = Path(root)
    parts: list[str] = []

    doc = load_project_doc(root)
    if doc:
        parts.append(f"AGENT.md:\n{doc}")

    parts.append(f"Структура проекту:\n{scan_structure(root)}")

    seen: set[str] = set()
    for name in FILE_RE.findall(question):
        p = root / name
        if p.is_file() and str(p) not in seen:
            seen.add(str(p))
            try:
                parts.append(f"{name}:\n{read_file(p)[:per_file]}")
            except Exception:
                pass

    for rf in reference_files:
        try:
            parts.append(f"Reference {Path(rf).name} (read-only):\n{read_file(rf)[:per_file]}")
        except Exception:
            pass

    return "\n\n".join(parts)
