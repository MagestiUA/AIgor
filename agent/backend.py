"""Вибір і перевірка LLM-бекенда за налаштуваннями користувача.

Пріоритет: налаштування застосунку (settings.json, UI «Налаштування») > .env/env >
дефолти config.py. Це потрібно для збірки для інших розробників: у них немає нашого
.env — адресу сервера, порт і власний ключ вони задають у вікні налаштувань.

Ключі settings.json: llm_backend ("" | "ollama" | "openai"), llm_host, llm_port,
llm_api_key, llm_model ("" = взяти першу модель із /v1/models).
"""
from __future__ import annotations

import re

import requests

from . import config
from . import settings as S


def build_base_url(host: str, port: str | int = "") -> str:
    """'62.1.2.3' + 8000 -> 'http://62.1.2.3:8000/v1'. Терпимо до схеми, порту в
    самому host, зайвих слешів і готового суфікса /v1."""
    h = (host or "").strip().rstrip("/")
    if not h:
        return ""
    if not re.match(r"^https?://", h, re.I):
        h = "http://" + h
    h = re.sub(r"/v1$", "", h, flags=re.I)
    p = str(port or "").strip()
    if p and not re.search(r":\d+$", h):
        h = f"{h}:{p}"
    return h + "/v1"


def resolve(settings: dict | None = None) -> dict:
    """Ефективна конфігурація: {backend, base_url, model, api_key}."""
    s = settings if settings is not None else S.load()
    backend = (s.get("llm_backend") or config.BACKEND or "ollama").lower()
    base_url = build_base_url(s.get("llm_host", ""), s.get("llm_port", "")) or config.REMOTE_BASE_URL
    return {
        "backend": backend,
        "base_url": base_url,
        "model": s.get("llm_model") or (config.REMOTE_MODEL if backend == "openai" else config.LOCAL_MODEL),
        "api_key": s.get("llm_api_key") or "",       # порожній -> клієнт візьме з env
    }


def make_client(settings: dict | None = None):
    cfg = resolve(settings)
    if cfg["backend"] == "openai":
        from .llm_openai import OpenAIClient
        return OpenAIClient(base_url=cfg["base_url"], model=cfg["model"], api_key=cfg["api_key"])
    from .llm import OllamaClient
    c = OllamaClient(model=(settings if settings is not None else S.load()).get("llm_model") or config.LOCAL_MODEL)
    c.single_model = True                      # локально: одна модель на всі ролі, вікно з limits (як на сервері)
    return c


def test_connection(base_url: str, api_key: str = "", timeout: float = 15.0) -> tuple[bool, str, list[str]]:
    """Перевірка сервера: GET /models. Повертає (ok, повідомлення, список моделей).
    Ключ береться з аргумента, а якщо порожній — з env (LLM_API_KEY / .env)."""
    import os
    key = api_key or os.environ.get(config.REMOTE_KEY_ENV, "")
    if not base_url:
        return False, "Не вказано адресу сервера.", []
    if not key:
        return False, "Не вказано API-ключ.", []
    try:
        r = requests.get(base_url.rstrip("/") + "/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except requests.exceptions.ConnectTimeout:
        return False, "Сервер не відповів вчасно (можливо, у гібернації або IP не в фаєрволі).", []
    except requests.exceptions.ConnectionError:
        return False, "Не вдалося підключитися (перевірте адресу/порт, VPN/фаєрвол).", []
    except requests.exceptions.RequestException as e:
        return False, f"Помилка запиту: {e}", []
    if r.status_code in (401, 403):
        return False, "Сервер відхилив ключ (401/403) — перевірте API-ключ.", []
    if not r.ok:
        return False, f"Сервер відповів HTTP {r.status_code}.", []
    try:
        models = [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return False, "Відповідь сервера — не OpenAI-сумісний /models.", []
    return True, f"З'єднання OK. Моделі: {', '.join(models) or '—'}", models
