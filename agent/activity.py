"""Допоміжне для живої стрічки подій у чаті («Що робить Ігорь»): короткий опис виклику
інструмента, стислий результат і ознака помилки. Чисті функції — тестуються офлайн."""
from __future__ import annotations

import json

_ERR_PREFIXES = ("помилка", "заблоковано", "не знайдено", "файл не знайдено", "timeout",
                 "команду відхилено", "консоль вимкнена", "невідомий інструмент", "тека не",
                 "веб-пошук недоступний")


def summarize_args(name: str, args: dict, n: int = 400) -> str:
    """Що саме виконується: для shell — сама команда, для файлів — шлях тощо."""
    a = args or {}
    if name == "run_shell":
        s = a.get("command", "")
    elif name in ("read_file", "list_dir", "read_attachment"):
        s = a.get("path") or a.get("name") or "."
        if a.get("offset"):
            s += f" (з позиції {a['offset']})"
    elif name == "write_file":
        s = f"{a.get('path', '')}  ({len(a.get('content', '') or '')} симв.)"
    elif name in ("edit_file", "create_from_source"):
        s = f"{a.get('path') or a.get('target', '')} — {str(a.get('instruction', ''))[:120]}"
    elif name == "web_search":
        s = a.get("query", "")
    elif name == "read_topic":
        s = a.get("name", "")
    else:
        try:
            s = json.dumps(a, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(a)
    s = " ".join(str(s).split()) if name != "run_shell" else str(s).strip()
    return s if len(s) <= n else s[:n] + "…"


def clip_result(result: str, n: int = 600) -> str:
    r = (result or "").strip()
    if not r:
        return "(порожній результат)"
    return r if len(r) <= n else f"{r[:n]}…  [усього {len(r)} симв.]"


def result_ok(result: str) -> bool:
    return not (result or "").strip().lower().startswith(_ERR_PREFIXES)
