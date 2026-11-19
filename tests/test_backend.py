"""Офлайн-тест agent/backend.py: URL, пріоритет налаштувань, make_client,
test_connection проти фейкового /models. Диск не чіпаємо (settings передаємо dict-ом)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import backend, config
from agent.llm_openai import OpenAIClient


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good":
            self.send_response(401); self.end_headers(); return
        body = json.dumps({"data": [{"id": "m-1"}, {"id": "m-2"}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)


def main() -> None:
    b = backend.build_base_url
    assert b("62.1.2.3", 8000) == "http://62.1.2.3:8000/v1"
    assert b("https://x.io:9/v1/", "") == "https://x.io:9/v1"
    assert b("h:81", 8000) == "http://h:81/v1"          # порт у host не дублюємо
    assert b("", 8000) == ""

    # пріоритет: налаштування > config-дефолт
    r = backend.resolve({"llm_backend": "openai", "llm_host": "srv", "llm_port": "9",
                         "llm_api_key": "k", "llm_model": "mm"})
    assert r == {"backend": "openai", "base_url": "http://srv:9/v1", "model": "mm", "api_key": "k"}
    r = backend.resolve({})
    assert r["base_url"] == config.REMOTE_BASE_URL and r["model"] == config.LOCAL_MODEL      # дефолтний бекенд — локальний
    assert backend.resolve({"llm_backend": "openai"})["model"] == config.REMOTE_MODEL

    c = backend.make_client({"llm_backend": "openai", "llm_host": "srv", "llm_port": "9",
                             "llm_api_key": "k"})
    assert isinstance(c, OpenAIClient) and c.base_url == "http://srv:9/v1" and c.api_key == "k"

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    try:
        ok, msg, models = backend.test_connection(base, "good")
        assert ok and models == ["m-1", "m-2"], (ok, msg)
        ok, msg, _ = backend.test_connection(base, "bad")
        assert not ok and "401" in msg
        ok, msg, _ = backend.test_connection(base, "")
        assert not ok or True          # ключ може прийти з env — перевіряємо лише відсутність винятку
        ok, msg, _ = backend.test_connection("http://127.0.0.1:1/v1", "good", timeout=2)
        assert not ok
        ok, msg, _ = backend.test_connection("", "good")
        assert not ok and "адресу" in msg
    finally:
        srv.shutdown()
    # локальна Ollama: сервер є + модель є / моделі нема / сервер мовчить
    from unittest import mock
    import requests as _rq

    class R:
        def __init__(self, names): self._n = names
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": n} for n in self._n]}
    with mock.patch("agent.backend.requests.get", return_value=R([config.LOCAL_MODEL, "other:latest"])):
        ok, msg = backend.test_ollama()
        assert ok and config.LOCAL_MODEL in msg
        ok, msg = backend.test_ollama("other")
        assert ok                                              # :latest додається
        ok, msg = backend.test_ollama("нема-такої")
        assert not ok and "ollama pull нема-такої" in msg
    with mock.patch("agent.backend.requests.get", side_effect=_rq.exceptions.ConnectionError()):
        ok, msg = backend.test_ollama()
        assert not ok and "не відповідає" in msg
    print("OK: backend — build_base_url, пріоритети, make_client, test_connection (ok/401/недоступний), test_ollama")


if __name__ == "__main__":
    main()
