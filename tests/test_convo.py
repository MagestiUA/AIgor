"""Офлайн-тест контекст-памʼяті розмови (convo) зі стаб-моделлю.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_convo
"""
from agent import convo


class Stub:
    """Повертає заскриптовані відповіді по черзі (як OllamaClient.chat)."""
    def __init__(self, scripted):
        self.scripted = scripted
        self.i = 0

    def chat(self, messages, tools=None, profile=None, fmt=None):
        c = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return {"content": c}


from agent import limits
limits.FORCE = limits.LOCAL   # не залежати від налаштувань користувача


def msgs(n: int, start: int = 0) -> list[dict]:
    out = []
    for i in range(start, start + n):
        out += [{"role": "user", "content": f"запит {i}", "kind": "text"},
                {"role": "assistant", "content": f"відповідь {i}", "kind": "answer"}]
    return out


class Spy(Stub):
    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.system, self.sent = messages[0]["content"], messages[-1]["content"]
        return super().chat(messages, tools, profile, fmt)


def main() -> None:
    kw = dict(max_msgs=12, max_chars=10_000, keep=6, budget=1000)
    # «лінивий» підсумок: поки хвіст малий — БЕЗ виклику моделі, підсумок і межа не змінюються
    class Boom:
        def chat(self, *a, **k):
            raise AssertionError("модель не мала викликатись")
    m = msgs(5)                                            # 10 повідомлень < 12
    assert convo.compact_tail("", m, 0, Boom(), **kw) == ("", 0)
    # хвіст переріс межу (16 > 12) → ОДИН виклик стискає найстаріші, 6 свіжих лишаються дослівно
    m = msgs(8)
    sp = Spy(["- запити 0-4: зроблено"])
    s, upto = convo.compact_tail("", m, 0, sp, **kw)
    assert s.startswith("- ") and s.endswith("запити 0-4: зроблено"), repr(s)
    assert upto == 10 and len(convo.tail_items(m, upto)) == 6, upto
    assert "запит 0" in sp.sent and "відповідь 4" in sp.sent and "запит 5" not in sp.sent
    assert "SEVERAL" in sp.system and ("files" in sp.system.lower())
    # дослівний хвіст для контексту: після межі, без поточного повідомлення користувача
    tail = convo.dialogue_tail(m + [{"role": "user", "content": "поточне", "kind": "text"}], upto)
    assert "запит 5" in tail and "відповідь 7" in tail and "запит 4" not in tail and "поточне" not in tail
    # результати кроків і примітки-факти теж у хвості; службові примітки — ні
    extra = [{"role": "assistant", "content": "**крок 1**\n\nстворено a.py", "kind": "step"},
             {"role": "assistant", "content": "⛔ Крок #2 не завершено", "kind": "note"},
             {"role": "assistant", "content": "🗜 Стиснув контекст", "kind": "note"},
             {"role": "assistant", "content": "Готово ✓", "kind": "note"}]
    t2 = convo.dialogue_tail(extra, 0)
    assert "Крок виконано: **крок 1**" in t2 and "⛔ Крок #2" in t2 and "Стиснув" not in t2 and "Готово" not in t2
    # чат: лише репліки (рядки «🔧 тул» не рахуються)
    assert len(convo.tail_items(extra + msgs(1), 0, kinds=("text", "answer"))) == 2
    # підсумок понад бюджет → стиснення + сповіщення; збій моделі — нічого не змінюємо
    ev: list = []
    s2, up2 = convo.compact_tail("y" * 2000, msgs(8), 0, Stub(["епізод", "СТИСНУТО"]),
                                 on_compress=lambda w, b, a: ev.append((w, b, a)), **kw)
    assert s2 == "СТИСНУТО" and up2 == 10 and ev and ev[0][0] == "підсумок розмови", (s2, ev)
    assert convo.compact_tail("- було", msgs(8), 0, Stub([""]), **kw) == ("- було", 0)          # порожній епізод
    class Down:
        def chat(self, *a, **k):
            raise RuntimeError("модель лежить")
    assert convo.compact_tail("- було", msgs(8), 0, Down(), **kw) == ("- було", 0)
    # задовгий хвіст за символами (мало повідомлень, але великі) теж стискається
    big = [{"role": "user", "content": "x" * 3000, "kind": "text"} for _ in range(5)]
    assert convo.compact_tail("", big, 0, Stub(["е"]), max_msgs=12, max_chars=5000, keep=2, budget=1000)[1] == 3
    # стара сесія без summary_upto: її підсумок уже покриває всю розмову
    import tempfile
    from pathlib import Path
    from agent import session as S
    d = Path(tempfile.mkdtemp())
    old = S.new_session("т", "")
    old.messages = msgs(3)
    old.context_summary = "- давнє"
    S.save_session(old, base=d)
    import json as _json
    raw = _json.loads((d / f"{old.id}.json").read_text(encoding="utf-8"))
    raw.pop("summary_upto")
    (d / f"{old.id}.json").write_text(_json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert S.load_session(old.id, base=d).summary_upto == 6

    # as_context
    assert convo.as_context("") == ""
    assert "підсумок" in convo.as_context("- щось")

    # kind="chat" використовує ОКРЕМИЙ (не кодинг-орієнтований) промпт
    sp_chat, sp_code = Spy(["е"]), Spy(["е"])
    convo.compact_tail("", msgs(8), 0, sp_chat, kind="chat", **kw)
    convo.compact_tail("", msgs(8), 0, sp_code, kind="code", **kw)
    assert sp_chat.system != sp_code.system
    assert "files" not in sp_chat.system.lower() and "function" not in sp_chat.system.lower()

    # update_digest: перший шматок -> дайджест; другий шматок + дайджест -> новий дайджест
    d1 = convo.update_digest("", "шматок 1: подія А сталась через Б",
                             client=Stub(["Подія А сталась через Б."]))
    assert "Подія А" in d1

    # модель ЛУНАЄ заголовок промпту замість чистого тексту (живий кейс) -> обрізаємо
    d_echo = convo.update_digest("", "шматок", client=Stub(["Поточний дайджест:\nЧистий текст."]))
    assert d_echo == "Чистий текст.", repr(d_echo)

    # той самий echo, але обгорнутий у markdown (живий кейс: "**Поточна нотатка:**")
    d_echo_md = convo.update_digest("", "шматок",
                                    client=Stub(["**Поточний дайджест:**\n\nЧистий текст."]))
    assert d_echo_md == "Чистий текст.", repr(d_echo_md)

    # модель повертає лише наш ВЛАСНИЙ плейсхолдер "(порожньо...)" -> трактуємо як
    # "нічого корисного", лишаємо попередній дайджест (живий кейс: сміттєвий файл
    # теми з вмістом буквально "(порожньо)")
    d_junk = convo.update_digest("старий дайджест", "куций хід",
                                 client=Stub(["(порожньо — це перший шматок)"]))
    assert d_junk == "старий дайджест", repr(d_junk)
    d2 = convo.update_digest(d1, "шматок 2: подія В сталась через А",
                             client=Stub(["Подія А -> Б -> В."]))
    assert "В" in d2
    # переповнення бюджету дайджесту -> другий (стискаючий) прохід
    d3 = convo.update_digest("x" * 200, "ще шматок",
                             client=Stub(["y" * 200_000, "СТИСНУТО"]), budget=1000)
    assert d3 == "СТИСНУТО", repr(d3)

    # generate_title: звичайна відповідь -> чиста назва; echo заголовка ("Title:")
    # обрізаємо; задовгий "title" (модель розговорилась) -> "" (caller лишає дефолт).
    t1 = convo.generate_title("як виправити memory leak в Python", client=Stub(["Memory leak у Python"]))
    assert t1 == "Memory leak у Python", repr(t1)
    t_echo = convo.generate_title("щось", client=Stub(["Title: Чиста назва"]))
    assert t_echo == "Чиста назва", repr(t_echo)
    t_long = convo.generate_title("щось", client=Stub(["x" * 200]))
    assert t_long == "", repr(t_long)
    t_empty = convo.generate_title("щось", client=Stub(["(порожньо)"]))
    assert t_empty == "", repr(t_empty)

    # сповіщення про стиснення (побажання розробників): колбек викликається РІВНО коли
    # відбувся стискаючий прохід — для дайджесту файлу (для підсумку розмови — вище)
    ev: list = []
    cb = lambda what, b, a: ev.append((what, b, a))
    convo.update_digest("x" * 200, "шматок", client=Stub(["y" * 5000, "СТИСНУТО"]), budget=1000, on_compress=cb)
    assert ev and ev[0][0] == "дайджест файлу" and ev[0][2] == len("СТИСНУТО"), ev
    ev.clear()
    convo.update_digest("", "шматок", client=Stub(["короткий дайджест"]), budget=1000, on_compress=cb)
    assert ev == []
    # збій колбека не ламає стиснення
    assert convo.compact_tail("y" * 2000, msgs(8), 0, Stub(["е", "СТИСНУТО"]), on_compress=lambda *a: 1 / 0,
                              **kw)[0] == "СТИСНУТО"

    print("OK: convo — лінивий підсумок (без виклику, поки хвіст малий; один виклик на найстаріші), дослівний хвіст, "
          "бюджет і сповіщення, стара сесія, chat/code промпти, update_digest шматків, generate_title")


if __name__ == "__main__":
    main()
