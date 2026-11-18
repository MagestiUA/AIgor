"""Навантаження GPU на LLM-сервері (лише читання) — для панелі «Сервер» у правій колонці.

Джерело: агент моніторингу на самому сервері (знімає nvidia-smi раз на 5 с, історія до години):
    GET http://<хост моделі>:<порт агента>/metrics.json?seconds=N
    Authorization: Bearer <той самий ключ, що й для моделі>
Хост беремо з налаштованої адреси LLM-сервера (нічого не вшито в код); порт агента — окреме налаштування (8001).

Як читати (з опису агента): VRAM майже завжди ~90% — vLLM резервує пам'ять наперед, це НЕ навантаження.
Реальне навантаження — now.gpu (%) і now.gpu_power (Вт). Помилки: 401 — ключ; {"ready": false} — агент
ще збирає дані; таймаут/відмова — сервер у гібернації, завантажується або IP не в фаєрволі (порти 8000-8001).
Ключ і URL у повідомлення про помилки не потрапляють.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import requests

DEFAULT_PORT = 8001
BARS = 30                              # скільки стовпчиків у міні-графіку


def metrics_url(base_url: str, port: int | str = DEFAULT_PORT, seconds: int = 600) -> str:
    host = urlparse(base_url).hostname or ""
    if not host:
        return ""
    return f"http://{host}:{int(port or DEFAULT_PORT)}/metrics.json?seconds={int(seconds)}"


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def downsample(series, n: int = BARS) -> list[int]:
    """Ряд % (з пропусками None) → n цілих 0..100: максимум у кожному відрізку (піки важливіші за середнє)."""
    vals = [_num(v) for v in (series or [])]
    if not vals:
        return []
    out: list[int] = []
    size = max(1, -(-len(vals) // n))                    # ceil
    for i in range(0, len(vals), size):
        chunk = [v for v in vals[i:i + size] if v is not None]
        out.append(int(max(0, min(100, round(max(chunk))))) if chunk else 0)
    return out[-n:]


def fetch(base_url: str, api_key: str, port: int | str = DEFAULT_PORT, seconds: int = 600,
          timeout: float = 4.0) -> dict:
    """Один запит до агента. Завжди повертає dict зі `state`:
    ok | wait (агент збирає дані) | auth (401) | down (нема зв'язку) | error | na (не налаштовано)."""
    out = {"state": "na", "msg": "", "ms": 0, "gpu_name": "", "util": None, "vram_pct": None,
           "vram_used_mb": None, "vram_total_mb": None, "temp": None, "power": None, "power_limit": None,
           "running": None, "kv_pct": None, "hist": []}
    url = metrics_url(base_url, port, seconds)
    if not url:
        out["msg"] = "адреса сервера не налаштована"
        return out
    t0 = time.monotonic()
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {}, timeout=timeout)
    except UnicodeError:                                  # ключ із не-ASCII символами (напр. помилка вставки)
        out.update(state="auth", msg="ключ містить недопустимі символи")
        return out
    except requests.RequestException:
        out.update(state="down", msg="сервер не відповідає (гібернація, завантаження або фаєрвол)")
        return out
    out["ms"] = int((time.monotonic() - t0) * 1000)
    if r.status_code == 401:
        out.update(state="auth", msg="ключ не підходить (401)")
        return out
    if r.status_code >= 400:
        out.update(state="error", msg=f"агент відповів помилкою {r.status_code}")
        return out
    try:
        d = r.json()
    except ValueError:
        out.update(state="error", msg="агент повернув не JSON")
        return out
    if not isinstance(d, dict) or d.get("ready") is False:
        out.update(state="wait", msg="агент ще збирає дані")
        return out
    now, static, vllm = d.get("now") or {}, d.get("static") or {}, d.get("vllm") or {}
    hist = d.get("history") or {}
    out.update(
        state="ok", msg="",
        gpu_name=str(static.get("gpu_name") or ""),
        util=_num(now.get("gpu")), vram_pct=_num(now.get("vram_pct")),
        vram_used_mb=_num(now.get("vram_used_mb")), vram_total_mb=_num(static.get("vram_total_mb")),
        temp=_num(now.get("gpu_temp")), power=_num(now.get("gpu_power")), power_limit=_num(now.get("gpu_power_limit")),
        running=_num(vllm.get("running")), kv_pct=_num(vllm.get("kv_pct")),
        hist=downsample(hist.get("gpu")),
    )
    return out


def _f(x, nd=0):
    return "—" if x is None else (f"{x:.{nd}f}" if nd else f"{round(x):d}")


def view(d: dict) -> tuple[dict[str, str], list[int]]:
    """Готові рядки для панелі: {state, status, util, temp_power, vram, model} + ряд для графіка."""
    st = d.get("state", "na")
    if st == "ok":
        status = f"Сервер відповідає · {d['ms']} мс"
    else:
        status = {"wait": "Агент збирає дані", "auth": "Ключ не підходить", "down": "Сервер не відповідає",
                  "error": "Помилка агента", "na": "Не налаштовано"}.get(st, st)
    v = {"state": st, "status": status, "hint": d.get("msg", ""), "util": "", "temp_power": "", "vram": "",
         "model": "", "name": d.get("gpu_name", "")}
    if st == "ok":
        v["util"] = _f(d["util"]) + "%"
        parts = []
        if d["temp"] is not None:
            parts.append(f"{_f(d['temp'])} °C")
        if d["power"] is not None:
            parts.append(f"{_f(d['power'])}/{_f(d['power_limit'])} Вт" if d["power_limit"] else f"{_f(d['power'])} Вт")
        v["temp_power"] = " · ".join(parts)
        if d["vram_used_mb"] is not None:
            tot = f" / {d['vram_total_mb'] / 1024:.1f}" if d["vram_total_mb"] else ""
            pct = f" ({_f(d['vram_pct'])}%)" if d["vram_pct"] is not None else ""
            v["vram"] = f"VRAM {d['vram_used_mb'] / 1024:.1f}{tot} ГіБ{pct}"
        if d["running"] is not None:
            v["model"] = f"запитів у роботі: {_f(d['running'])}" + (f" · KV-кеш {_f(d['kv_pct'])}%" if d["kv_pct"] is not None else "")
    return v, list(d.get("hist") or [])
