"""Розбивка контексту для бічної панелі «Контекст» (як у Claude Code): що саме займає
вікно моделі, скільки токенів, і — біля кожного пункту, що може стискатись — дані про
стиснення (поріг, скільки разів стискали, останнє «було → стало»).

Токени — ОЦІНКА за кількістю символів (≈2.8 симв./токен для кирилиці+коду; див.
limits.py), не точний підрахунок токенайзера. Чиста функція без залежностей від UI —
тестується офлайн (tests/test_context_view.py).
"""
from __future__ import annotations

import json
import math

CHARS_PER_TOKEN = 2.8

# Кольори сегментів (узгоджені зі списком)
COLORS = {"system": "#e8803a", "tools": "#2fb392", "summary": "#a78bfa", "topics": "#38bdf8",
          "messages": "#4f8cff", "attachments": "#e5c04b", "project": "#f472b6", "free": "#4b4b4b"}


def est_tokens(text: str) -> int:
    return math.ceil(round(len(text or "") / CHARS_PER_TOKEN, 6))     # round: уникнути 1000.0000000000001


def tools_tokens(schema: list) -> int:
    return est_tokens(json.dumps(schema, ensure_ascii=False))


def _fmt_chars(n: int) -> str:
    return f"{n:_}".replace("_", " ")


def _fmt_tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def compress_note(compressions: list[dict], what: str, limit_chars: int | None = None,
                  prefix: str = "") -> str:
    """Рядок про стиснення для пункту `what` ('підсумок розмови' | 'нотатка теми' | 'дайджест файлу')."""
    mine = [c for c in compressions if c.get("what") == what]
    parts = []
    if limit_chars:
        parts.append(f"стискається, коли перевищить {_fmt_chars(limit_chars)} симв.")
    if mine:
        last = mine[-1]
        who = f" «{last['name']}»" if last.get("name") else ""
        parts.append(f"стиснень: {len(mine)}; останнє{who}: {_fmt_chars(last['before'])} → {_fmt_chars(last['after'])} симв.")
    elif limit_chars:
        parts.append("ще не стискалось")
    return (prefix + " · ".join(parts)) if parts else ""


def build(*, window: int, system: str, tools_tok: int, project: str = "", topics_note: str = "",
          summary: str = "", summary_budget: int = 0, topic_budget: int = 0, digest_budget: int = 0,
          messages: list[dict] | None = None, history_window: int = 20,
          attachments: list[dict] | None = None, inline_budget: int = 0,
          compressions: list[dict] | None = None) -> dict:
    """-> {window, used, used_pct, rows[{key,label,tokens,pct,color,note,compress}], free}.

    messages — усі повідомлення чату [{role, content, kind}]; у контекст потрапляють
    лише останні history_window (kind text/answer), решту замінює підсумок.
    attachments — [{name, size, inline}]: inline=True вшито в повідомлення, інакше
    читається тулом на вимогу (у вікно не входить, доки модель не прочитала)."""
    compressions = compressions or []
    rows: list[dict] = []

    def add(key, label, tokens, note="", compress=""):
        rows.append({"key": key, "label": label, "tokens": int(tokens), "color": COLORS[key],
                     "note": note, "compress": compress})

    add("system", "Системний промпт", est_tokens(system))
    if project:
        add("project", "Проєкт (інструкції / AGENT.md)", est_tokens(project),
            note=f"{_fmt_chars(len(project))} симв.")
    add("tools", "Інструменти", tools_tok)
    add("summary", "Підсумок розмови", est_tokens(summary),
        note=f"{_fmt_chars(len(summary))} симв." if summary else "порожній",
        compress=compress_note(compressions, "підсумок розмови", summary_budget))
    if topics_note:
        add("topics", "Теми (нотатки минулих розмов)", est_tokens(topics_note),
            note="лише перелік назв; вміст читається тулом на вимогу",
            compress=compress_note(compressions, "нотатка теми", topic_budget))

    ctx_msgs = [m for m in (messages or []) if m.get("role") in ("user", "assistant")
                and m.get("kind", "text") in ("text", "answer") and m.get("content")]
    inwin = ctx_msgs[-history_window:] if history_window else ctx_msgs
    dropped = len(ctx_msgs) - len(inwin)
    add("messages", "Повідомлення", sum(est_tokens(m["content"]) for m in inwin),
        note=f"у вікні {len(inwin)} з {len(ctx_msgs)}" + (f"; {dropped} старіших замінює підсумок" if dropped else ""))

    atts = attachments or []
    inl = [a for a in atts if a.get("inline")]
    ondem = [a for a in atts if not a.get("inline")]
    if atts:
        size_tok = sum(math.ceil(round(a.get("size", 0) / CHARS_PER_TOKEN, 6)) for a in inl)
        note = f"вшито: {len(inl)}" + (f"; на вимогу (тулом): {len(ondem)}" if ondem else "")
        add("attachments", "Вкладення", size_tok, note=note,
            compress=compress_note(compressions, "дайджест файлу", digest_budget,
                                   prefix="великі файли — частинами, дайджест: ") if ondem or
            any(c.get("what") == "дайджест файлу" for c in compressions) else "")

    used = sum(r["tokens"] for r in rows)
    free = max(0, window - used)
    for r in rows:
        r["pct"] = round(100 * r["tokens"] / window, 1) if window else 0.0
        r["tokens_label"] = _fmt_tok(r["tokens"])
    return {"window": window, "used": used, "used_pct": round(100 * used / window) if window else 0,
            "free": free, "free_pct": round(100 * free / window, 1) if window else 0.0,
            "free_label": _fmt_tok(free), "used_label": _fmt_tok(used), "window_label": _fmt_tok(window),
            "rows": rows}
