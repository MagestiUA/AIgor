"""Діагностичний архів, id чату в логах і сирий лог сервера (agent/logs.py).
LOG_DIR/DIAG_DIR підміняються на тимчасові теки; справжній ключ у тесті — вигаданий."""
import json
import logging
import tempfile
import zipfile
from pathlib import Path

from agent import logs


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    real = (logs.LOG_DIR, logs.DIAG_DIR)
    logs.LOG_DIR, logs.DIAG_DIR = tmp / "logs", tmp / "diag"
    name, raw = "igor-diagtest", "igor-server-diagtest"
    try:
        log = logs.setup(name)
        # id чату в кожному рядку; версія у стартовому рядку
        logs.session_var.set("abcdef1234567890")
        log.info("подія чату")
        srv = logs.raw_file_logger(raw)
        srv.info("Traceback (most recent call last): сирий рядок сервера")
        for h in list(log.handlers) + list(srv.handlers):
            h.flush()
        text = (logs.LOG_DIR / f"{name}.log").read_text(encoding="utf-8")
        assert "[abcdef12]" in text and "подія чату" in text, text
        assert "ігорь=" in text and "ОС=" in text
        assert "сирий рядок сервера" in (logs.LOG_DIR / f"{raw}.log").read_text(encoding="utf-8")

        # архів: логи + налаштування БЕЗ ключа + info
        sp = tmp / "settings.json"
        sp.write_text(json.dumps({"llm_host": "h", "llm_api_key": "SECRET-KEY-123"}), encoding="utf-8")
        def fake_fetch(url):
            if "duckduckgo" in url:
                raise ConnectionError("blocked by firewall")
            return 200
        out = logs.make_diagnostics(sp, net_fetch=fake_fetch, net_resolve=lambda h: "1.2.3.4")
        assert out.parent == logs.DIAG_DIR and out.suffix == ".zip"
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            assert {"info.json", "settings.json", f"logs/{name}.log", f"logs/{raw}.log"} <= names, names
            blob = b"".join(z.read(n) for n in names).decode("utf-8", "replace")
        assert "SECRET-KEY-123" not in blob and "<прихований>" in blob
        assert json.loads(zipfile.ZipFile(out).read("info.json"))["вміст_у_логах"] is False
        net = json.loads(zipfile.ZipFile(out).read("network.json"))
        assert net["dns_duckduckgo"]["ok"] and not net["https_duckduckgo"]["ok"], net
        assert "blocked by firewall" in net["https_duckduckgo"]["помилка"] and net["llm_health"]["ok"]
        assert "SECRET-KEY-123" not in json.dumps(net)
    finally:
        for n in (name, raw):
            for h in list(logging.getLogger(n).handlers):
                h.close()
            logging.getLogger(n).handlers = []
        logs.LOG_DIR, logs.DIAG_DIR = real
        logs.session_var.set("-")
    print("OK: diag — id чату в логах, версія, лог сервера, архів без ключа")


if __name__ == "__main__":
    main()
