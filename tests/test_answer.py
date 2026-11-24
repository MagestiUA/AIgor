"""Офлайн-тест збору контексту для режиму аналізу (answerer.build_context).
Сама відповідь — у спільному циклі з тулами читання (tests/test_read_loop.py).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_answer
"""
import tempfile
from pathlib import Path

from agent.answerer import build_context


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "parser.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
    (root / "util.py").write_text("X = 1\n", encoding="utf-8")

    ctx = build_context(root, question="що робить parser.py?")
    assert "Структура проекту" in ctx
    assert "parser.py" in ctx and "return 42" in ctx
    # util.py не згадувався у питанні -> його вмісту в контексті нема (лише у структурі)
    assert "X = 1" not in ctx
    # read-only джерело (reference) теж потрапляє в контекст; неіснуюче — пропускається
    ctx2 = build_context(root, reference_files=[str(root / "util.py"), str(root / "нема.py")], question="загалом?")
    assert "Reference util.py (read-only):\nX = 1" in ctx2 and "нема.py" not in ctx2
    print("OK: build_context тягне згаданий файл + структуру + read-only джерела")


if __name__ == "__main__":
    main()
