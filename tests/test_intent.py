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

    print("OK: класифікація наміру edit/plan/answer/shell + дефолт answer")


if __name__ == "__main__":
    main()
