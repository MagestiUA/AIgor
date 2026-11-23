"""Клієнт OpenAI-сумісного API (vLLM на орендованому H100) з ТИМ САМИМ інтерфейсом,
що й llm.OllamaClient: chat / chat_stream / unload / last_stats / last_error.
Решта коду (agent_loop, convo, topics, lca_web…) працює з клієнтом, не знаючи
бекенда — вибір у lca_web.get_client() за config.BACKEND.

Що перекладаємо між форматами (агент говорить «по-ollama-вськи»):
  * assistant.tool_calls[].function.arguments: dict (ollama) <-> JSON-рядок (OpenAI);
  * role=tool без id (ollama) -> потрібен tool_call_id (OpenAI): привʼязуємо за порядком;
  * think -> chat_template_kwargs.enable_thinking (vLLM); роздуми приходять у
    reasoning_content / reasoning, а якщо парсера роздумів немає — ріжемо <think>…</think>;
  * fmt ("json" чи JSON-schema) -> response_format;
  * options: передаємо лише те, що vLLM розуміє (temperature/top_p/top_k/min_p);
    num_ctx і DRY — суто ollama-речі, тут ігноруються.

Сервер орендований і йде в гібернацію після 60 хв без запитів; перший запит після
цього падає ~до 10 хв, поки він прокидається — тож _post_with_retry довго й терпляче
ретраїть (зʼєднання/таймаут/502/503/504), а не здається за 3 спроби, як для Ollama.
Ключ беремо з env LLM_API_KEY ПРИ КОЖНОМУ виклику (не кешуємо й нікуди не логуємо).
"""
from __future__ import annotations

import json
import os
import time
import uuid

import requests

from . import config
from . import logs

_RETRY_STATUS = {429, 502, 503, 504, 520, 521, 522, 523, 524}
_PASS_OPTIONS = ("top_p", "top_k", "min_p", "seed")


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Ollama-формат історії -> OpenAI-формат (tool_calls зі string-аргументами, id)."""
    out: list[dict] = []
    pending_ids: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls, pending_ids = [], []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                cid = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                pending_ids.append(cid)
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fn.get("name", ""),
                    "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                }})
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            cid = m.get("tool_call_id") or (pending_ids.pop(0) if pending_ids else f"call_{uuid.uuid4().hex[:12]}")
            out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content") or ""})
        else:
            content = m.get("content") or ""
            imgs = m.get("images") if role == "user" else None
            if imgs:        # зображення (data-URL) → мультимодальний content: текст + частини image_url
                parts = [{"type": "text", "text": content or "(зображення)"}]
                parts += [{"type": "image_url", "image_url": {"url": u}} for u in imgs]
                out.append({"role": role, "content": parts})
            else:
                out.append({"role": role, "content": content})
    return out


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _norm_tool_calls(calls) -> list[dict]:
    """OpenAI tool_calls -> ollama-форма ({'function': {'name', 'arguments': dict}})."""
    return [{"function": {"name": c.get("function", {}).get("name", ""),
                          "arguments": _parse_args(c.get("function", {}).get("arguments"))}}
            for c in (calls or [])]


def _split_think(content: str, thinking: str) -> tuple[str, str]:
    """Немає парсера роздумів на сервері -> роздуми лежать у content у <think>…</think>."""
    if thinking or "</think>" not in content:
        return content, thinking
    head, _, tail = content.partition("</think>")
    return tail.lstrip(), head.replace("<think>", "").strip()


class OpenAIClient:
    def __init__(self, base_url: str = config.REMOTE_BASE_URL, model: str = config.REMOTE_MODEL,
                 key_env: str = config.REMOTE_KEY_ENV, wake_timeout: float = 660.0,
                 retry_delay: float = 15.0, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.key_env = key_env
        self.api_key = api_key                # з налаштувань UI; порожній -> env
        self.wake_timeout = wake_timeout      # скільки чекаємо, поки сервер прокинеться
        self.retry_delay = retry_delay
        self.last_stats: dict = {}
        self.last_error: str = ""
        self.strict = "deepseek" in base_url.lower()    # DeepSeek не знає vLLM-полів; для решти — автоперехід після 400

    # ── Допоміжне ───────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        key = self.api_key or os.environ.get(self.key_env, "")
        if not key:
            raise RuntimeError("Не задано API-ключ LLM-сервера. Вкажіть його в Налаштуваннях "
                               f"(або змінну середовища/.env {self.key_env}).")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _payload(self, messages, tools, profile, fmt, stream: bool) -> dict:
        msgs = _to_openai_messages(messages)
        p: dict = {
            "model": self.model,              # профільний "model" — ollama-ім'я, тут ігноруємо
            "messages": msgs,
            "stream": stream,
            "temperature": profile.get("temperature", 0.2),
        }
        if self.strict:
            # «чистий» OpenAI-API (DeepSeek та ін.): без vLLM-специфічних полів; роздуми вмикаються параметром thinking
            # у історії є виклики тулів → DeepSeek вимагав би повертати reasoning_content кожного ходу; тож без роздумів
            in_tool_loop = any(m.get("role") == "tool" or m.get("tool_calls") for m in msgs)
            p["thinking"] = {"type": "enabled" if profile.get("think") and not in_tool_loop else "disabled"}
        else:
            p["chat_template_kwargs"] = {"enable_thinking": bool(profile.get("think"))}
            for k in _PASS_OPTIONS:
                if k in profile.get("options", {}):
                    p[k] = profile["options"][k]
        if stream:
            p["stream_options"] = {"include_usage": True}
        if tools:
            p["tools"] = tools
        if fmt:
            if self.strict:                   # json_schema підтримує не кожен сервер: json_object + схема в підказці
                p["response_format"] = {"type": "json_object"}
                if fmt != "json":
                    hint = "Respond ONLY with a JSON object matching this JSON schema: " + json.dumps(fmt, ensure_ascii=False)
                    msgs.insert(0, {"role": "system", "content": hint})
                elif not any("json" in str(m.get("content", "")).lower() for m in msgs):
                    msgs.insert(0, {"role": "system", "content": "Respond ONLY with a valid JSON object."})
            else:
                p["response_format"] = ({"type": "json_object"} if fmt == "json" else
                                        {"type": "json_schema",
                                         "json_schema": {"name": "out", "schema": fmt}})
        return p

    def _send(self, messages, tools, profile, fmt, stream: bool):
        """POST із автопереходом у strict-режим: сервер відхилив нестандартні поля (400) → повторюємо «чистим» запитом
        і запам'ятовуємо це для наступних викликів. Для адрес deepseek strict вмикається одразу."""
        r = self._post_with_retry(self._payload(messages, tools, profile, fmt, stream), stream=stream)
        if r.status_code == 400 and not self.strict:
            logs.get("llm").warning("HTTP 400 (%s) — повторюю у strict-режимі (без vLLM-полів)", r.text[:200])
            r.close()
            self.strict = True
            r = self._post_with_retry(self._payload(messages, tools, profile, fmt, stream), stream=stream)
        return r

    @staticmethod
    def _check(r) -> None:
        """raise_for_status, але з текстом помилки від сервера (інакше видно лише «400 Bad Request»)."""
        if r.ok:
            return
        body = ""
        try:
            body = r.text[:400]
        except Exception:                                       # noqa: BLE001
            pass
        logs.get("llm").error("HTTP %s: %s", r.status_code, body)
        raise requests.exceptions.HTTPError(f"{r.status_code} від сервера: {body or r.reason}", response=r)

    def _post_with_retry(self, payload: dict, stream: bool = False):
        """POST /chat/completions. Ретраїмо до wake_timeout (сервер може прокидатись
        з гібернації до ~10 хв): помилки зʼєднання/таймаут і 429/5xx-шлюзів."""
        headers = self._headers()
        deadline = time.monotonic() + self.wake_timeout
        while True:
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=payload,
                                  headers=headers, stream=stream,
                                  timeout=(10, config.REQUEST_TIMEOUT))
                if r.status_code in _RETRY_STATUS:
                    self.last_error = f"HTTP {r.status_code}"
                    logs.get("llm").warning("сервер відповів %s — повтор через %.0fс", r.status_code, self.retry_delay)
                    r.close()
                else:
                    self.last_error = ""
                    return r
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                self.last_error = str(e)
                logs.get("llm").warning("немає зʼєднання з %s: %s — повтор", self.base_url, type(e).__name__)
            if time.monotonic() + self.retry_delay > deadline:
                logs.get("llm").error("сервер не відповів за %.0fс (%s)", self.wake_timeout, self.last_error)
                raise RuntimeError(f"LLM-сервер не відповідає ({self.last_error}). Можливо, він у "
                                   "гібернації або ваш IP не доданий у фаєрвол.")
            time.sleep(self.retry_delay)

    def unload(self) -> None:
        """Вивантаження моделі — суто ollama-поняття; тут нічого."""

    # ── Запити ──────────────────────────────────────────────────────────────
    def chat(self, messages: list[dict], tools: list | None = None,
             profile: dict = config.EXECUTOR, fmt: str | dict | None = None) -> dict:
        t0 = time.monotonic()
        r = self._send(messages, tools, profile, fmt, stream=False)
        self._check(r)
        data = r.json()
        msg = data["choices"][0]["message"]
        content, thinking = _split_think(msg.get("content") or "",
                                         msg.get("reasoning_content") or msg.get("reasoning") or "")
        usage = data.get("usage") or {}
        self.last_stats = {"prompt": usage.get("prompt_tokens", 0) or 0,
                           "out": usage.get("completion_tokens", 0) or 0,
                           "eval_ns": int((time.monotonic() - t0) * 1e9), "model": self.model}
        logs.get("llm").info("chat ok model=%s msgs=%d tools=%d think=%s prompt=%d out=%d %.1fс",
                             self.model, len(messages), len(tools or []), bool(profile.get("think")),
                             self.last_stats["prompt"], self.last_stats["out"], time.monotonic() - t0)
        out = {"role": "assistant", "content": content}
        if thinking:
            out["thinking"] = thinking
        if msg.get("tool_calls"):
            out["tool_calls"] = _norm_tool_calls(msg["tool_calls"])
        return out

    def chat_stream(self, messages: list[dict], tools: list | None = None,
                    profile: dict = config.EXECUTOR, fmt: str | dict | None = None,
                    stop_event=None):
        """Генератор подій у форматі llm._stream_event: {content, thinking, done, ...}."""
        t0 = time.monotonic()
        t_first: float | None = None
        acc: dict[int, dict] = {}
        usage: dict = {}
        with self._send(messages, tools, profile, fmt, stream=True) as r:
            self._check(r)
            for line in r.iter_lines():
                if stop_event and stop_event.is_set():
                    break
                if not line or not line.startswith(b"data:"):
                    continue
                body = line[5:].strip()
                if body == b"[DONE]":
                    break
                try:
                    obj = json.loads(body)
                except Exception:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    for tc in d.get("tool_calls") or []:
                        a = acc.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                        fn = tc.get("function") or {}
                        a["name"] += fn.get("name") or ""
                        a["args"] += fn.get("arguments") or ""
                    content = d.get("content") or ""
                    thinking = d.get("reasoning_content") or d.get("reasoning") or ""
                    if content or thinking:
                        t_first = t_first or time.monotonic()
                        yield {"content": content, "thinking": thinking, "done": False}
        elapsed = time.monotonic() - (t_first or t0)
        logs.get("llm").info("stream ok model=%s msgs=%d tools=%d think=%s prompt=%d out=%d %.1fс",
                             self.model, len(messages), len(tools or []), bool(profile.get("think")),
                             usage.get("prompt_tokens", 0) or 0, usage.get("completion_tokens", 0) or 0,
                             time.monotonic() - t0)
        self.last_stats = {"prompt": usage.get("prompt_tokens", 0) or 0,
                           "out": usage.get("completion_tokens", 0) or 0,
                           "eval_ns": int(elapsed * 1e9), "model": self.model}
        calls = [{"function": {"name": a["name"], "arguments": _parse_args(a["args"])}}
                 for _, a in sorted(acc.items())]
        yield {"content": "", "thinking": "", "done": True, "tool_calls": calls or None,
               "stats": {k: self.last_stats[k] for k in ("prompt", "out", "eval_ns")}}
