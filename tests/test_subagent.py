"""Субагент-дослідник: ізольований контекст, лише читання, короткий висновок без довгих літералів."""
import tempfile
from pathlib import Path

from agent import literals, subagent
from agent.toolkit import ToolContext, default_registry

SECRET = "A1b2" * 60            # довгий літерал (240 символів)


class Client:
    last_stats: dict = {}

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.calls.append([dict(m) for m in messages])
        n = len(self.calls)
        if n == 1:
            return {"content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "big.py"}}}]}
        if n == 2:        # спроба змінити файл — субагенту заборонено
            return {"content": "", "tool_calls": [{"function": {"name": "write_file", "arguments": {"path": "x.py", "content": "1"}}}]}
        return {"content": f"Токен у big.py: {SECRET}. Функція get() на рядку 2.", "tool_calls": []}


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "big.py").write_text(f'TOKEN = "{SECRET}"\ndef get():\n    return TOKEN\n' + "# рядок\n" * 3000, encoding="utf-8")
    ctx = ToolContext(root=root, client=Client())
    assert "investigate" in default_registry().names() and "investigate" not in subagent.readonly_registry().names()
    out = default_registry().dispatch("investigate", {"task": "де токен?"}, ctx)
    cl = ctx.client
    assert "big.py" in out and SECRET not in out and "довге значення" in out, out[:300]        # довгий літерал не переказується
    assert len(out) < subagent.REPORT_MAX + 200 and "субагент: 2 дій" in out
    assert not (root / "x.py").exists()                                                       # лише читання
    assert cl.calls[0][0]["role"] == "system" and len(cl.calls[0]) == 2                       # чистий контекст: system + завдання
    assert "недоступний субагенту" in cl.calls[2][-1]["content"]
    assert len(out) < 500 < sum(len(m.get("content") or "") for m in cl.calls[2])             # у головний контекст — короткий висновок
    # шар літералів не зачеплено: strip/restore як і раніше повертає байт-у-байт
    src = 'import json\nDATA = json.loads("' + SECRET * 6 + '")\n' + '# x\n' * 5
    code, mp = literals.strip_code(src)
    assert SECRET * 6 not in code and literals.restore_code(code, mp) == src
    print("OK: subagent — чистий контекст, лише читання, короткий висновок, літерали не переказуються")


if __name__ == "__main__":
    main()
