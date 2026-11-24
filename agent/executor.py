"""Executor — виконує ОДИН крок над ОДНИМ файлом.

Пайплайн (think=off, вузький контекст):
  read -> strip(дані в плейсхолдери) -> LLM(whole) -> ast.parse -> restore -> diff
Опакові дані крізь модель не проходять, тож не псуються й не роздувають контекст.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import config, patching, tools
from .literals import restore_code, strip_code
from .llm import OllamaClient

SYSTEM = (
    "You are a precise code editor. You receive the FULL content of ONE Python file "
    "and an instruction. Return the COMPLETE new content of that file and nothing "
    "else: no explanations, no commentary. You may wrap it in a ```python fence. "
    "Tokens like __LIT_0__ are placeholders for large data that was removed; keep "
    "them EXACTLY as they are, do not alter them. You may delete a placeholder line "
    "only if the instruction explicitly asks to remove that commented/dead code."
)


@dataclass
class EditResult:
    ok: bool
    final_code: str | None
    diff: str
    error: str
    backup: str | None


def _extract_code(text: str) -> str:
    """Дістати код: перший ```-блок, якщо є; інакше весь текст."""
    t = (text or "").strip()
    m = re.search(r"```[\w+.-]*\s*\n(.*?)```", t, re.DOTALL)
    if m:
        return m.group(1).rstrip("\n")
    return t


def _is_python(path: str | Path) -> bool:
    return str(path).lower().endswith((".py", ".pyw"))


def _fence_lang(path: str | Path) -> str:
    return "python" if _is_python(path) else (Path(str(path)).suffix.lstrip(".").lower() or "text")


def run_edit(path: str | Path, instruction: str,
             client: OllamaClient | None = None, write: bool = False,
             project_doc: str | None = None) -> EditResult:
    client = client or OllamaClient()
    original = tools.read_file(path)
    body = tools.text_for_model(original)       # модель бачить \n; CRLF оригіналу повертає match_style

    py = _is_python(path)                       # strip літералів і ast-перевірка — лише для .py
    stripped, mapping = strip_code(body) if py else (body, {})
    preamble = f"Project context:\n{project_doc}\n\n" if project_doc else ""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"{preamble}Instruction:\n{instruction}\n\n"
                    f"File `{path}`:\n```{_fence_lang(path)}\n{stripped}\n```"},
    ]
    msg = client.chat(messages, profile=config.EXECUTOR)
    new_stripped = _extract_code(msg.get("content") or "")

    if py:
        ok, err = tools.validate_python(new_stripped)
        if not ok:
            return EditResult(False, None, "", f"вивід LLM невалідний: {err}", None)

    final = restore_code(new_stripped, mapping)
    if py:
        ok2, err2 = tools.validate_python(final)
        if not ok2:
            return EditResult(False, None, "", f"після restore невалідно: {err2}", None)

    if body.endswith("\n") and not final.endswith("\n"):         # _extract_code обрізає кінцевий перенос
        final += "\n"
    final, _note = tools.match_style(original, final, str(Path(path).name))
    diff = tools.unified_diff(original, final, str(Path(path).name))

    backup = None
    if write:
        backup = tools.backup_file(path)
        tools.write_file(path, final)

    return EditResult(True, final, diff, "", backup)


PATCH_SYSTEM = (
    "You are a precise code editor. You receive the FULL content of ONE file and an instruction. "
    "Do NOT return the whole file. Return ONLY the changes, as one or more SEARCH/REPLACE blocks:\n"
    "<<<<<<< SEARCH\n(existing lines, copied exactly)\n=======\n(new lines)\n>>>>>>> REPLACE\n"
    "Rules:\n"
    "- SEARCH must be WHOLE existing lines copied character for character (keep the indentation) and must "
    "occur exactly once in the file; add 1-3 neighbouring lines of context if needed to make it unique.\n"
    "- Keep blocks small: change only what the instruction requires. Several blocks are allowed.\n"
    "- To insert code: put an anchor line in SEARCH and repeat it in REPLACE together with the new lines.\n"
    "- To delete lines: leave the REPLACE part empty. To append to the end of the file: leave SEARCH empty.\n"
    "- Match the file's indentation style (tabs or spaces) exactly.\n"
    "- Tokens like __LIT_0__ are placeholders for large data that was removed; never touch them.\n"
    "Output nothing except the blocks (no explanations)."
)
WHOLE_FILE_MAX = 12_000        # символів: до цього розміру при невдачі патчів можна переписати файл цілком
PATCH_ROUNDS = 2               # перша спроба + одна повторна з поясненням, що не збіглось


def run_patch(path: str | Path, instruction: str,
              client: OllamaClient | None = None, write: bool = False,
              project_doc: str | None = None) -> EditResult:
    """Точкова правка: модель повертає лише змінені фрагменти (SEARCH/REPLACE), ми застосовуємо їх самі.
    Файл у 59k символів — це десятки токенів відповіді замість ~17k (див. agent/patching.py).
    Нічого не записується, поки всі блоки не застосувалися й результат не пройшов перевірки."""
    client = client or OllamaClient()
    original = tools.read_file(path)
    body = tools.text_for_model(original)
    py = _is_python(path)
    stripped, mapping = strip_code(body) if py else (body, {})
    style = tools.detect_indent(body)
    preamble = f"Project context:\n{project_doc}\n\n" if project_doc else ""
    messages = [
        {"role": "system", "content": PATCH_SYSTEM},
        {"role": "user",
         "content": f"{preamble}Instruction:\n{instruction}\n\n"
                    f"File `{path}`:\n```{_fence_lang(path)}\n{stripped}\n```"},
    ]
    current, failed = stripped, [(0, "модель не відповіла")]
    for rnd in range(PATCH_ROUNDS):
        msg = client.chat(messages, profile=config.EXECUTOR)
        reply = msg.get("content") or ""
        blocks = patching.parse_blocks(reply)
        if not blocks:
            failed = [(0, "відповідь не містить жодного блоку SEARCH/REPLACE")]
        else:
            current, failed = patching.apply_blocks(current, blocks, style)
        if not failed or rnd == PATCH_ROUNDS - 1:
            break
        why = "\n".join(f"- блок {n}: {w}" if n else f"- {w}" for n, w in failed)
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "Не всі блоки застосовано:\n" + why +
             "\nУспішні блоки вже застосовано — НЕ повторюй їх. Надішли ЛИШЕ виправлені блоки для невдалих; "
             "SEARCH має точно відповідати поточному тексту файлу."},
        ]

    if failed:
        if len(stripped) <= WHOLE_FILE_MAX:                  # малий файл: дешевше переписати цілком
            return run_edit(path, instruction, client, write, project_doc)
        reasons = "; ".join(f"блок {n}: {w.splitlines()[0]}" if n else w for n, w in failed)
        return EditResult(False, None, "", "не вдалося застосувати правку точково (" + reasons +
                          "). Файл не змінено — уточни інструкцію або вкажи рядки/функцію точніше.", None)
    if current == stripped:
        return EditResult(False, None, "", "правка нічого не змінила (блоки збіглись із поточним текстом)", None)

    if py:
        ok, err = tools.validate_python(current)
        if not ok:
            return EditResult(False, None, "", f"після правки синтаксична помилка: {err}", None)
    final = restore_code(current, mapping)
    if py:
        ok2, err2 = tools.validate_python(final)
        if not ok2:
            return EditResult(False, None, "", f"після restore невалідно: {err2}", None)

    final, _note = tools.match_style(original, final, str(Path(path).name))
    diff = tools.unified_diff(original, final, str(Path(path).name))
    backup = None
    if write:
        backup = tools.backup_file(path)
        tools.write_file(path, final)
    return EditResult(True, final, diff, "", backup)


def create_from_source(target: str | Path, source_path: str | Path, instruction: str,
                       client: OllamaClient | None = None,
                       project_doc: str | None = None) -> EditResult:
    """Створити НОВИЙ файл на основі джерела (read-only). Літерали джерела
    переносяться verbatim через strip/restore (модель їх не бачить)."""
    client = client or OllamaClient()
    original = tools.read_file(source_path)
    body = tools.text_for_model(original)
    py = _is_python(source_path) and _is_python(target)
    stripped, mapping = strip_code(body) if py else (body, {})
    preamble = f"Project context:\n{project_doc}\n\n" if project_doc else ""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"{preamble}Instruction:\n{instruction}\n\n"
                    f"Base the NEW file on this source `{Path(source_path).name}`:\n"
                    f"```{_fence_lang(source_path)}\n{stripped}\n```\nReturn the COMPLETE new file."},
    ]
    msg = client.chat(messages, profile=config.EXECUTOR)
    new_stripped = _extract_code(msg.get("content") or "")

    if py:
        ok, err = tools.validate_python(new_stripped)
        if not ok:
            return EditResult(False, None, "", f"вивід LLM невалідний: {err}", None)

    final = restore_code(new_stripped, mapping)
    if py:
        ok2, err2 = tools.validate_python(final)
        if not ok2:
            return EditResult(False, None, "", f"після restore невалідно: {err2}", None)

    if body.endswith("\n") and not final.endswith("\n"):
        final += "\n"
    final, _note = tools.match_style(original, final, str(Path(target).name))   # копія — у стилі джерела (CRLF/відступи)
    diff = tools.unified_diff("", final, str(Path(target).name))
    return EditResult(True, final, diff, "", None)


def apply_edit(path: str | Path, result: EditResult) -> str | None:
    """Записати СХВАЛЕНУ правку з бекапом. Викликати після показу diff і згоди
    користувача (CLI/GUI). Повертає шлях бекапу."""
    if not result.ok or result.final_code is None:
        raise ValueError("немає валідного результату для запису")
    backup = tools.backup_file(path)
    tools.write_file(path, result.final_code)
    return backup
