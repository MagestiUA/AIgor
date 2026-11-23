"""Збережені підключення до LLM («профілі серверів»): швидке перемикання між серверами/моделями в налаштуваннях.

Лежать у settings.json (ключ `llm_servers`) разом з API-ключем — так само відкрито, як і поточний `llm_api_key`.
У State (він їде у фронтенд) потрапляють лише НАЗВИ; ключі звідси не виходять.
"""
from __future__ import annotations

from . import settings as S

FIELDS = ("backend", "host", "port", "model", "context", "api_key")


def make_name(backend: str, host: str, port: str, model: str) -> str:
    if backend == "ollama":
        return f"Локально · {model or 'модель за замовчуванням'}"
    addr = f"{host}:{port}" if port else host
    return f"{addr or 'сервер'} · {model or 'авто'}"


def _all() -> list[dict]:
    v = S.load().get("llm_servers")
    return [e for e in v if isinstance(e, dict) and e.get("name")] if isinstance(v, list) else []


def names() -> list[str]:
    return [e["name"] for e in _all()]


def get(name: str) -> dict | None:
    return next((dict(e) for e in _all() if e["name"] == name), None)


def save(entry: dict) -> str:
    """Додати або оновити (за назвою) профіль. -> назва."""
    clean = {k: str(entry.get(k) or "").strip() for k in FIELDS}
    clean["name"] = (entry.get("name") or make_name(clean["backend"], clean["host"], clean["port"], clean["model"])).strip()
    items = [e for e in _all() if e["name"] != clean["name"]]
    items.append(clean)
    d = S.load()
    d["llm_servers"] = items
    S.save(d)
    return clean["name"]


def delete(name: str) -> bool:
    items = _all()
    rest = [e for e in items if e["name"] != name]
    if len(rest) == len(items):
        return False
    d = S.load()
    d["llm_servers"] = rest
    S.save(d)
    return True
