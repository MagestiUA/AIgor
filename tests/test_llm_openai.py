"""Офлайн-тест llm_openai.OpenAIClient проти фейкового OpenAI-сервера (локальний
http.server у потоці, жодної мережі/справжнього ключа).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_llm_openai
Нічого на диску не чіпає; env LLM_API_KEY підміняється й відновлюється у finally."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import config
from agent.llm_openai import OpenAIClient, _split_think, _to_openai_messages

SEEN: list[dict] = []          # запити, що дійшли до фейк-сервера
FAIL_FIRST = {"n": 0}          # скільки перших запитів відповісти 503 (імітація гібернації)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"auth": self.headers.get("Authorization"), "body": body})
        if FAIL_FIRST["n"] > 0:
            FAIL_FIRST["n"] -= 1
            self.send_response(503); self.end_headers(); return
        user = body["messages"][-1]["content"]
        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"reasoning_content": "думаю"}}]},
                {"choices": [{"delta": {"content": "При"}}]},
                {"choices": [{"delta": {"content": "віт"}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "read_file", "arguments": '{"pa'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}}]},
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
            ]
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for c in chunks:
                self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            return
        msg = {"role": "assistant", "content": f"echo:{user}"}
        if user == "tool":
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]}
        if user == "think":
            msg = {"role": "assistant", "content": "<think>роздум</think>відповідь"}
        out = json.dumps({"choices": [{"message": msg}],
                          "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(out)


def main() -> None:
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    real_key = os.environ.get("LLM_API_KEY")
    try:
        # без ключа — зрозуміла помилка, запит не йде
        os.environ.pop("LLM_API_KEY", None)
        c = OpenAIClient(base_url=base, wake_timeout=5, retry_delay=0.05)
        try:
            c.chat([{"role": "user", "content": "x"}])
            assert False, "мала бути помилка про відсутній ключ"
        except RuntimeError as e:
            assert "LLM_API_KEY" in str(e)
        assert not SEEN

        os.environ["LLM_API_KEY"] = "test-key"

        # звичайний чат: Bearer, модель клієнта (а не профільна ollama-назва),
        # think->enable_thinking, num_ctx/DRY НЕ летять, stats з usage
        m = c.chat([{"role": "user", "content": "привіт"}], profile=config.EXECUTOR)
        assert m["content"] == "echo:привіт"
        req = SEEN[-1]
        assert req["auth"] == "Bearer test-key"
        assert req["body"]["model"] == config.REMOTE_MODEL
        assert req["body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert "num_ctx" not in json.dumps(req["body"]) and "dry_multiplier" not in json.dumps(req["body"])
        assert c.last_stats["prompt"] == 5 and c.last_stats["out"] == 3

        # <think> у content без парсера роздумів -> розділяємо
        m = c.chat([{"role": "user", "content": "think"}], profile=config.PLANNER)
        assert m["content"] == "відповідь" and m["thinking"] == "роздум"
        assert SEEN[-1]["body"]["chat_template_kwargs"] == {"enable_thinking": True}
        assert _split_think("чисто", "") == ("чисто", "")

        # tool_calls: JSON-рядок -> dict (ollama-форма)
        m = c.chat([{"role": "user", "content": "tool"}], tools=[{"type": "function"}])
        assert m["tool_calls"] == [{"function": {"name": "list_dir", "arguments": {"path": "."}}}]

        # fmt: json / schema -> response_format
        c.chat([{"role": "user", "content": "a"}], fmt="json")
        assert SEEN[-1]["body"]["response_format"] == {"type": "json_object"}
        schema = {"type": "object"}
        c.chat([{"role": "user", "content": "a"}], fmt=schema)
        assert SEEN[-1]["body"]["response_format"]["json_schema"]["schema"] == schema

        # переклад історії: dict-аргументи -> рядок, tool отримує id попереднього виклику
        conv = _to_openai_messages([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "f", "arguments": {"x": 1}}},
                {"function": {"name": "g", "arguments": {}}}]},
            {"role": "tool", "content": "r1"}, {"role": "tool", "content": "r2"}])
        calls = conv[1]["tool_calls"]
        assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}
        assert conv[2]["tool_call_id"] == calls[0]["id"] and conv[3]["tool_call_id"] == calls[1]["id"]

        # стрім: роздуми, контент, зібраний з дельт tool_call, done зі stats
        evs = list(c.chat_stream([{"role": "user", "content": "s"}]))
        assert "".join(e["content"] for e in evs) == "Привіт"
        assert "".join(e["thinking"] for e in evs) == "думаю"
        done = evs[-1]
        assert done["done"] and done["tool_calls"] == [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]
        assert done["stats"]["prompt"] == 11 and done["stats"]["out"] == 7

        # гібернація: перші 2 запити 503 -> ретрай -> успіх
        FAIL_FIRST["n"] = 2
        n0 = len(SEEN)
        assert c.chat([{"role": "user", "content": "після сну"}])["content"] == "echo:після сну"
        assert len(SEEN) - n0 == 3

        # сервер так і не прокинувся -> зрозуміла помилка, а не вічне очікування
        FAIL_FIRST["n"] = 10_000
        try:
            OpenAIClient(base_url=base, wake_timeout=0.3, retry_delay=0.1).chat(
                [{"role": "user", "content": "x"}])
            assert False
        except RuntimeError as e:
            assert "не відповідає" in str(e)
    finally:
        srv.shutdown()
        if real_key is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = real_key

    print("OK: llm_openai — Bearer/модель/think, tool_calls у обидва боки, fmt, стрім, "
          "<think>, ретрай при гібернації, помилка без ключа")


if __name__ == "__main__":
    main()
