"""Коментар користувача по ходу виконання бачить і ВИКОНАВЕЦЬ — між своїми діями, не чекаючи кінця кроку.
Реальний State._run_tool_step на заглушках стану/моделі/тулів."""
import asyncio
import types
from pathlib import Path

import lca_web.lca_web as W
from agent import limits

limits.FORCE = limits.REMOTE                      # hard_iters задано → без виклику моделі для оцінки ітерацій


class FakeState:
    def __init__(self):
        self.current_id = "sid-live"
        self._step_touched, self._step_actions = [], []
        self.notes = []
        self.dispatched = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _recent_image_urls(self):
        return []

    def _act_start(self, *a, **k):
        return 1

    def _act_done(self, *a, **k):
        pass

    def _add_stats(self, s):
        pass

    def _append(self, role, content, kind="text", **kw):
        self.notes.append(content)

    async def _dispatch_tool(self, reg, name, args, ctx):
        self.dispatched.append((name, args))
        # користувач пише, ПОКИ виконується дія (між першою і другою відповіддю моделі)
        W._LIVE_NOTES.setdefault(self.current_id, []).append({"text": "стій, не чіпай prod-конфіг", "seen": False})
        return "rc=0"


class Client:
    last_stats: dict = {}

    def __init__(self):
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.calls.append([dict(m) for m in messages])
        if len(self.calls) == 1:
            return {"content": "", "tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "echo 1"}}}]}
        return {"content": "зробив", "tool_calls": []}


def main() -> None:
    st = FakeState()
    cl = Client()
    ctx = types.SimpleNamespace(client=cl)
    run = types.MethodType(W.State._run_tool_step, st)
    final, log = asyncio.run(run("зроби крок", ctx, title="t", context="ctx", stop_event=None))
    assert final == "зробив" and len(cl.calls) == 2
    first = " ".join(m["content"] for m in cl.calls[0] if m["role"] == "user")
    second = [m["content"] for m in cl.calls[1] if m["role"] == "user"]
    assert "не чіпай prod-конфіг" not in first                                   # на першому запиті коментаря ще не було
    assert any("не чіпай prod-конфіг" in c and "ПІД ЧАС виконання" in c for c in second), second   # на другому виконавець його бачить
    # коментар НЕ прибрано: планувальник отримає його після кроку; а виконавцю вдруге не показуємо
    assert W._LIVE_NOTES[st.current_id][0]["seen"] is True and len(W._LIVE_NOTES[st.current_id]) == 1
    assert W._take_live_notes(st.current_id) == ["стій, не чіпай prod-конфіг"] and W._take_live_notes(st.current_id) == []
    # send_task: під час виконання плану повідомлення = коментар по ходу (а не черга «на потім»)
    class SendState:
        def __init__(self, active):
            self.current_id, self.project_root, self.folder_input, self.mode = "sid-send", "R", "", "code"
            self.task, self.attachments, self.busy = "виправ імпорти", [], True
            self.has_pending = self.has_question = False
            self.queued_text, self.msgs = "", []
            if active:
                W._PLAN_ACTIVE.add(self.current_id)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _append(self, role, content, kind="text", **kw):
            self.msgs.append((role, content))

    send = W.State.send_task.fn if hasattr(W.State.send_task, "fn") else W.State.send_task
    s = SendState(active=True)
    asyncio.run(types.MethodType(send, s)())
    assert W._LIVE_NOTES["sid-send"] == [{"text": "виправ імпорти", "seen": False}] and s.queued_text == "", s.queued_text
    assert s.msgs[0] == ("user", "виправ імпорти") and "Коментар прийнято" in s.msgs[1][1]
    W._LIVE_NOTES.pop("sid-send", None)
    W._PLAN_ACTIVE.discard("sid-send")
    # а коли план НЕ виконується (модель просто зайнята) — як і раніше, у чергу
    s = SendState(active=False)
    asyncio.run(types.MethodType(send, s)())
    assert s.queued_text == "виправ імпорти" and "sid-send" not in W._LIVE_NOTES

    limits.FORCE = None
    print("OK: live_comment — виконавець бачить коментар між діями, планувальник отримує його після кроку")


if __name__ == "__main__":
    main()
