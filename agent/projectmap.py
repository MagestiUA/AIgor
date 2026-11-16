"""Карта проєкту: що де лежить і що в кожному файлі — додається в контекст КОЖНОГО запиту
код-режиму, щоб модель знала, з якою частиною пайплайну працює і який файл читати/правити.

Зберігається у ПРОФІЛІ користувача (~/.local-code-agent/projects/<хеш теки>/map.json), а не
в проєкті: це може бути прод-код під git. Скелет будується ДЕТЕРМІНОВАНО (AST для Python, прості
регулярки для решти) — без LLM, тож він точний і безкоштовний; модель лише додає нотатки
(тул project_note).

Застарівання — «м'яко», як у людини. Для кожного файлу ми пам'ятаємо, ЩО БУЛО, коли його
читали востаннє (read_summary), і відбиток вмісту на той момент:
  ○ ще не читав · ✓ читав, файл не змінився · ⚠ змінено після читання (показуємо, що було раніше).
Власні правки Ігоря (write_file/edit_file) оновлюють запис одразу; зовнішні зміни виявляються за
mtime/розміром і хешем при кожному зверненні. Модель не ув'язнена в карті: це підказка, а не істина.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import limits
from .project import SKIP_DIRS

STORE_DIR = Path.home() / ".local-code-agent" / "projects"
MAX_FILES = 1500
MAX_BYTES = 1_000_000
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
            ".cs", ".rb", ".php", ".kt", ".swift", ".sh", ".ps1", ".bat", ".sql", ".html", ".css", ".md",
            ".yml", ".yaml", ".toml", ".json", ".ini", ".cfg"}
NAMED = {"Dockerfile", "docker-compose.yml", "requirements.txt", "Makefile", "package.json", "pyproject.toml"}
HEADER = ("Карта проєкту (автоматично складена; ○ ще не читав · ✓ читав, без змін · ⚠ змінено після читання). "
          "Це підказка, не істина: перед правкою завжди читай актуальний файл. Якщо побачиш, що опис "
          "застарів (файл змінився чи ти його змінив), онови нотатку викликом project_note(path, note).")


def project_key(root: str | Path) -> str:
    return hashlib.sha1(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()[:12]


def _map_file(root) -> Path:
    return STORE_DIR / project_key(root) / "map.json"


def load(root) -> dict:
    try:
        return json.loads(_map_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"root": str(Path(root).resolve()), "version": 1, "files": {}}


def save(root, m: dict) -> None:
    f = _map_file(root)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        pass                                                   # карта — допоміжна, не валимо запит


# ── скелет файлу ─────────────────────────────────────────────────────────────
def _py_skeleton(text: str) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "(синтаксична помилка — не розібрано)"
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    classes, funcs, imports, entry = [], [], [], False
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            ms = [m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes.append(f"{n.name}({', '.join(ms[:6])}{'…' if len(ms) > 6 else ''})" if ms else n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(n.name)
        elif isinstance(n, ast.Import):
            imports += [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            imports.append(("." * n.level) + (n.module or "").split(".")[0])
        elif isinstance(n, ast.If) and "__name__" in ast.dump(n.test) and "__main__" in ast.dump(n.test):
            entry = True
    parts = []
    if doc:
        parts.append(f"«{doc[0][:90]}»")
    if classes:
        parts.append("class " + "; ".join(classes[:14]) + ("…" if len(classes) > 14 else ""))
    if funcs:
        parts.append("def " + ", ".join(funcs[:10]) + ("…" if len(funcs) > 10 else ""))
    uniq = list(dict.fromkeys(i for i in imports if i))
    if uniq:
        parts.append("імпорт: " + ", ".join(uniq[:7]) + ("…" if len(uniq) > 7 else ""))
    if entry:
        parts.append("▶ точка входу")
    return " | ".join(parts)[:1200]


_JS_DECL = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\*?|class|const|let)\s+([A-Za-z_$][\w$]*)", re.M)


def skeleton_for(rel: str, text: str) -> str:
    ext = Path(rel).suffix.lower()
    if ext == ".py":
        return _py_skeleton(text)
    if ext in (".js", ".ts", ".tsx", ".jsx", ".mjs"):
        names = list(dict.fromkeys(_JS_DECL.findall(text)))
        return ("визначає: " + ", ".join(names[:10]) + ("…" if len(names) > 10 else "")) if names else ""
    if ext == ".md":
        m = re.search(r"^#\s+(.+)$", text, re.M)
        return f"«{m.group(1).strip()[:90]}»" if m else ""
    if Path(rel).name == "package.json" or ext == ".json":
        try:
            d = json.loads(text)
            return ("ключі: " + ", ".join(list(d)[:8])) if isinstance(d, dict) else ""
        except ValueError:
            return ""
    return ""


# ── оновлення карти ──────────────────────────────────────────────────────────
def _iter_files(root: Path):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git"))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if p.suffix.lower() not in CODE_EXT and f not in NAMED:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_BYTES:
                continue
            n += 1
            if n > MAX_FILES:
                return
            yield p.relative_to(root).as_posix(), p, st


def _fingerprint(p: Path) -> tuple[str, str]:
    text = p.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16], text


def _update_entry(entry: dict | None, rel: str, p: Path, st) -> dict:
    sig = [st.st_mtime_ns, st.st_size]
    if entry and entry.get("sig") == sig:
        return entry                                         # не змінювався — нічого не читаємо
    fp, text = _fingerprint(p)
    e = dict(entry or {})
    changed = e.get("fp") not in (None, fp)
    e.update({"sig": sig, "fp": fp, "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
              "skeleton": skeleton_for(rel, text)})
    if changed:
        e["changed_at"] = time.time()
    return e


def refresh(root: str | Path) -> dict:
    """Синхронізувати карту з диском: нові/змінені файли перераховуємо, видалені прибираємо."""
    root = Path(root)
    m = load(root)
    files, dirty = m.setdefault("files", {}), False
    seen = set()
    for rel, p, st in _iter_files(root):
        seen.add(rel)
        new = _update_entry(files.get(rel), rel, p, st)
        if new is not files.get(rel):
            files[rel] = new
            dirty = True
    for rel in list(files):
        if rel not in seen:
            del files[rel]
            dirty = True
    if dirty:
        m["updated"] = time.time()
        save(root, m)
    return m


def _entry_for(root: Path, rel: str, m: dict) -> dict | None:
    p = root / rel
    try:
        st = p.stat()
    except OSError:
        return None
    e = _update_entry(m["files"].get(rel), rel, p, st)
    m["files"][rel] = e
    return e


def mark_read(root: str | Path, rel: str) -> None:
    """Модель щойно прочитала файл: запам'ятовуємо відбиток і ЩО БУЛО в ньому (скелет)."""
    root = Path(root)
    rel = Path(rel).as_posix().lstrip("./")
    m = load(root)
    e = _entry_for(root, rel, m)
    if e is None:
        return
    e["read_fp"], e["read_at"], e["read_summary"] = e["fp"], time.time(), e.get("skeleton", "")
    save(root, m)
    _CACHE.clear()


def refresh_file(root: str | Path, rel: str) -> None:
    """Файл змінила сама модель: оновлюємо запис і вважаємо його «щойно прочитаним»."""
    mark_read(root, rel)


def set_note(root: str | Path, rel: str, note: str) -> bool:
    root = Path(root)
    rel = Path(rel).as_posix().lstrip("./")
    m = load(root)
    e = _entry_for(root, rel, m)
    if e is None:
        return False
    e["note"], e["note_fp"] = " ".join((note or "").split())[:300], e["fp"]
    save(root, m)
    _CACHE.clear()
    return True


# ── відображення ─────────────────────────────────────────────────────────────
def _mark(e: dict) -> str:
    if not e.get("read_fp"):
        return "○"
    return "✓" if e["read_fp"] == e.get("fp") else "⚠"


def _line(rel: str, e: dict, indent: str, skel: int = 1200) -> str:
    mark = _mark(e)
    head = f"{indent}{Path(rel).name} {mark} [{e.get('lines', 0)}л]"
    extra = []
    if e.get("skeleton") and skel:
        sk = e["skeleton"]
        extra.append(sk if len(sk) <= skel else sk[:skel] + "…")
    if mark == "⚠" and e.get("read_summary"):
        extra.append(f"(було, коли читав: {e['read_summary'][:140]})")
    if e.get("note"):
        stale = " (можливо застаріла)" if e.get("note_fp") != e.get("fp") else ""
        extra.append(f"нотатка: {e['note']}{stale}")
    return head + (" — " + " ".join(extra) if extra else "")


def _is_test(rel: str) -> bool:
    name = Path(rel).name
    return rel.startswith(("tests/", "test/")) or name.startswith("test_") or name.endswith("_test.py")


def render(m: dict, budget: int) -> str:
    """Три рівні: (0) повні описи → (1) короткі описи, без описів для тестів → (2) лише імена з
    позначками. Повертає перший, що вміщається в бюджет; крайній випадок — жорстке обрізання."""
    files = m.get("files", {})
    if not files:
        return ""
    by_dir: dict[str, list[str]] = {}
    for rel in sorted(files):
        by_dir.setdefault(str(Path(rel).parent.as_posix()) if "/" in rel else ".", []).append(rel)

    def with_skeletons(skel: int, skip_tests: bool, note: str = "") -> str:
        out = [HEADER] + ([note] if note else [])
        for d in sorted(by_dir):
            if d != ".":
                out.append(f"{d}/")
            ind = "  " if d != "." else ""
            for rel in by_dir[d]:
                s = 0 if (skip_tests and _is_test(rel)) else skel
                out.append(_line(rel, files[rel], ind, s))
        return "\n".join(out)

    for skel, skip, note in ((1200, False, ""), (140, True, "(карту скорочено: описи обрізано)")):
        text = with_skeletons(skel, skip, note)
        if len(text) <= budget:
            return text
    out = [HEADER, "(карту стиснуто: список імен; деталі файлу — read_file/project_note)"]
    for d in sorted(by_dir):
        names = " ".join(f"{Path(r).name}{_mark(files[r])}" for r in by_dir[d])
        out.append(f"{'' if d == '.' else d + '/'}: {names}")
    text = "\n".join(out)
    return text if len(text) <= budget else text[:budget - 20] + "\n… (обрізано)"


_CACHE: dict[str, tuple[float, str]] = {}


def context_block(root: str | Path | None, budget: int | None = None, ttl: float = 2.0) -> str:
    """Готовий блок для контексту: синхронізує карту з диском і малює її у бюджет. Короткий
    кеш (ttl), щоб кілька викликів поспіль у межах одного кроку не сканували теку."""
    if not root or not Path(root).is_dir():
        return ""
    budget = budget or limits.get().map_chars
    key = f"{Path(root).resolve()}|{budget}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        text = render(refresh(root), budget)
    except Exception:                                          # noqa: BLE001 — карта не має ламати запит
        text = ""
    _CACHE[key] = (time.time(), text)
    return text


def invalidate() -> None:
    _CACHE.clear()
