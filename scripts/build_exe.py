"""Збірка одного файлу dist/Igor_<версія>.exe (PyInstaller --onefile).

Запуск (інтерпретатором ЗБІРНОГО середовища, де стоять requirements + pyinstaller):
    python scripts/build_exe.py            # збірка з поточної версії (файл VERSION)
    python scripts/build_exe.py --bump     # спершу підняти останню цифру версії: b_0.0.1 -> b_0.0.2
    python scripts/build_exe.py --console  # з консольним вікном (для налагодження збірки)

Етапи:
 1. Копія джерел у build/stage (репозиторій і його .web/.venv не чіпаємо).
 2. `reflex export --frontend-only` у stage з сентинелом замість адреси бекенда
    (agent/serve.py підставить реальний порт при запуску) — Node/Bun потрібні лише тут.
 3. PyInstaller: код + залежності + зібраний фронтенд (web_client) + rxconfig/assets/VERSION.
Результат: dist/Igor_<версія>.exe
"""
from __future__ import annotations

import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")      # консоль Windows (cp1251) не мусить ламати збірку
    except Exception:
        pass
from agent.serve import SENTINEL_HOST  # noqa: E402


def bump(ver: str) -> str:
    m = re.match(r"^(.*?)(\d+)$", ver)
    if not m:
        raise SystemExit(f"Не вдалось підняти версію з {ver!r}")
    return f"{m.group(1)}{int(m.group(2)) + 1}"


def _rmtree(path: Path) -> None:
    """Видалити теку надійно: на Windows node_modules часто лишає read-only/довгі шляхи."""
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(path)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if path.exists():
        raise SystemExit(f"Не вдалось видалити {path} — закрийте процеси, що її тримають")


def run(cmd, **kw):
    print(">>", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def main() -> None:
    args = sys.argv[1:]
    vfile = ROOT / "VERSION"
    ver = vfile.read_text(encoding="utf-8").strip()
    if "--bump" in args:
        ver = bump(ver)
        vfile.write_text(ver, encoding="utf-8")
    name = f"Igor_{ver}"
    print(f"=== Збірка {name}.exe ===", flush=True)

    # 1. stage
    # Проміжна збірка — поза проєктом (%TEMP%): IDE (PyCharm) індексує теку проєкту й тримає
    # відкритими файли node_modules, тож видалити build/stage всередині нього не вдається.
    work = Path(os.environ.get("IGOR_BUILD_DIR") or Path(tempfile.gettempdir()) / "igor_build")
    stage = work / "stage"
    _rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for d in ("agent", "lca_web", "assets"):
        if (ROOT / d).is_dir():
            shutil.copytree(ROOT / d, stage / d, ignore=ignore)
    for f in ("main.py", "rxconfig.py", "VERSION", "requirements.txt"):
        shutil.copy2(ROOT / f, stage / f)

    # 2. фронтенд зі сентинелом
    env = {**os.environ, "REFLEX_API_URL": f"http://{SENTINEL_HOST}", "REFLEX_TELEMETRY_ENABLED": "false",
           "PYTHONIOENCODING": "utf-8"}
    reflex = Path(sys.executable).parent / "reflex.exe"
    run([reflex, "export", "--frontend-only", "--no-zip", "--env", "prod"], cwd=stage, env=env)
    client = stage / ".web" / "build" / "client"
    if not (client / "index.html").is_file():
        raise SystemExit("Фронтенд не зібрався: нема .web/build/client/index.html")
    if SENTINEL_HOST not in (stage / ".web" / "env.json").read_text(encoding="utf-8"):
        raise SystemExit("Сентинел не потрапив у env.json — REFLEX_API_URL не спрацював")

    # 3. PyInstaller
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", name,
           "--icon", stage / "assets" / "igor.ico",                  # іконка самого .exe (scripts/make_icon.py)
           "--distpath", ROOT / "dist", "--workpath", work / "pyi", "--specpath", work,
           "--paths", stage,
           "--add-data", f"{client};web_client", "--add-data", f"{stage / 'rxconfig.py'};.",
           "--add-data", f"{stage / 'VERSION'};.",
           "--add-data", f"{stage / 'lca_web'};lca_web_src",
           "--hidden-import", "lca_web.lca_web",
           "--collect-submodules", "agent", "--collect-submodules", "lca_web",
           "--collect-all", "granian", "--collect-all", "webview", "--collect-all", "ddgs"]
    if (stage / "assets").is_dir():
        cmd += ["--add-data", f"{stage / 'assets'};assets"]
    reflex_dists = sorted({d.metadata["Name"] for d in md.distributions()
                           if (d.metadata["Name"] or "").lower().startswith("reflex")})
    for dist in reflex_dists:
        pkg = dist.replace("-", "_").lower()
        cmd += ["--collect-all", pkg, "--copy-metadata", dist]
    for dist in ("granian", "starlette", "pywebview", "requests"):
        cmd += ["--copy-metadata", dist]
    if "--console" not in args:
        cmd.append("--noconsole")
    cmd.append(stage / "main.py")
    run(cmd, cwd=ROOT)
    exe = ROOT / "dist" / f"{name}.exe"
    print(f"\nГОТОВО: {exe}  ({exe.stat().st_size / 1_048_576:.0f} МБ)" if exe.exists() else "\nexe не створено")


if __name__ == "__main__":
    main()
