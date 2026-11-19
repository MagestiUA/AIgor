"""Офлайн-тест agent/context_view.py: розбивка контексту й дані про стиснення."""
from agent import context_view as cv


def main() -> None:
    assert cv.est_tokens("") == 0 and cv.est_tokens("a" * 28) == 10
    comp = [{"what": "підсумок розмови", "before": 120_000, "after": 61_000},
            {"what": "підсумок розмови", "before": 110_000, "after": 55_000},
            {"what": "дайджест файлу", "before": 300_000, "after": 90_000, "name": "big.md"}]
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 280, "kind": "text"}
            for i in range(30)] + [{"role": "assistant", "content": "крок", "kind": "step"}]
    d = cv.build(window=262_144, system="s" * 2800, tools_tok=1500, project="p" * 560,
                 topics_note="t" * 280, summary="u" * 28_000, summary_budget=100_000, topic_budget=200_000,
                 digest_budget=200_000, messages=msgs, history_window=20,
                 attachments=[{"name": "a.md", "size": 2800, "inline": True},
                              {"name": "big.md", "size": 900_000, "inline": False}],
                 inline_budget=200_000, compressions=comp)
    rows = {r["key"]: r for r in d["rows"]}
    assert set(rows) == {"system", "project", "tools", "summary", "topics", "messages", "attachments"}
    assert rows["system"]["tokens"] == 1000 and rows["tools"]["tokens"] == 1500
    # у вікно потрапляє 20 з 30 текстових повідомлень, kind=step не рахується
    assert rows["messages"]["tokens"] == 20 * 100 and "у вікні 20 з 30" in rows["messages"]["note"]
    assert "10 старіших" in rows["messages"]["note"]
    # стиснення: поріг, лічильник і останнє «було → стало» біля кожного стискуваного пункту
    s = rows["summary"]["compress"]
    assert "100 000" in s and "стиснень: 2" in s and "110 000 → 55 000" in s, s
    assert "big.md" in rows["attachments"]["compress"] and "300 000 → 90 000" in rows["attachments"]["compress"]
    assert "ще не стискалось" in rows["topics"]["compress"]
    # вкладення: вшите рахується, «на вимогу» — ні
    assert rows["attachments"]["tokens"] == 1000 and "на вимогу" in rows["attachments"]["note"]
    # підсумки
    assert d["used"] == sum(r["tokens"] for r in d["rows"]) and d["free"] == 262_144 - d["used"]
    assert 0 <= d["used_pct"] <= 100 and abs(sum(r["pct"] for r in d["rows"]) + d["free_pct"] - 100) < 0.6
    # порожній чат без проєкту/тем/вкладень: лише базові рядки, без падінь
    e = cv.build(window=131_072, system="x", tools_tok=10, summary_budget=32_000)
    assert [r["key"] for r in e["rows"]] == ["system", "tools", "summary", "messages"]
    assert e["rows"][2]["note"] == "порожній"
    # нотатки сесії (код-режим): окремий рядок лише коли індекс не порожній; стиснення історії кроку — біля «Повідомлень»
    nb = "Нотатки цієї сесії (…):" + chr(10) + "- a: суть" + chr(10) + "- b: суть"
    d2 = cv.build(window=65_536, system="s" * 280, tools_tok=500, notes_note=nb, messages=msgs,
                  compressions=[{"what": "історія кроку", "before": 40_000, "after": 6_000}])
    r2 = {r["key"]: r for r in d2["rows"]}
    assert r2["notes"]["tokens"] > 0 and "нотаток у переліку: 2" in r2["notes"]["note"] and r2["notes"]["label"] == "Нотатки сесії"
    assert "40 000 → 6 000" in r2["messages"]["compress"] and "notes" not in {r["key"] for r in d["rows"]}
    print("OK: context_view — оцінка токенів, вікно історії, вкладення, дані про стиснення біля пунктів, нотатки сесії")


if __name__ == "__main__":
    main()
