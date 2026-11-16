"""Інтеграція панелі «Контекст»: обчислювані змінні State на прикладі даних.
Теми й ліміти ізольовані (тимчасова тека тем, limits.FORCE) — реальні дані не чіпаємо."""
import json
import tempfile
from pathlib import Path

from agent import limits, topics
import lca_web.lca_web as W

limits.FORCE = limits.REMOTE


def main() -> None:
    real_root = topics.TOPICS_ROOT
    topics.TOPICS_ROOT = Path(tempfile.mkdtemp()) / ".chat_topics"
    try:
        s = W.State(_reflex_internal_init=True)
        s.mode = "chat"
        s.current_id = ""
        s.messages = [{"role": "user", "content": "Привіт " * 50, "kind": "text", "attachments": [], "thinking": ""},
                      {"role": "assistant", "content": "ok " * 100, "kind": "answer", "attachments": [], "thinking": ""}]
        s.context_summary = "- підсумок\n" * 300
        s.compress_log = [{"what": "підсумок розмови", "before": 120_000, "after": 61_000, "name": "", "ts": 0}]
        rep = json.loads(s.ctx_report)
        assert rep["window"] == 262_144 and 0 <= rep["used_pct"] < 5
        rows = {r["label"]: r for r in s.ctx_rows}
        assert {"Системний промпт", "Інструменти", "Підсумок розмови", "Повідомлення"} <= set(rows)
        assert "120 000 → 61 000" in rows["Підсумок розмови"]["compress"]
        assert "стиснень: 1" in rows["Підсумок розмови"]["compress"]
        assert rows["Повідомлення"]["note"] == "у вікні 2 з 2"
        assert s.ctx_total.startswith("≈") and "/" in s.ctx_total and isinstance(s.ctx_pct, int)
        # секції «Середовище» / «Теки та файли» (чат без проєкту й вкладень: теки порожні)
        labels = {r["label"] for r in s.env_rows}
        assert labels >= {"Бекенд", "Модель", "Режим"} or labels >= {"Сервер", "Модель", "Режим"}, labels
        assert s.folder_rows == []
        assert all(set(r) == {"label", "tokens_label", "pct_label", "color", "note", "compress"} for r in s.ctx_rows)
        assert s.ctx_segments and all(x["width"].endswith("%") for x in s.ctx_segments)
        # код-режим: інший системний промпт і повний набір інструментів
        s.mode = "code"
        rep2 = json.loads(s.ctx_report)
        t1 = next(r["tokens"] for r in rep["rows"] if r["key"] == "tools")
        t2 = next(r["tokens"] for r in rep2["rows"] if r["key"] == "tools")
        assert t2 > t1
        # стискання запис: подія -> журнал стану
        s.current_id = "x"
        print("OK: ctx_state — розбивка, дані про стиснення, режими чат/код")
    finally:
        topics.TOPICS_ROOT = real_root


if __name__ == "__main__":
    main()
