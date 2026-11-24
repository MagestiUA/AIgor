"""Маршрутизація запиту: однозначне — кодом (intent.quick_route, без моделі), решту — планувальник у ТОМУ САМОМУ виклику,
що й планування (planner.deliberate(route=True)), замість окремого класифікатора наміру.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_intent
"""
import json

from agent import orchestration as O
from agent.intent import quick_route
from agent.planner import deliberate


class Stub:
    def __init__(self, reply):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        self.system = self.user = ""
        self.fmt = None

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.system, self.user, self.fmt = messages[0]["content"], messages[-1]["content"], fmt
        return {"content": self.reply}


def main() -> None:
    # ── quick_route: однозначні випадки без моделі ──
    TB = ("Traceback (most recent call last):" + chr(10) + '  File "app.py", line 12, in <module>' + chr(10) +
          "    main()" + chr(10) + "ValueError: bad value")
    assert quick_route("чи можна додати хелс-чек сервіс для кластера?") == "answer"
    assert quick_route("це було просто питання, без вимоги писати код") == "answer"
    assert quick_route("чи можеш додати тести?") == ""                                   # ввічливе прохання — вирішує планувальник
    assert quick_route("відрефактори parser.py") == ""
    assert quick_route(TB, autonomous=True) == "edit"                                    # auto + лог → виправляти
    assert quick_route(TB + chr(10) + "не змінюй код, лише поясни", autonomous=True) == ""
    assert quick_route(TB, autonomous=False) == ""                                       # без auto — вирішує планувальник
    assert quick_route("що тут не так?", autonomous=True, has_attachments=True) == ""     # auto + лог-вкладення → планувальник
    assert quick_route("що тут не так?", autonomous=True, has_attachments=False) == "answer"
    assert O.looks_like_error_log(TB) and not O.looks_like_error_log("ValueError")
    assert O.explicit_no_change("поясни, але не змінюй код") and not O.explicit_no_change(TB)
    assert "РЕЖИМ САМОСТІЙНИХ ПРАВОК" in O.AUTONOMY_NOTE and "ВИПРАВ" in O.AUTONOMY_NOTE

    # ── deliberate(route=True): той самий виклик визначає тип запиту ──
    s = Stub({"action": "answer", "reasoning": "це питання"})
    r = deliberate("як працює парсер?", client=s, route=True)
    assert r["action"] == "answer" and r["state"] is None
    assert '"answer"' in json.dumps(s.fmt) and '"shell"' in json.dumps(s.fmt) and "FIRST decide" in s.system
    assert deliberate("запусти тести", client=Stub({"action": "shell"}), route=True)["action"] == "shell"
    r = deliberate("спершу план: додай експорт", client=Stub({"action": "plan", "wait_for_approval": True,
                    "steps": [{"kind": "act", "description": "додати експорт", "target": "a.py"}]}), route=True)
    assert r["action"] == "plan" and r["wait"] and r["state"].steps[0].kind == "act"
    # без route — маршрутизації нема: answer/shell недопустимі (→ plan), схема їх не містить
    s = Stub({"action": "answer"})
    r = deliberate("x", client=s)
    assert r["action"] == "plan" and '"answer"' not in json.dumps(s.fmt) and "FIRST decide" not in s.system
    # нерозбірлива відповідь маршрутизатора → безпечне «лише відповісти» (як колишній дефолт класифікатора)
    assert deliberate("???", client=Stub("not json"), route=True)["action"] == "answer"
    # режим auto й вкладення — у промпті маршрутизатора
    s = Stub({"action": "plan", "steps": [{"kind": "act", "description": "виправити"}]})
    deliberate("що тут не так?", client=s, route=True, autonomous=True, has_attachments=True)
    assert "AUTONOMOUS MODE" in s.system and "прикріплено файли" in s.user
    s = Stub({"action": "answer"})
    deliberate("розбери збій", client=s, route=True)
    assert "AUTONOMOUS MODE" not in s.system
    print("OK: маршрутизація — quick_route без моделі + answer/shell/plan у виклику планувальника")


if __name__ == "__main__":
    main()
