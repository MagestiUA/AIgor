"""agent/gpumon.py: запит до агента моніторингу GPU на фальшивому локальному сервері (без реального сервера й ключа)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import gpumon

KEY = "test-key-123"
STATE = {"mode": "ok", "seen_auth": None, "seen_path": None}
GOOD = {"static": {"gpu_name": "H100 PCIe", "vram_total_mb": 81559},
        "now": {"gpu": 37.0, "vram_used_mb": 73728, "vram_pct": 90.4, "gpu_temp": 42, "gpu_power": 83, "gpu_power_limit": 350},
        "vllm": {"running": 2, "kv_pct": 12.5},
        "history": {"t": list(range(100)), "gpu": [0, None, 100, 50] * 25}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        STATE["seen_auth"], STATE["seen_path"] = self.headers.get("Authorization"), self.path
        m = STATE["mode"]
        if m == "401" or self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        body = {"ready": False} if m == "wait" else GOOD
        raw = b"<html>not json" if m == "html" else json.dumps(body).encode()
        self.send_response(500 if m == "500" else 200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    # URL: хост із адреси моделі (будь-яка схема/порт), порт агента окремий; без хоста — порожньо
    assert gpumon.metrics_url("http://llm.example.com:8000/v1", 8001, 300) == "http://llm.example.com:8001/metrics.json?seconds=300"
    assert gpumon.metrics_url("", 8001) == "" and gpumon.metrics_url("не-url") == ""
    assert gpumon.metrics_url("http://h:1/v1", "") .startswith("http://h:8001/")          # порожній порт → типовий

    # ряд для графіка: пропуски → 0, піки зберігаються, довжина ≤ BARS, значення 0..100
    s = gpumon.downsample([0, None, 100, 50] * 25)
    assert 1 <= len(s) <= gpumon.BARS and max(s) == 100 and all(0 <= x <= 100 for x in s)
    assert gpumon.downsample(None) == [] and gpumon.downsample([None, None]) == [0, 0]

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:9/v1"                                  # хост важливий, порт моделі ігнорується
    port = srv.server_port

    d = gpumon.fetch(base, KEY, port)
    assert d["state"] == "ok" and d["util"] == 37.0 and d["gpu_name"] == "H100 PCIe" and d["running"] == 2
    assert STATE["seen_auth"] == f"Bearer {KEY}" and STATE["seen_path"].startswith("/metrics.json?seconds=600")
    v, hist = gpumon.view(d)
    assert v["status"].startswith("Сервер відповідає") and v["util"] == "37%" and v["temp_power"] == "42 °C · 83/350 Вт", v
    assert v["vram"] == "VRAM 72.0 / 79.6 ГіБ (90%)" and v["model"] == "запитів у роботі: 2 · KV-кеш 12%" or v["model"].startswith("запитів у роботі: 2"), v
    assert hist and max(hist) == 100

    # помилки: ключ, агент ще збирає, не JSON, 500, нема зв'язку; ключ і URL у текст не потрапляють
    for mode, st in (("wait", "wait"), ("html", "error"), ("500", "error")):
        STATE["mode"] = mode
        r = gpumon.fetch(base, KEY, port)
        assert r["state"] == st, (mode, r)
    STATE["mode"] = "ok"
    r = gpumon.fetch(base, "wrong-key", port)
    assert r["state"] == "auth" and "401" in r["msg"] and "wrong-key" not in r["msg"]
    r = gpumon.fetch(base, "невірний", port)                           # кирилиця в ключі — без винятку
    assert r["state"] == "auth" and "невірний" not in r["msg"]
    srv.shutdown()
    r = gpumon.fetch(base, KEY, port, timeout=1)                      # сервер вимкнено → «не відповідає»
    assert r["state"] == "down" and KEY not in r["msg"] and "127.0.0.1" not in r["msg"]
    vv, hh = gpumon.view(r)
    assert vv["status"] == "Сервер не відповідає" and vv["util"] == "" and hh == []
    assert gpumon.fetch("", KEY)["state"] == "na"
    print("OK: gpumon — URL з хоста моделі, Bearer-ключ, розбір метрик, графік, помилки без витоку ключа/URL")


if __name__ == "__main__":
    main()
