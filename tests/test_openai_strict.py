"""OpenAI-клієнт: strict-режим для серверів без vLLM-полів (DeepSeek), автоперехід після 400, текст помилки сервера."""
from unittest import mock

import requests

from agent import config
from agent.llm_openai import OpenAIClient


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._b, self.text, self.reason = status, body or {}, text, "x"
        self.ok = status < 400

    def json(self):
        return self._b

    def close(self):
        pass


OK = Resp(200, {"choices": [{"message": {"content": "ок"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}


def main() -> None:
    msgs = [{"role": "user", "content": "привіт"}]
    # 1) звичайний (vLLM) режим: нестандартні поля є; deepseek → strict одразу: їх нема, thinking керується параметром
    v = OpenAIClient("http://1.2.3.4:8000/v1", "m", api_key="k")
    p = v._payload(msgs, None, {**config.EXECUTOR, "options": {"top_k": 20}}, SCHEMA, False)
    assert "chat_template_kwargs" in p and p["top_k"] == 20 and p["response_format"]["type"] == "json_schema"
    d = OpenAIClient("https://api.deepseek.com/v1", "deepseek-flash", api_key="k")
    assert d.strict
    p = d._payload(msgs, None, {**config.PLANNER, "options": {"top_k": 20}}, SCHEMA, True)
    assert "chat_template_kwargs" not in p and "top_k" not in p and p["thinking"] == {"type": "enabled"}
    assert p["response_format"] == {"type": "json_object"} and "JSON schema" in p["messages"][0]["content"]
    # у циклі тулів роздуми вимикаємо (інакше сервер вимагає reasoning_content з попередніх ходів)
    loop = msgs + [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x", "arguments": {}}}]},
                   {"role": "tool", "content": "r"}]
    assert d._payload(loop, [{"x": 1}], config.PLANNER, None, False)["thinking"] == {"type": "disabled"}
    # 2) сервер відхилив нестандартні поля (400) → повтор у strict без них, і далі вже strict
    seen = []
    def fake_post(url, json=None, **kw):
        seen.append(json)
        return Resp(400, text="unknown field chat_template_kwargs") if "chat_template_kwargs" in json else OK
    with mock.patch("agent.llm_openai.requests.post", side_effect=fake_post):
        v.chat(msgs, profile=config.EXECUTOR)
        assert v.strict and len(seen) == 2 and "chat_template_kwargs" in seen[0] and "chat_template_kwargs" not in seen[1]
        v.chat(msgs, profile=config.EXECUTOR)
        assert len(seen) == 3                                            # вдруге одразу чистий запит
    # 3) справжня помилка: текст від сервера доходить до користувача
    with mock.patch("agent.llm_openai.requests.post", return_value=Resp(400, text="Invalid model: nope")):
        try:
            OpenAIClient("https://api.deepseek.com/v1", "nope", api_key="k").chat(msgs, profile=config.EXECUTOR)
            raise AssertionError("мала бути помилка")
        except requests.exceptions.HTTPError as e:
            assert "Invalid model: nope" in str(e)
    print("OK: openai strict — DeepSeek без vLLM-полів, автоперехід після 400, текст помилки сервера")


if __name__ == "__main__":
    main()
