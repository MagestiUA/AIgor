"""Примусовий висновок після «читання без дій», авто-делегування дослідницьких кроків субагенту, субагент під тиском контексту."""
import tempfile
from pathlib import Path

from agent import agent_loop as AL
from agent import limits, subagent
from agent.toolkit import ToolContext, default_registry

limits.FORCE = limits.REMOTE


class Client:
    """Читає файли без кінця, поки не отримає тексту «Досить збирати» (примусовий висновок)."""
    last_stats: dict = {}

    def __init__(self, write_at=None, prompt=0):
        self.calls, self.n, self.write_at, self.prompt = [], 0, write_at, prompt

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.calls.append([dict(m) for m in messages])
        self.last_stats = {"prompt": self.prompt} if self.prompt else {}
        if messages[-1]["role"] == "user" and "Досить збирати" in messages[-1]["content"]:
            assert tools is None                                           # висновок — без тулів
            return {"content": "ВИСНОВОК: усе в a.py", "tool_calls": []}
        self.n += 1
        name, args = ("read_file", {"path": f"f{self.n}.py"})
        if self.write_at and self.n == self.write_at:
            name, args = "write_file", {"path": "w.py", "content": "1"}
        return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def ctx_for(client):
    root = Path(tempfile.mkdtemp())
    for i in range(1, 80):
        (root / f"f{i}.py").write_text(f"# файл {i}\n" + "x = 1\n" * (i + 3), encoding="utf-8")   # різні вмісти — без «зациклення»
    return ToolContext(root=root, client=client, permissions={"edits": "auto", "shell": "off"})


def main() -> None:
    # 1) лише читання: на 8-му — підказка, на 12-му — примусовий висновок замість нескінченного читання
    cl = Client()
    final, log = AL.run_step("Прочитай файли й скажи, що в них", ctx_for(cl), cl, default_registry(), max_iters=40)
    assert final == "ВИСНОВОК: усе в a.py" and len(log) == AL.READ_FORCE, (final, len(log))
    assert any(AL.READ_NUDGE in m["content"] for c in cl.calls for m in c if m["role"] == "user")
    # 2) бюджет вичерпано на читаннях → теж висновок, а не «ліміт ітерацій»
    cl = Client()
    final, _ = AL.run_step("Прочитай файли", ctx_for(cl), cl, default_registry(), max_iters=5)
    assert final == "ВИСНОВОК: усе в a.py"
    # 3) була дія-запис → крок НЕ примушуємо завершитись (за ним стежить планувальник) → «ліміт ітерацій»
    cl = Client(write_at=2)
    final, _ = AL.run_step("Прочитай і запиши", ctx_for(cl), cl, default_registry(), max_iters=14)
    assert final == AL.ITER_LIMIT_MSG, final
    # 4) що вважаємо дослідницьким кроком
    for t in ("Знайди, де обробляється вставка зображень", "З'ясуй, як працює самооновлення", "Проаналізуй логи"):
        assert AL.is_research_step(t), t
    for t in ("Знайди і виправ помилку у файлі", "Створи файл a.py", "Запусти тести", "Додай функцію"):
        assert not AL.is_research_step(t), t
    # 5) авто-делегування: звіт субагента потрапляє в промпт виконавця, і виконавець не мусить сам читати
    class Both(Client):
        def chat(self, messages, tools=None, profile=None, fmt=None):
            if messages[0]["content"] == subagent.SYSTEM:                  # виклик субагента
                return {"content": "Знайшов: усе в a.py, функція run()", "tool_calls": []}
            self.calls.append([dict(m) for m in messages])
            return {"content": "Відповідь за звітом", "tool_calls": []}
    cl = Both()
    final, log = AL.run_step("Знайди, де викликається main()", ctx_for(cl), cl, default_registry(), max_iters=10)
    first = cl.calls[0][1]["content"]
    assert final == "Відповідь за звітом" and "ЗВІТ СУБАГЕНТА" in first and "усе в a.py" in first and log == []
    cl = Both()                                                             # не дослідницький → звіту нема
    AL.run_step("Створи файл", ctx_for(cl), cl, default_registry(), max_iters=3)
    assert "ЗВІТ СУБАГЕНТА" not in cl.calls[0][1]["content"]
    # 6) субагент під тиском контексту: стискає старі результати й віддає висновок (не «контекст заповнений»)
    cl = Client(prompt=int(0.95 * limits.REMOTE.context_tokens))
    out = subagent.investigate("де що?", ctx_for(cl), cl)
    assert "ВИСНОВОК: усе в a.py" in out and "контекст майже заповнений" not in out, out[:200]
    cl = Client()                                                           # без тиску: бюджет MAX_ITERS → висновок
    out = subagent.investigate("де що?", ctx_for(cl), cl)
    assert "ВИСНОВОК" in out and f"{subagent.MAX_ITERS} дій" in out, out[-120:]
    limits.FORCE = None
    print("OK: forced_conclusion — підказка/примус/бюджет, не чіпає кроки з діями, авто-делегування, субагент під тиском")


if __name__ == "__main__":
    main()
