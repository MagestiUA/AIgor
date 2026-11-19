"""Офлайн-тест класифікації наміру (стаб замість Ollama).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_intent
"""
from agent.intent import classify_intent


class Stub:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages, tools=None, profile=None, fmt=None):
        return {"content": self.reply}


def main() -> None:
    assert classify_intent("відрефактори parser.py", Stub('{"mode": "edit"}')) == "edit"
    assert classify_intent("давай спершу сплануємо", Stub('{"mode": "plan"}')) == "plan"
    assert classify_intent("що робить ця функція?", Stub('{"mode": "answer"}')) == "answer"
    assert classify_intent("запусти тести", Stub('{"mode": "shell"}')) == "shell"
    # нерозбірливе / зламане -> безпечний дефолт answer
    assert classify_intent("???", Stub("not json")) == "answer"
    assert classify_intent("???", Stub('{"mode": "wat"}')) == "answer"
    # контекст: класифікатор бачить попередню відповідь асистента (фідбек: «роби» без контексту — навмання)
    class Spy(Stub):
        seen = ""

        def chat(self, messages, tools=None, profile=None, fmt=None):
            Spy.seen = messages[-1]["content"]
            return {"content": self.reply}

    classify_intent("роби", Spy('{"mode": "edit"}'), context="Попередня відповідь: так, додамо експорт у CSV")
    assert "додамо експорт у CSV" in Spy.seen and Spy.seen.endswith("роби"), Spy.seen
    classify_intent("роби", Spy('{"mode": "edit"}'))
    assert Spy.seen == "роби"                      # без контексту — як раніше

    # питання про можливість / «просто питання» -> answer БЕЗ виклику моделі; ввічливе прохання — іде в модель
    class Boom(Stub):
        def chat(self, *a, **k):
            raise AssertionError("модель не мала викликатись")

    assert classify_intent("чи можна додати хелс-чек сервіс для кластера?", Boom("")) == "answer"
    assert classify_intent("це було просто питання, без вимоги писати код", Boom("")) == "answer"
    assert classify_intent("чи можеш додати тести?", Stub('{"mode": "edit"}')) == "edit"
    # ── режим «правки: auto»: лог помилки / повідомлення про збій = виправляти, а не лише пояснювати ──
    from agent import orchestration as O
    TB = ("Traceback (most recent call last):" + chr(10) + '  File "app.py", line 12, in <module>' + chr(10) +
          "    main()" + chr(10) + "ValueError: bad value")
    class Boom(Stub):
        def chat(self, *a, **k):
            raise AssertionError("модель не мала викликатись")
    assert O.looks_like_error_log(TB) and O.looks_like_error_log("npm ERR! code E404" + chr(10) + "npm ERR! 404 Not Found" + chr(10) + "npm ERR! x")
    assert not O.looks_like_error_log("поясни, як працює парсер?") and not O.looks_like_error_log("ValueError")        # одне слово — не лог
    assert O.explicit_no_change("поясни, але не змінюй код") and O.explicit_no_change("just a question") and not O.explicit_no_change(TB)
    assert classify_intent(TB, Boom(""), autonomous=True) == "edit"                       # auto + лог → edit без виклику моделі
    assert classify_intent(TB + chr(10) + "не змінюй код, лише поясни", Stub('{"mode": "answer"}'), autonomous=True) == "answer"
    assert classify_intent(TB, Stub('{"mode": "answer"}'), autonomous=False) == "answer"  # без auto — як раніше (модель)
    class Sys(Stub):
        seen = ""

        def chat(self, messages, tools=None, profile=None, fmt=None):
            Sys.seen = messages[0]["content"] + "|" + messages[-1]["content"]
            return {"content": self.reply}
    assert classify_intent("що тут не так?", Sys('{"mode": "edit"}'), autonomous=True, has_attachments=True) == "edit"
    assert "AUTONOMOUS MODE" in Sys.seen and "прикріплено файли" in Sys.seen
    classify_intent("розбери цей збій у парсері", Sys('{"mode": "answer"}'), autonomous=False)
    assert "AUTONOMOUS MODE" not in Sys.seen                                               # без auto правила нема
    # питання з прикріпленим логом в auto іде до моделі (а не автоматично «answer»); без вкладення — старий шорткат
    assert classify_intent("що тут не так?", Boom(""), autonomous=True, has_attachments=False) == "answer"
    assert "РЕЖИМ САМОСТІЙНИХ ПРАВОК" in O.AUTONOMY_NOTE and "ВИПРАВ" in O.AUTONOMY_NOTE
    print("OK: класифікація наміру edit/plan/answer/shell + дефолт answer")


if __name__ == "__main__":
    main()
