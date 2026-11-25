"""Самооновлення «Ігоря» з GitHub Releases.

Потік: check() -> (користувач натиснув «Оновити») -> download() кладе нову exe поруч зі старою як `<exe>.new`
й звіряє SHA-256 -> request_restart() ставить прапорець -> лаунчер (main.py) закриває вікно й викликає
launch_pending_update(): окремий .cmd-скрипт перейменовує стару exe в `<exe>.old`, ставить нову на її місце
й запускає. Стара версія лишається як `.old` для відкату. Нічого не ставиться без натискання користувача.

Реліз: тег = версія (b_0.0.19); ресурси `Igor.exe` і `Igor.exe.sha256` (див. scripts/publish_release.py).
Мережеві збої/відсутність релізів — не помилка для користувача при тихій перевірці на старті."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import requests

REPO = "MagestiUA/AIgor"            # перейменовано з local-code-agent (GitHub перенаправляє стару адресу)
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_EXE = "Igor.exe"
ASSET_SHA = "Igor.exe.sha256"

STATE_DIR = Path.home() / ".local-code-agent" / "update"
PENDING = STATE_DIR / "pending.json"       # {"version","new","exe","sha256"} — завантажене й перевірене
FLAG = STATE_DIR / "update-go.json"        # UI просить лаунчер закритись і підмінити exe
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "Igor-updater"}


def parse_version(s: str) -> tuple[int, ...] | None:
    """'b_0.0.19' / 'v1.2' -> (0, 0, 19); None, якщо версії нема (напр. 'dev' або git-хеш)."""
    m = re.search(r"(\d+(?:\.\d+)+)", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


_ETAGS: dict[str, tuple[str, dict]] = {}     # api_url -> (ETag, остання відповідь): для часткових перевірок


def check(current: str, api_url: str = API_URL, timeout: float = 6.0) -> dict:
    """Запитати останній реліз. -> {ok, current, latest, newer, exe_url, sha_url, notes, error}.
    Перевірки можуть бути частими (періодичні), тож шлемо If-None-Match: відповідь 304 «не змінилось» не
    рахується в ліміті GitHub (60 запитів/год з однієї IP без ключа) і майже нічого не коштує."""
    out = {"ok": False, "current": current, "latest": "", "newer": False,
           "exe_url": "", "sha_url": "", "notes": "", "error": ""}
    try:
        headers = dict(HEADERS)
        cached = _ETAGS.get(api_url)
        if cached:
            headers["If-None-Match"] = cached[0]
        r = requests.get(api_url, headers=headers, timeout=timeout)
        if r.status_code == 304 and cached:
            d = cached[1]
        else:
            if r.status_code == 404:                 # репозиторій без жодного релізу — не помилка, просто нема що ставити
                _ETAGS.pop(api_url, None)
                out["ok"] = True
                return out
            r.raise_for_status()
            d = r.json()
            if r.headers.get("ETag"):
                _ETAGS[api_url] = (r.headers["ETag"], d)
    except (requests.RequestException, ValueError) as e:
        out["error"] = f"не вдалося зв'язатися з GitHub: {type(e).__name__}"
        return out
    assets = {a.get("name"): a.get("browser_download_url", "") for a in d.get("assets", [])}
    out.update(ok=True, latest=str(d.get("tag_name") or ""), notes=(d.get("body") or "").strip()[:1500],
               exe_url=assets.get(ASSET_EXE, ""), sha_url=assets.get(ASSET_SHA, ""))
    out["newer"] = is_newer(out["latest"], current)
    if out["newer"] and not (out["exe_url"] and out["sha_url"]):
        out.update(newer=False, ok=False, error=f"у релізі {out['latest']} нема {ASSET_EXE}/{ASSET_SHA}")
    return out


def can_self_update() -> tuple[bool, str]:
    """Оновлення на місці можливе лише зі зібраної exe в теці, куди можна писати."""
    if not getattr(sys, "frozen", False):
        return False, "оновлення працює лише в зібраній exe (запуск із вихідників оновлюйте через git)"
    exe = Path(sys.executable)
    try:
        probe = exe.with_name(exe.name + ".probe")
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False, f"немає прав запису в теку {exe.parent} — перенесіть Igor.exe в іншу теку або оновіть вручну"
    return True, ""


def _expected_sha(sha_url: str, timeout: float) -> str:
    r = requests.get(sha_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=timeout)
    r.raise_for_status()
    m = re.search(r"\b([0-9a-fA-F]{64})\b", r.text)
    if not m:
        raise ValueError("файл контрольної суми порожній або має невірний формат")
    return m.group(1).lower()


def download(info: dict, exe_path: Path | None = None, progress: Callable[[float], None] | None = None,
             timeout: float = 30.0) -> Path:
    """Завантажити нову exe поруч зі старою (`<exe>.new`), звірити SHA-256 і записати pending.json.
    Кидає RuntimeError з зрозумілим текстом, якщо щось не так; файл при цьому не лишається."""
    exe = Path(exe_path or sys.executable)
    new = exe.with_name(exe.name + ".new")
    part = exe.with_name(exe.name + ".part")
    try:
        want = _expected_sha(info["sha_url"], timeout)
        h = hashlib.sha256()
        with requests.get(info["exe_url"], headers={"User-Agent": HEADERS["User-Agent"]}, stream=True,
                          timeout=timeout) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(min(1.0, got / total))
        if h.hexdigest() != want:
            raise RuntimeError("контрольна сума не збігається — файл пошкоджено, оновлення скасовано")
        os.replace(part, new)
    except RuntimeError:
        part.unlink(missing_ok=True)
        raise
    except (requests.RequestException, ValueError, OSError) as e:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"не вдалося завантажити оновлення: {type(e).__name__}: {e}") from e
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps({"version": info["latest"], "new": str(new), "exe": str(exe), "sha256": want},
                                  ensure_ascii=False), encoding="utf-8")
    return new


def request_restart() -> None:
    """UI: оновлення завантажено, закрити програму й підмінити exe (виконує лаунчер)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FLAG.write_text(str(time.time()), encoding="utf-8")


def restart_requested() -> bool:
    return FLAG.exists()


def clear_stale() -> None:
    """При старті лаунчера: прапорець із минулого сеансу не має закрити свіжозапущене вікно."""
    FLAG.unlink(missing_ok=True)


_SCRIPT = r"""@echo off
setlocal
set n=0
:wait
del "%IGOR_OLD%" >nul 2>&1
move /y "%IGOR_EXE%" "%IGOR_OLD%" >nul 2>&1
if not errorlevel 1 goto swap
set /a n+=1
if %n% GEQ 60 goto fail
ping -n 2 127.0.0.1 >nul
goto wait
:swap
move /y "%IGOR_NEW%" "%IGOR_EXE%" >nul 2>&1
if errorlevel 1 (
  move /y "%IGOR_OLD%" "%IGOR_EXE%" >nul 2>&1
  goto fail
)
start "" "%IGOR_EXE%"
goto done
:fail
if exist "%IGOR_EXE%" start "" "%IGOR_EXE%"
:done
(goto) 2>nul & del "%~f0"
"""


def fresh_env(base: dict | None = None) -> dict:
    """Середовище для запуску НОВОЇ exe після підміни. PyInstaller (onefile) лишає в середовищі змінні
    (`_PYI_*`, `_MEIPASS2`), за якими процес-нащадок із ТИМ САМИМ шляхом exe вважає себе дочірнім і бере
    ресурси з розпаковки батька — а батько на виході її видаляє (живий кейс b_0.0.19→b_0.0.20: нова exe
    стартувала зі старою VERSION і падала на відсутньому rxconfig.py). Тому чистимо їх і просимо
    завантажувач розпакуватись заново."""
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if k != "_MEIPASS2" and not k.startswith("_PYI_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch_pending_update(log=None) -> bool:
    """Лаунчер після закриття вікна: якщо є прапорець і перевірений файл — запустити скрипт підміни.
    Шляхи йдуть через змінні середовища (скрипт лишається чистим ASCII — кирилиця в шляхах не страшна)."""
    if not FLAG.exists():
        return False
    FLAG.unlink(missing_ok=True)
    try:
        p = json.loads(PENDING.read_text(encoding="utf-8"))
        new, exe = Path(p["new"]), Path(p["exe"])
        h = hashlib.sha256(new.read_bytes()).hexdigest()
        if h != p["sha256"]:
            raise RuntimeError("файл оновлення змінено після перевірки")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        script = STATE_DIR / "apply_update.cmd"
        script.write_text(_SCRIPT, encoding="ascii")
        env = {**fresh_env(), "IGOR_EXE": str(exe), "IGOR_NEW": str(new), "IGOR_OLD": str(exe) + ".old"}
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(["cmd", "/c", str(script)], env=env, creationflags=flags, close_fds=True)
        PENDING.unlink(missing_ok=True)
        if log:
            log.info("оновлення до %s: запущено підміну exe", p.get("version"))
        return True
    except Exception as e:                                  # noqa: BLE001
        if log:
            log.exception("не вдалося запустити оновлення")
        else:
            print(f"оновлення не вдалося: {e}")
        return False
