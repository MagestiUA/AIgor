"""recall_history: пошук у повній історії сесії; модель ЗНАЄ про тул (підказка в підсумку, системний промпт, опис тула)."""
import tempfile
from pathlib import Path

from agent import agent_loop as AL, convo, history, session as sess
from agent.toolkit import ToolContext, default_registry


def main() -> None:
    real = sess.SESSIONS_DIR
    sess.SESSIONS_DIR = Path(tempfile.mkdtemp()) / "sessions"            # ніколи не чіпаємо реальні сесії
    try:
        msgs = [{"role": "user", "content": "Запусти pytest tests/test_parser.py -x і подивись помилку", "kind": "text"},
                {"role": "assistant", "content": "Помилка: KeyError: 'timeout' у parser.py:42, конфіг без поля timeout.", "kind": "answer"},
                {"role": "assistant", "content": "службова нотатка про стиснення", "kind": "note"},
                {"role": "user", "content": "Додай ще логування у модуль auth", "kind": "text"},
                {"role": "assistant", "content": "x" * 9000, "kind": "answer"}]
        # 1) пошук: знаходить за словом (з відмінками), показує номер, пропускає службові повідомлення
        out = history.search(msgs, "помилки timeout")
        assert "#2" in out and "KeyError: 'timeout'" in out and "службова" not in out
        assert "нічого не знайдено" in history.search(msgs, "кубернетес")
        # 2) читання повністю за номером, обрізання довгих, неіснуючий номер
        assert "pytest tests/test_parser.py" in history.search(msgs, number=1)
        assert "обрізано" in history.search(msgs, number=5) and "нема" in history.search(msgs, number=3)
        # 3) без запиту — останні репліки; порожня історія
        assert "#4" in history.search(msgs) and "порожня" in history.search([])
        # 4) тул на справжній сесії; без сесії — зрозуміла відмова
        s = sess.new_session("t", tempfile.mkdtemp())
        s.messages = msgs
        sess.save_session(s)
        reg = default_registry()
        ctx = ToolContext(root=Path(tempfile.mkdtemp()), session_id=s.id)
        assert "KeyError" in reg.dispatch("recall_history", {"query": "timeout"}, ctx)
        assert "pytest" in reg.dispatch("recall_history", {"number": 1}, ctx)
        assert "немає активної сесії" in reg.dispatch("recall_history", {"query": "a"}, ToolContext(root=ctx.root))
        assert "не знайдено" in reg.dispatch("recall_history", {"query": "a"}, ToolContext(root=ctx.root, session_id="нема"))
        # 5) модель ЗНАЄ про можливість: підсумок несе підказку, системний промпт згадує тул, тул читальний
        assert "recall_history" in convo.as_context("- щось зроблено") and convo.as_context("") == ""
        assert "recall_history" in AL.SYSTEM and "recall_history" in AL.READ_ONLY_TOOLS
        assert any(t.name == "recall_history" for t in reg.tools.values()) if hasattr(reg, "tools") else True
    finally:
        sess.SESSIONS_DIR = real
    print("OK: history — пошук/номер/тул/підказки моделі")


if __name__ == "__main__":
    main()
