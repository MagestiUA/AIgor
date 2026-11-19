"""Нотатки сесії на диску: індекс у контексті, remember/recall, автозбереження звіту субагента, обмеження, чистка."""
import tempfile
from pathlib import Path

from agent import agent_loop as AL
from agent import limits, sessionmem as M, subagent
from agent.toolkit import ToolContext, default_registry


def main() -> None:
    real = M.ROOT
    M.ROOT = Path(tempfile.mkdtemp()) / "session-memory"                 # ніколи не чіпаємо реальний профіль
    limits.FORCE = limits.REMOTE
    try:
        # 1) запис/читання/заміна; довгі значення не переказуємо
        assert M.save_note("S1", "Де вкладення", "Файли лежать у attachments/<sid>. Мініатюри в .thumbs") == "де-вкладення"
        assert M.save_note("S1", "Де вкладення", "оновлено: тека attachments/<sid>") == "де-вкладення"      # заміна, не дубль
        assert len(M.list_notes("S1")) == 1 and "оновлено" in M.read_note("S1", "Де вкладення")
        tok = "A1b2" * 60
        M.save_note("S1", "токен", f"ключ {tok} лежить у .env")
        body = M.read_note("S1", "токен")
        assert tok not in body and "довге значення" in body
        assert M.read_note("S1", "вкладення") and M.read_note("S1", "нема-такої") is None        # нечіткий пошук за назвою
        assert M.save_note("", "x", "y") == "" and M.save_note("S1", "x", "  ") == ""             # без sid/тексту — нічого
        # 2) індекс: коротка суть у рядок, бюджет, '' якщо нотаток нема
        idx = M.index_block("S1")
        assert "recall(name)" in idx and "де-вкладення" in idx and tok not in idx and M.index_block("NOPE") == ""
        for i in range(30):
            M.save_note("S2", f"нотатка {i}", "суть " + "x" * 90)
        small = M.index_block("S2", budget=500)
        assert len(small) <= 560 and "ще" in small
        # 3) ліміт кількості: найстаріші витісняються
        for i in range(M.MAX_NOTES + 5):
            M.save_note("S3", f"n{i}", "текст")
        assert len(M.list_notes("S3")) == M.MAX_NOTES
        # 4) тули remember/recall; без сесії — зрозуміла відмова
        root = Path(tempfile.mkdtemp())
        reg = default_registry()
        ctx = ToolContext(root=root, session_id="S4")
        assert "збережено" in reg.dispatch("remember", {"name": "Висновок", "text": "усе в a.py"}, ctx)
        assert "усе в a.py" in reg.dispatch("recall", {"name": "висновок"}, ctx)
        miss = reg.dispatch("recall", {"name": "нема"}, ctx)
        assert "нема" in miss and "висновок" in miss                                           # підказує, що є
        assert "немає активної сесії" in reg.dispatch("remember", {"name": "a", "text": "b"}, ToolContext(root=root))
        # 5) remember/recall — не «дія»: не вимикають примусовий висновок після читань
        rs = AL.ReadStreak()
        rs.observe("remember")
        rs.observe("recall")
        assert not rs.acted
        # 6) звіт субагента зберігається в нотатки (повторне дослідження не потрібне)
        class Cl:
            last_stats: dict = {}

            def chat(self, messages, tools=None, profile=None, fmt=None):
                return {"content": "Знайшов: handle_upload у lca_web.py", "tool_calls": []}
        c5 = ToolContext(root=root, client=Cl(), session_id="S5")
        out = AL.research_report("Знайди, де обробляється завантаження", "", c5, reg)
        assert "handle_upload" in out and any("дослідження" in s for s, _, _ in M.list_notes("S5"))
        # 7) перейменування й чистка
        M.rename("S5", "S6")
        assert M.list_notes("S6") and not M.list_notes("S5")
        M.clear("S6")
        assert not M.list_notes("S6")
    finally:
        M.ROOT = real
        limits.FORCE = None
    print("OK: sessionmem — нотатки на диску, індекс, remember/recall, автозбереження звіту, ліміти, чистка")


if __name__ == "__main__":
    main()
