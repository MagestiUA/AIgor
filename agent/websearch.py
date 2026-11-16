"""Веб-пошук — тонка абстракція над провайдером. Старт: DuckDuckGo (ddgs, без ключа,
безкоштовно). Провайдера можна підмінити (Tavily/Brave) без змін у тулі/циклі.

search() повертає список {title, url, snippet} або None, якщо провайдер недоступний
(не встановлено бібліотеку / помилка мережі). PROVIDER підмінюється в тестах.
"""
from __future__ import annotations

DEFAULT_MAX = 5
LAST_ERROR = ""      # причина останньої невдачі (покажемо моделі/в стрічці/лозі замість мовчазного None)


def _ddg_search(query: str, max_results: int):
    """Провайдер DuckDuckGo через бібліотеку ddgs (стара назва — duckduckgo_search)."""
    global LAST_ERROR
    try:
        from ddgs import DDGS
    except Exception as e1:
        try:
            from duckduckgo_search import DDGS
        except Exception:
            LAST_ERROR = f"не вдалось імпортувати ddgs ({type(e1).__name__}: {str(e1)[:160]})"
            return None                       # бібліотека не встановлена/не запакована
    rows = DDGS().text(query, max_results=max_results) or []
    out = []
    for r in rows:
        out.append({
            "title": (r.get("title") or "").strip(),
            "url": (r.get("href") or r.get("url") or "").strip(),
            "snippet": (r.get("body") or r.get("snippet") or "").strip(),
        })
    return out


PROVIDER = _ddg_search                        # активний провайдер (підмінний)


def search(query: str, max_results: int = DEFAULT_MAX):
    """Пошук. Повертає [{title,url,snippet}] або None, якщо провайдер недоступний."""
    q = (query or "").strip()
    if not q:
        return []
    global LAST_ERROR
    try:
        return PROVIDER(q, max_results)
    except Exception as e:
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            from . import logs
            logs.get("tools").warning("веб-пошук не вдався: %s", LAST_ERROR)
        except Exception:
            pass
        return None


def format_results(results) -> str:
    """Компактний текст сніпетів для подачі моделі."""
    if results is None:
        return ("веб-пошук недоступний: " + (LAST_ERROR or "провайдер не налаштований") +
                " (встановіть `ddgs` або перевірте мережу)")
    if not results:
        return "нічого не знайдено"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n".join(lines)
