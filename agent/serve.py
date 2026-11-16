"""Запуск Ігоря БЕЗ Node/Bun: готовий (зібраний `reflex export`) фронтенд віддає сам
бекенд (Granian), на одному порту.

Проблема, яку це вирішує: адреса бекенда «запечена» у зібраний фронтенд (env.json →
`api_url`, за замовчуванням http://localhost:8000), а порт у кожного користувача
вільний інший. Тому фронтенд збирається з сентинелом (`SENTINEL_HOST`), а перед
запуском ми копіюємо статику в робочу теку й підставляємо реальний порт. Попередньо
стиснені `.gz/.br` видаляємо — інакше сервер віддавав би старі байти.

Використовується в збірці (.exe) і в `python main.py --prod`.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

SENTINEL_HOST = "igor-host.invalid:1"
RUNTIME = Path.home() / ".local-code-agent" / "runtime"
_TEXT_EXT = {".js", ".html", ".json", ".css", ".txt", ".xml", ".mjs"}


def bundle_root() -> Path:
    """Корінь ресурсів: тека розпакування PyInstaller або корінь репозиторію."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def static_src(root: Path) -> Path:
    return root / "web_client" if getattr(sys, "frozen", False) else root / ".web" / "build" / "client"


def prepare_runtime(port: int, src: Path, rxconfig: Path, assets: Path | None = None,
                    base: Path | None = None, app_src: Path | None = None) -> Path:
    """Скопіювати статику в <base>/p<port>/.web/build/client з підставленим портом;
    повернути робочу теку (в ній же rxconfig.py та assets/). Старі теки (>2 діб) чистимо."""
    base = base or RUNTIME
    base.mkdir(parents=True, exist_ok=True)
    for d in base.glob("p*"):
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > 2 * 86400:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    rt = base / f"p{port}"
    shutil.rmtree(rt, ignore_errors=True)
    dst = rt / ".web" / "build" / "client"
    shutil.copytree(src, dst)
    real = f"127.0.0.1:{port}"
    for f in dst.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix in (".gz", ".br"):
            f.unlink()
        elif f.suffix in _TEXT_EXT:
            try:
                txt = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if SENTINEL_HOST in txt:
                f.write_text(txt.replace(SENTINEL_HOST, real), encoding="utf-8")
    shutil.copy2(rxconfig, rt / "rxconfig.py")
    if assets and assets.is_dir():
        shutil.copytree(assets, rt / "assets", dirs_exist_ok=True)
    # Reflex перевіряє наявність модуля застосунку ФАЙЛОМ відносно sys.path/cwd (get_module_path);
    # у .exe код лежить в архіві, тож без копії пакета `lca_web` на диску подієва обробка падає
    # з «Module lca_web.lca_web not found» (живий кейс першої збірки).
    if app_src and app_src.is_dir():
        shutil.copytree(app_src, rt / "lca_web", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return rt


def serve(port: int) -> None:
    """Підняти бекенд із вбудованою статикою (блокує до зупинки)."""
    root = bundle_root()
    app_src = root / ("lca_web_src" if getattr(sys, "frozen", False) else "lca_web")
    rt = prepare_runtime(port, static_src(root), root / "rxconfig.py", root / "assets", app_src=app_src)
    os.chdir(rt)
    # Внутрішні прапори Reflex мають префікс "__": без компіляції та з монтуванням
    # готового фронтенду в бекенд.
    os.environ.update({"__REFLEX_SKIP_COMPILE": "true", "__REFLEX_MOUNT_FRONTEND_COMPILED_APP": "true",
                       "REFLEX_ENV_MODE": "prod", "REFLEX_TELEMETRY_ENABLED": "false",
                       "PYTHONIOENCODING": "utf-8"})
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from granian.constants import Interfaces
    from granian.server import Server
    Server(target="lca_web.lca_web:app", factory=True, address="127.0.0.1", port=port,
           interface=Interfaces.ASGI, workers=1).serve()
