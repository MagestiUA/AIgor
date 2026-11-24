"""Карта проєкту (agent/projectmap.py): скелет, відбитки «що було, коли читав», власні правки,
нотатки, бюджет, зв'язок із тулами. STORE_DIR — тимчасова тека (профіль користувача не чіпаємо)."""
import os
import tempfile
import time
from pathlib import Path

from agent import limits, projectmap as pm
from agent.toolkit import ToolContext, default_registry

limits.FORCE = limits.production()

APP = '''"""Головний модуль сервісу."""
import os
import requests
from .db import Database


class Service(Base):
    def start(self):
        pass

    def stop(self):
        pass


def helper():
    pass


if __name__ == "__main__":
    Service().start()
'''


def bump_mtime(p: Path) -> None:
    t = time.time() + 5
    os.utime(p, (t, t))                 # гарантовано інший mtime без sleep


def main() -> None:
    real = pm.STORE_DIR
    pm.STORE_DIR = Path(tempfile.mkdtemp()) / "projects"
    try:
        root = Path(tempfile.mkdtemp())
        (root / "pkg").mkdir()
        (root / "pkg" / "app.py").write_text(APP, encoding="utf-8")
        (root / "pkg" / "bad.py").write_text("def f(:\n", encoding="utf-8")
        (root / "README.md").write_text("# Мій сервіс\n\nопис", encoding="utf-8")
        (root / "web.js").write_text("export function render() {}\nclass Page {}\n", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "x.js").write_text("ignored", encoding="utf-8")
        (root / "data.bin").write_bytes(bytes(100))

        # ключ стабільний, зберігання — в профілі (не в проєкті)
        assert pm.project_key(root) == pm.project_key(str(root))
        m = pm.refresh(root)
        assert set(m["files"]) == {"pkg/app.py", "pkg/bad.py", "README.md", "web.js"}, set(m["files"])
        assert not list(root.glob("**/map.json")) and pm._map_file(root).is_file()

        # скелет: Python (docstring, класи з методами, функції, імпорти, точка входу), JS, md, зламаний py
        sk = m["files"]["pkg/app.py"]["skeleton"]
        assert "Головний модуль" in sk and "class Service(start, stop)" in sk and "def helper" in sk
        assert "імпорт: os, requests, .db" in sk and "точка входу" in sk, sk
        assert "render" in m["files"]["web.js"]["skeleton"] and "Page" in m["files"]["web.js"]["skeleton"]
        assert "Мій сервіс" in m["files"]["README.md"]["skeleton"]
        assert "синтаксична помилка" in m["files"]["pkg/bad.py"]["skeleton"]

        # позначки: нічого не читав -> ○
        text = pm.render(m, 10_000)
        assert "app.py ○" in text and text.startswith("Карта проєкту") and "pkg/" in text

        # прочитав -> ✓ ; файл змінився ЗОВНІ -> ⚠ + «що було, коли читав»
        pm.mark_read(root, "pkg/app.py")
        assert "app.py ✓" in pm.render(pm.load(root), 10_000)
        (root / "pkg" / "app.py").write_text(APP.replace("def helper", "def renamed"), encoding="utf-8")
        bump_mtime(root / "pkg" / "app.py")
        txt = pm.render(pm.refresh(root), 10_000)
        assert "app.py ⚠" in txt and "було, коли читав" in txt and "def helper" in txt and "def renamed" in txt, txt

        # власна правка Ігоря (refresh_file) -> знову ✓, скелет свіжий
        pm.refresh_file(root, "pkg/app.py")
        txt = pm.render(pm.refresh(root), 10_000)
        assert "app.py ✓" in txt and "def renamed" in txt

        # нотатка: зберігається; після зміни файлу стає «можливо застаріла»
        assert pm.set_note(root, "pkg/app.py", "точка входу сервісу") and not pm.set_note(root, "нема.py", "x")
        assert "нотатка: точка входу сервісу" in pm.render(pm.load(root), 10_000)
        assert "застаріла" not in pm.render(pm.load(root), 10_000)
        (root / "pkg" / "app.py").write_text(APP + "\n# зміна\n", encoding="utf-8")
        bump_mtime(root / "pkg" / "app.py")
        assert "можливо застаріла" in pm.render(pm.refresh(root), 10_000)

        # видалений файл зникає, новий з'являється
        (root / "web.js").unlink()
        (root / "new.py").write_text("x = 1\n", encoding="utf-8")
        assert set(pm.refresh(root)["files"]) == {"pkg/app.py", "pkg/bad.py", "README.md", "new.py"}

        # бюджет: компактний режим (імена з позначками) і жорстке обрізання
        big = pm.refresh(root)
        full = len(pm.render(big, 10_000))
        seen = set()
        for b in range(len(pm.HEADER) + 100, full, 25):            # будь-який бюджет: вміщається й деградує поступово
            txt = pm.render(big, b)
            assert len(txt) <= b, (b, len(txt))
            seen |= {m for m in ("скорочено", "стиснуто") if m in txt}
        assert "стиснуто" in seen, seen
        assert len(pm.render(big, 120)) <= 120

        # контекстний блок: кеш не тримає застарілого після mark_read/set_note
        pm.invalidate()
        b1 = pm.context_block(root, budget=10_000)
        pm.mark_read(root, "new.py")
        assert "new.py ✓" in pm.context_block(root, budget=10_000) and b1 != pm.context_block(root, budget=10_000)
        assert pm.context_block(None) == "" and pm.context_block(root / "нема") == ""

        # зв'язок із тулами (ToolContext.track_project): читання -> ✓, запис -> оновлення, project_note
        reg = default_registry()
        ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"}, track_project=True)
        pm.invalidate()
        reg.dispatch("read_file", {"path": "README.md"}, ctx)
        assert "README.md ✓" in pm.render(pm.load(root), 10_000)
        reg.dispatch("write_file", {"path": "gen.py", "content": "def made():\n    pass\n"}, ctx)
        e = pm.load(root)["files"]["gen.py"]
        assert "def made" in e["skeleton"] and e["read_fp"] == e["fp"]          # власний запис = «знаю вміст»
        assert "збережено" in reg.dispatch("project_note", {"path": "gen.py", "note": "генерований модуль"}, ctx)
        assert "нотатка: генерований модуль" in pm.render(pm.load(root), 10_000)
        assert "не знайдено" in reg.dispatch("project_note", {"path": "нема.py", "note": "x"}, ctx)
        # без прапора (чат-режим) тули карту НЕ чіпають
        before = pm.load(root)["files"]["README.md"]["read_at"]
        time.sleep(0.01)
        reg.dispatch("read_file", {"path": "README.md"}, ToolContext(root=root, permissions={"edits": "off", "shell": "off"}))
        assert pm.load(root)["files"]["README.md"]["read_at"] == before
        # результат-помилка читання не позначається як прочитане
        reg.dispatch("read_file", {"path": "нема.py"}, ctx)
        assert "нема.py" not in pm.load(root)["files"]
    finally:
        pm.STORE_DIR = real
    print("OK: projectmap — скелет, ○/✓/⚠ з «що було», власні правки, нотатки, бюджет, тули")


if __name__ == "__main__":
    main()
