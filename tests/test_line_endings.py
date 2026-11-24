"""Закінчення рядків і надійність сховищ (виправлення після огляду 24-11-2026).

1) На Windows Path.read_text/write_text перетворювали \\n↔\\r\\n: кожен записаний Ігорем файл ставав CRLF, а match_style
   ніколи не бачив CRLF в оригіналі. Тепер LF-файл лишається LF, CRLF — CRLF, а модель завжди бачить \\n.
2) Теми чату — у профілі (раніше в теці коду, яку onefile-exe видаляє на виході); старі копіюються.
3) Сесія пишеться атомарно (tmp + replace), без залишків .tmp.
Усе — у тимчасових теках, реальний профіль не чіпаємо."""
import tempfile
from pathlib import Path

from agent import executor, limits, session, tools, topics
from agent.toolkit import ToolContext, default_registry

limits.FORCE = limits.LOCAL


class Stub:
    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.seen = messages[-1]["content"]
        return {"content": self.replies.pop(0)}


def main() -> None:
    d = Path(tempfile.mkdtemp())
    reg = default_registry()
    ctx = ToolContext(root=d, permissions={"edits": "auto", "shell": "off"})

    # write_file поверх LF-файлу — лишається LF; поверх CRLF — лишається CRLF
    (d / "lf.py").write_bytes(b"a = 1\nb = 2\n")
    (d / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    reg.dispatch("write_file", {"path": "lf.py", "content": "a = 1\nb = 3\n"}, ctx)
    reg.dispatch("write_file", {"path": "crlf.py", "content": "a = 1\nb = 3\n"}, ctx)
    assert (d / "lf.py").read_bytes() == b"a = 1\nb = 3\n", (d / "lf.py").read_bytes()
    assert (d / "crlf.py").read_bytes() == b"a = 1\r\nb = 3\r\n", (d / "crlf.py").read_bytes()

    # новий файл — як написала модель (\n); .bat — CRLF
    reg.dispatch("write_file", {"path": "new.txt", "content": "x\ny\n"}, ctx)
    reg.dispatch("write_file", {"path": "run.bat", "content": "@echo off\necho hi\n"}, ctx)
    assert (d / "new.txt").read_bytes() == b"x\ny\n"
    assert (d / "run.bat").read_bytes() == b"@echo off\r\necho hi\r\n"

    # модель бачить \n навіть у CRLF-файлі
    out = reg.dispatch("read_file", {"path": "crlf.py"}, ctx)
    assert "\r" not in out and "b = 3" in out, repr(out)

    # точкова правка (edit_file → SEARCH/REPLACE) зберігає CRLF
    c = Stub(["<<<<<<< SEARCH\nb = 3\n=======\nb = 4\n>>>>>>> REPLACE"])
    ctx.client = c
    r = reg.dispatch("edit_file", {"path": "crlf.py", "instruction": "b = 4"}, ctx)
    assert "застосовано" in r, r
    assert (d / "crlf.py").read_bytes() == b"a = 1\r\nb = 4\r\n", (d / "crlf.py").read_bytes()
    assert "\r" not in c.seen                                     # у промпт пішов текст із \n

    # переписування цілком (run_edit) теж зберігає CRLF
    res = executor.run_edit(d / "crlf.py", "b = 5", client=Stub(["```python\na = 1\nb = 5\n```"]), write=True)
    assert res.ok and (d / "crlf.py").read_bytes() == b"a = 1\r\nb = 5\r\n", (d / "crlf.py").read_bytes()

    # теми: у профілі, старі копіюються з теки коду (лише відсутні)
    real = topics.TOPICS_ROOT, topics.LEGACY_ROOT
    try:
        topics.TOPICS_ROOT = d / "profile" / "topics"
        topics.LEGACY_ROOT = d / "code" / ".chat_topics"
        topics.LEGACY_ROOT.mkdir(parents=True)
        (topics.LEGACY_ROOT / "стара тема.txt").write_text("нотатка", encoding="utf-8")
        assert topics.migrate_legacy() == 1 and topics.migrate_legacy() == 0
        assert topics.load_topic("стара тема") == "нотатка"
        assert (topics.LEGACY_ROOT / "стара тема.txt").exists()               # оригінал не чіпаємо
        assert ".local-code-agent" in str(real[0]) and "topics" in str(real[0])
    finally:
        topics.TOPICS_ROOT, topics.LEGACY_ROOT = real

    # сесія: атомарний запис, без залишків .tmp, читається назад
    sd = d / "sessions"
    s = session.new_session("т", str(d))
    s.add_message("user", "привіт")
    session.save_session(s, base=sd)
    session.save_session(s, base=sd)
    assert not list(sd.glob("*.tmp")), list(sd.glob("*"))
    assert session.load_session(s.id, base=sd).messages[0]["content"] == "привіт"
    assert [m["id"] for m in session.list_sessions(base=sd)] == [s.id]
    print("OK: line_endings — LF/CRLF зберігаються, модель бачить \\n, теми в профілі, атомарний запис сесії")


if __name__ == "__main__":
    main()
