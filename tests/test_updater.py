"""Самооновлення: версії, перевірка релізу, завантаження з контрольною сумою, справжня підміна файлу скриптом.
Сервер GitHub підмінено локальним HTTP-сервером; усі шляхи — у тимчасовій теці (реальні дані не чіпаємо)."""
import hashlib
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent import updater

NEW_BODY = (b"@echo off\r\necho started pyi=[%_PYI_APPLICATION_HOME_DIR%] mei=[%_MEIPASS2%] reset=[%PYINSTALLER_RESET_ENVIRONMENT%]"
            b"> \"%~dp0marker.txt\"\r\n" + b"REM padding\r\n" * 5000)
GOOD_SHA = hashlib.sha256(NEW_BODY).hexdigest()
STATE = {"sha": GOOD_SHA, "release": 200}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/release":
            if STATE["release"] == 404:
                return self._send(404, b"{}", "application/json")
            d = {"tag_name": "b_0.0.19", "body": "Що нового: тест", "assets": [
                {"name": "Igor.exe", "browser_download_url": base + "/exe"},
                {"name": "Igor.exe.sha256", "browser_download_url": base + "/sha"}]}
            return self._send(200, json.dumps(d).encode(), "application/json")
        if self.path == "/exe":
            return self._send(200, NEW_BODY)
        if self.path == "/sha":
            return self._send(200, (STATE["sha"] + "  Igor.exe\n").encode())
        self._send(404, b"")


def main() -> None:
    # ── версії ────────────────────────────────────────────────────────────────────
    assert updater.parse_version("b_0.0.19") == (0, 0, 19) and updater.parse_version("dev") is None
    assert updater.is_newer("b_0.0.19", "b_0.0.18") and not updater.is_newer("b_0.0.18", "b_0.0.18")
    assert updater.is_newer("b_0.0.100", "b_0.0.99") and not updater.is_newer("b_0.0.17", "b_0.0.18")
    assert not updater.is_newer("b_0.0.19", "a1b2c3d")                    # dev-збірка з git-хешем не оновлюється

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{srv.server_port}/release"

    tmp = Path(tempfile.mkdtemp())
    updater.STATE_DIR, updater.PENDING, updater.FLAG = tmp / "st", tmp / "st" / "pending.json", tmp / "st" / "go.json"

    # ── перевірка релізу ──────────────────────────────────────────────────────────
    info = updater.check("b_0.0.18", api_url=api)
    assert info["ok"] and info["newer"] and info["latest"] == "b_0.0.19" and info["exe_url"] and "тест" in info["notes"]
    assert not updater.check("b_0.0.19", api_url=api)["newer"]
    STATE["release"] = 404
    r = updater.check("b_0.0.18", api_url=api)
    assert r["ok"] and not r["newer"] and r["latest"] == "" and not r["error"]    # релізів ще нема — не помилка
    STATE["release"] = 200
    r = updater.check("b_0.0.18", api_url="http://127.0.0.1:1/x", timeout=1)
    assert not r["ok"] and "GitHub" in r["error"]                          # мережевий збій — без винятку

    # ── завантаження: збіг суми ───────────────────────────────────────────────────
    d = tmp / "app"
    d.mkdir()
    exe = d / "fake.cmd"
    exe.write_bytes(b"@echo off\r\nREM old\r\n")
    seen = []
    new = updater.download(info, exe_path=exe, progress=seen.append)
    assert new.read_bytes() == NEW_BODY and new.name == "fake.cmd.new" and seen and seen[-1] == 1.0
    assert json.loads(updater.PENDING.read_text(encoding="utf-8"))["sha256"] == GOOD_SHA

    # ── завантаження: НЕзбіг суми → відмова, файлу нема ───────────────────────────
    new.unlink()
    STATE["sha"] = "0" * 64
    try:
        updater.download(info, exe_path=exe)
        raise AssertionError("мав відмовити")
    except RuntimeError as e:
        assert "контрольна сума" in str(e)
    assert not (d / "fake.cmd.new").exists() and not (d / "fake.cmd.part").exists()
    assert exe.read_bytes() == b"@echo off\r\nREM old\r\n"                  # стара версія не зачеплена
    STATE["sha"] = GOOD_SHA

    # ── без прапорця нічого не робиться; з прапорцем — справжня підміна ───────────
    new = updater.download(info, exe_path=exe)
    assert not updater.launch_pending_update()
    assert exe.read_bytes().startswith(b"@echo off\r\nREM old")
    updater.request_restart()
    assert updater.restart_requested()
    # середовище «старої» onefile-exe: нова версія НЕ має його успадкувати (інакше візьме ресурси з _MEI батька,
    # яку той видаляє на виході — живий кейс b_0.0.19→b_0.0.20)
    os.environ["_PYI_APPLICATION_HOME_DIR"] = r"C:\fake\_MEI123"
    os.environ["_MEIPASS2"] = r"C:\fake\_MEI123"
    fe = updater.fresh_env()
    assert "_PYI_APPLICATION_HOME_DIR" not in fe and "_MEIPASS2" not in fe and fe["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert fe["PATH"] == os.environ["PATH"]                                    # решта середовища збережена
    assert updater.launch_pending_update()
    marker = d / "marker.txt"
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.2)
    assert marker.exists(), "нова версія не запустилась після підміни"
    time.sleep(0.5)
    seen_env = marker.read_text(errors="ignore")
    assert "pyi=[]" in seen_env.replace("%_PYI_APPLICATION_HOME_DIR%", "") or "_MEI123" not in seen_env, seen_env
    assert "_MEI123" not in seen_env and "reset=[1]" in seen_env, seen_env
    os.environ.pop("_PYI_APPLICATION_HOME_DIR", None)
    os.environ.pop("_MEIPASS2", None)
    assert exe.read_bytes() == NEW_BODY, "exe не підмінено"
    assert (d / "fake.cmd.old").read_bytes() == b"@echo off\r\nREM old\r\n", "стара версія для відкату не збережена"
    assert not (d / "fake.cmd.new").exists() and not updater.FLAG.exists()

    # ── файл оновлення змінили після перевірки → підміна відмовляє ────────────────
    exe.write_bytes(b"@echo off\r\nREM cur\r\n")
    marker.unlink()
    new = updater.download(info, exe_path=exe)
    new.write_bytes(b"EVIL")
    updater.request_restart()
    assert not updater.launch_pending_update()
    assert exe.read_bytes() == b"@echo off\r\nREM cur\r\n"
    srv.shutdown()
    print("OK: updater — версії, перевірка релізу, сума, відмова при підміні файлу, справжня підміна exe зі збереженням .old")


if __name__ == "__main__":
    main()
