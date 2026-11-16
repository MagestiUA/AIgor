"""Точкові правки (agent/patching.py + executor.run_patch): модель повертає лише SEARCH/REPLACE-блоки,
файл не переписується цілком. Модель підмінено скриптом відповідей, файли — у тимчасовій теці."""
import tempfile
from pathlib import Path

from agent import executor, patching
from agent.executor import run_patch


class Script:
    """Підміна моделі: віддає заготовлені відповіді по черзі й запам'ятовує, що їй надсилали."""
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, profile=None, **kw):
        self.calls.append(messages)
        return {"content": self.replies.pop(0)}


def blk(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


def w(d: Path, name: str, text: str, newline: str = "\n") -> Path:
    p = d / name
    p.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return p


def main() -> None:
    # ── чисті функції ────────────────────────────────────────────────────────────
    bs = patching.parse_blocks("```\n" + blk("a = 1", "a = 2") + "\n```\n" + blk("", "tail") + "\n"
                               "<<<<<<< SEARCH\nx\n=======\n>>>>>>> REPLACE")
    assert [(b.search, b.replace) for b in bs] == [(["a = 1"], ["a = 2"]), ([], ["tail"]), (["x"], [])]
    text = "a = 1\nx\nb = 3"
    new, failed = patching.apply_blocks(text, bs)
    assert new == "a = 2\nb = 3\ntail" and not failed, (new, failed)
    new, failed = patching.apply_blocks("q\nq", [patching.Block(["q"], ["z"])])
    assert failed and "2 разів" in failed[0][1] and new == "q\nq"                    # неоднозначно — не чіпаємо
    new, failed = patching.apply_blocks("def f():\n    return 1", [patching.Block(["def g():"], ["x"])])
    assert failed and "не знайдено" in failed[0][1] and "def f():" in failed[0][1]  # підказка про схоже місце

    d = Path(tempfile.mkdtemp())

    # ── 1) успіх: дві точкові правки; решта файлу байт-у-байт; у промпті файл, у відповіді лише фрагменти
    src = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    p = w(d, "m.py", src)
    c = Script(blk("    return 1", "    return 10") + "\n" + blk("    return 2", "    return 20"))
    r = run_patch(p, "змінити константи", c, write=True)
    assert r.ok, r.error
    assert p.read_text(encoding="utf-8") == src.replace("return 1", "return 10").replace("return 2", "return 20")
    assert len(c.calls) == 1 and "SEARCH/REPLACE" in c.calls[0][0]["content"]
    assert "-    return 1" in r.diff and "+    return 10" in r.diff and r.backup

    # ── 2) відступи: файл на табах, модель дала пробіли → збіг без відступів + привели до табів (py валідний)
    tabbed = "class A:\n\tdef f(self):\n\t\tx = 1\n\t\treturn x\n"
    p = w(d, "t.py", tabbed)
    c = Script(blk("        x = 1", "        x = 1\n        y = 2"))
    r = run_patch(p, "додай y", c, write=True)
    assert r.ok, r.error
    assert p.read_text(encoding="utf-8") == "class A:\n\tdef f(self):\n\t\tx = 1\n\t\ty = 2\n\t\treturn x\n"

    # ── 3) неоднозначний SEARCH → друга спроба з поясненням → успіх; успішні блоки не повторюються
    src = "def f():\n    pass\n\n\ndef g():\n    pass\n"
    p = w(d, "amb.py", src)
    c = Script(blk("    pass", "    return 1"),
               blk("def g():\n    pass", "def g():\n    return 2"))
    r = run_patch(p, "g повертає 2", c, write=True)
    assert r.ok, r.error
    assert p.read_text(encoding="utf-8") == "def f():\n    pass\n\n\ndef g():\n    return 2\n"
    assert len(c.calls) == 2 and "2 разів" in c.calls[1][-1]["content"] and "НЕ повторюй" in c.calls[1][-1]["content"]

    # ── 4) великий файл і провал двічі: помилка, файл не змінено, а НЕ переписування цілком
    big = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(900))        # > WHOLE_FILE_MAX
    assert len(big) > executor.WHOLE_FILE_MAX
    p = w(d, "big.py", big)
    c = Script(blk("def нема():", "x"), blk("def теж_нема():", "y"))
    r = run_patch(p, "щось", c, write=True)
    assert not r.ok and "Файл не змінено" in r.error and len(c.calls) == 2
    assert p.read_text(encoding="utf-8") == big
    c = Script("пояснення без блоків", "знову без блоків")
    r = run_patch(p, "щось", c, write=True)
    assert not r.ok and "жодного блоку" in r.error and p.read_text(encoding="utf-8") == big

    # ── 5) малий файл і провал двічі → запасний шлях: переписування цілком (як раніше)
    p = w(d, "small.py", "x = 1\n")
    c = Script(blk("нема", "y"), blk("нема", "y"), "```python\nx = 2\n```")
    r = run_patch(p, "x=2", c, write=True)
    assert r.ok and p.read_text(encoding="utf-8") == "x = 2\n" and len(c.calls) == 3

    # ── 6) CRLF зберігається; точкова правка не чіпає решту
    p = w(d, "crlf.py", "a = 1\nb = 2\n", "\r\n")
    c = Script(blk("b = 2", "b = 3"))
    assert run_patch(p, "b=3", c, write=True).ok
    assert p.read_bytes() == b"a = 1\r\nb = 3\r\n"

    # ── 7) великі літерали: у промпт не потрапляють і лишаються байт-у-байт
    lit = "Z9" * 800
    src = f'DATA = "{lit}"\n\n\ndef f():\n    return 1\n'
    p = w(d, "lit.py", src)
    c = Script(blk("    return 1", "    return 2"))
    r = run_patch(p, "return 2", c, write=True)
    assert r.ok and lit not in c.calls[0][1]["content"] and "__LIT_0__" in c.calls[0][1]["content"]
    assert p.read_text(encoding="utf-8") == src.replace("return 1", "return 2")

    # ── 8) не-Python (YAML/Dockerfile): без ast-перевірки
    p = w(d, "c.yml", "services:\n  app:\n    image: x\n")
    c = Script(blk("    image: x", '    image: x\n    ports:\n      - "8080:8080"'))
    assert run_patch(p, "ports", c, write=True).ok
    assert p.read_text(encoding="utf-8").endswith('      - "8080:8080"\n')

    # ── 9) правка ламає синтаксис .py → помилка, файл не змінено
    p = w(d, "bad.py", "def f():\n    return 1\n")
    c = Script(blk("def f():", "def f(:"))
    r = run_patch(p, "зламай", c, write=True)
    assert not r.ok and "синтаксична помилка" in r.error and p.read_text(encoding="utf-8") == "def f():\n    return 1\n"

    # ── 10) без write=True файл не пишеться (прев'ю diff для режиму ask)
    p = w(d, "prev.py", "k = 1\n")
    r = run_patch(p, "k=2", Script(blk("k = 1", "k = 2")), write=False)
    assert r.ok and "+k = 2" in r.diff and p.read_text(encoding="utf-8") == "k = 1\n"
    print("OK: patching — точкові SEARCH/REPLACE, відступи/CRLF/літерали, повтор із поясненням, безпечні провали")


if __name__ == "__main__":
    main()
