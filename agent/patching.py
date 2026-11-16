"""Точкові правки файлу: модель повертає лише змінені фрагменти як блоки SEARCH/REPLACE, а ми
застосовуємо їх ДЕТЕРМІНОВАНО. Раніше edit_file змушував модель друкувати файл цілком: для файлу на
59k символів це ~17k токенів і 2 хвилини на кожну спробу (діагностика тестера, b_0.0.11).

Формат блоку:
    <<<<<<< SEARCH
    наявні рядки (дослівно, цілі рядки)
    =======
    нові рядки
    >>>>>>> REPLACE

Правила застосування (усе — по цілих рядках):
  1) SEARCH має збігтися ТОЧНО й бути ЄДИНИМ у файлі; якщо точного збігу нема — шукаємо ігноруючи
     відступи й пробіли на краях рядків (таби/пробіли, \\r) — теж лише коли збіг єдиний;
  2) порожній SEARCH = дописати REPLACE в кінець файлу; порожній REPLACE = видалити знайдені рядки;
  3) відступи нового тексту приводяться до стилю файлу (таб/пробіли), щоб не отримати суміш.
Помилки повертаються текстом із підказкою (де у файлі схожий фрагмент) — для повторної спроби моделі.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

BLOCK_RE = re.compile(
    r"<{5,9} SEARCH[ \t]*\r?\n(.*?)\r?\n?={5,9}[ \t]*\r?\n(.*?)\r?\n?>{5,9} REPLACE",
    re.DOTALL,
)


@dataclass
class Block:
    search: list[str]
    replace: list[str]


def parse_blocks(text: str) -> list[Block]:
    """Усі блоки SEARCH/REPLACE з відповіді моделі (код-огорожі ``` навколо не заважають)."""
    out = []
    for m in BLOCK_RE.finditer(text or ""):
        s, r = m.group(1), m.group(2)
        out.append(Block(s.split("\n") if s else [], r.split("\n") if r else []))
    return out


def _norm(line: str) -> str:
    return line.strip()


def _find(lines: list[str], search: list[str], norm) -> list[int]:
    """Початкові індекси всіх збігів послідовності рядків search у lines."""
    k = len(search)
    if k == 0 or k > len(lines):
        return []
    key = [norm(x) for x in search]
    return [i for i in range(len(lines) - k + 1) if [norm(x) for x in lines[i:i + k]] == key]


def _fit_indent(rep: list[str], style: str | None) -> list[str]:
    """Привести провідні відступи нових рядків до стилю файлу (таб/пробіли)."""
    if style not in ("tab", "space"):
        return rep
    out = []
    for ln in rep:
        if style == "tab" and ln.startswith("    ") and "\t" not in ln[: len(ln) - len(ln.lstrip(" \t"))]:
            n = len(ln) - len(ln.lstrip(" "))
            ln = "\t" * (n // 4) + " " * (n % 4) + ln[n:]
        elif style == "space" and ln.startswith("\t"):
            n = len(ln) - len(ln.lstrip("\t"))
            ln = "    " * n + ln[n:]
        out.append(ln)
    return out


def hint(lines: list[str], search: list[str]) -> str:
    """Де у файлі найсхожіший рядок на перший змістовний рядок SEARCH — для повторної спроби."""
    first = next((s for s in search if s.strip()), "")
    if not first:
        return ""
    cands = {_norm(x): i for i, x in enumerate(lines) if x.strip()}
    near = difflib.get_close_matches(_norm(first), list(cands), n=1, cutoff=0.6)
    if not near:
        return ""
    i = cands[near[0]]
    lo, hi = max(0, i - 1), min(len(lines), i + 4)
    return "Схоже місце у файлі (скопіюй рядки ТОЧНО):\n" + "\n".join(f"{j + 1}| {lines[j]}" for j in range(lo, hi))


def apply_block(lines: list[str], block: Block, style: str | None = None) -> tuple[list[str] | None, str]:
    """(нові_рядки, "") або (None, причина). lines не змінюється."""
    rep = _fit_indent(block.replace, style)
    if not block.search:                              # дописати в кінець
        return lines + rep, ""
    for norm in (lambda x: x, lambda x: x.strip()):   # точно, потім — без відступів по краях
        hits = _find(lines, block.search, norm)
        if len(hits) == 1:
            i = hits[0]
            return lines[:i] + rep + lines[i + len(block.search):], ""
        if len(hits) > 1:
            return None, (f"SEARCH трапляється {len(hits)} разів — додай 1–3 сусідні рядки, "
                          "щоб збіг був єдиним.")
    h = hint(lines, block.search)
    return None, "SEARCH не знайдено у файлі." + (("\n" + h) if h else "")


def apply_blocks(text: str, blocks: list[Block], style: str | None = None) -> tuple[str, list[tuple[int, str]]]:
    """Послідовно застосувати блоки. (новий_текст, [(номер_блоку_з_1, причина)] для невдалих).
    Невдалі блоки пропускаються, решта застосовується."""
    lines = text.split("\n")
    failed: list[tuple[int, str]] = []
    for n, b in enumerate(blocks, 1):
        new, why = apply_block(lines, b, style)
        if new is None:
            failed.append((n, why))
        else:
            lines = new
    return "\n".join(lines), failed
