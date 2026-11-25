"""Питання планувальника й відповідь на нього (регресія зі скриншота користувача, локальна модель):
  * планувальник обрав clarify, але питання не сформулював → раніше в чаті виходило «****» (у markdown це горизонтальна
    лінія) без жодного питання; тепер пояснення і є питання;
  * відповідь, введена в поле (а не клік по кнопці), раніше відкладалась у чергу й нічого не відбувалось — тепер це
    відповідь планувальнику, і планування продовжується;
  * планувальник не питає того, що виконавець знайде сам (промпт).
Реальні State._handle_decision / _resume_planning / send_task на заглушках; сесії — у тимчасовій теці."""
import asyncio
import json
import tempfile
import types
from pathlib import Path

import lca_web.state as W
from agent import limits, planner
from agent import session as sess

limits.FORCE = limits.production()


class FakeState:
    def __init__(self, sid):
        self.current_id, self.status = sid, ""
        self.has_question = self.has_pending = False
        self.q_text, self.q_reasoning = "", ""
        self.q_options, self.msgs = [], []
        for n in ("_decide", "_handle_decision", "_deliberate_flow", "_resume_planning"):
            setattr(self, n, types.MethodType(getattr(W.State, n), self))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _attachments_note(self):
        return ""

    def _recent_image_urls(self):
        return []

    def _append(self, role, content, kind="text", **kw):
        self.msgs.append((role, content, kind))

    def _load_question(self, q):
        self.has_question = bool(q)


class Stub:
    """Модель-планувальник: повертає заскриптовані JSON-відповіді й запам'ятовує, що отримала."""
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.seen.append(messages[-1]["content"])
        return {"content": json.dumps(self.answers.pop(0), ensure_ascii=False)}


def new_state(tmp):
    sess.SESSIONS_DIR = tmp / "sessions"
    s = sess.new_session("т", "R")
    sess.save_session(s)
    return FakeState(s.id)


def main() -> None:
    real = sess.SESSIONS_DIR
    tmp = Path(tempfile.mkdtemp())
    try:
        # 1) clarify БЕЗ питання (лише пояснення), без варіантів — як на скриншоті
        st = new_state(tmp)
        why = "Користувач просить додати оповіщення про збої зв'язку, але не вказав, де саме викликати send_to_telegram."
        result = {"action": "clarify", "reasoning": why, "question": "", "options": [], "state": None}
        asyncio.run(st._handle_decision(result, "додай оповіщення", "ctx", None, [], "R", True, None))
        role, content, kind = st.msgs[-1]
        assert "****" not in content and content == f"**{why}**" and kind == "plan", content
        assert st.has_question and st.q_text == why and st.q_options == []
        pq = sess.load_session(st.current_id).pending_question
        assert pq["question"] == why and pq["reasoning"] == "", pq
        # ні пояснення, ні питання → загальне прохання відповісти (а не порожня лінія)
        st2 = new_state(tmp)
        asyncio.run(st2._handle_decision({"action": "choose", "reasoning": "", "question": "  ", "options": [], "state": None},
                                         "t", "c", None, [], "R", True, None))
        assert "****" not in st2.msgs[-1][1] and "уточнення" in st2.msgs[-1][1] and st2.has_question
        # нормальне питання — як було: пояснення, потім питання жирним
        st3 = new_state(tmp)
        asyncio.run(st3._handle_decision({"action": "clarify", "reasoning": "Є два файли.", "question": "Який саме?",
                                          "options": [{"label": "a.py", "detail": ""}], "state": None},
                                         "t", "c", None, [], "R", True, None))
        assert st3.msgs[-1][1] == "Є два файли.\n\n**Який саме?**" and st3.q_options == [{"label": "a.py", "detail": ""}]

        # 2) відповідь на питання → планувальник її бачить і складає план (показується на схвалення: plan_first)
        stub = Stub([{"action": "plan", "reasoning": "ок", "steps": [
            {"kind": "act", "description": "Знайти методи з викликами telnet/ssh", "target": None},
            {"kind": "act", "description": "Додати туди відправку сповіщення при помилці", "target": None}]}])
        task, outcome = asyncio.run(st._resume_planning("знайди в яких методах відбуваються виклики через телнет/ssh", "R", True, stub))
        assert "знайди в яких методах" in stub.seen[0] and "Вже уточнено з користувачем" in stub.seen[0], stub.seen[0][-300:]
        assert task == "додай оповіщення" and outcome == ""                      # план чекає схвалення
        assert not st.has_question and st.has_pending and st.msgs[-1][2] == "plan" and "Знайти методи" in st.msgs[-1][1]
        s_after = sess.load_session(st.current_id)
        assert s_after.pending_question is None and s_after.pending_plan is not None

        # 3) введений у поле текст при активному питанні — відповідь, а не черга; зайнята модель / план — як раніше черга
        limits.FORCE = limits.production()

        class SendState:
            def __init__(self, **kw):
                self.current_id, self.project_root, self.folder_input, self.mode = "sid-q", "R", "", "code"
                self.task, self.attachments, self.busy = "моя відповідь", [], False
                self.has_pending, self.has_question = False, True
                self.queued_text, self.msgs, self.processed = "", [], []
                self.__dict__.update(kw)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def _append(self, role, content, kind="text", **kw):
                self.msgs.append((role, content))

            async def _process_one(self, text):
                self.processed.append(text)

            async def _drain_and_unbusy(self):
                pass

        send = W.State.send_task.fn if hasattr(W.State.send_task, "fn") else W.State.send_task
        s = SendState()
        asyncio.run(types.MethodType(send, s)())
        assert s.processed == ["моя відповідь"] and s.queued_text == "" and s.busy, (s.processed, s.queued_text)
        s = SendState(busy=True)                                                  # планувальник саме думає → у чергу
        asyncio.run(types.MethodType(send, s)())
        assert s.processed == [] and s.queued_text == "моя відповідь"
        s = SendState(has_question=False, has_pending=True)                       # план чекає «Виконати» → у чергу
        asyncio.run(types.MethodType(send, s)())
        assert s.processed == [] and s.queued_text == "моя відповідь"

        # 4) промпт: не питати того, що виконавець знайде сам; питання обов'язкове
        assert "NEVER ask the user for something the executor can FIND" in planner.DELIB_SYSTEM
        assert "REQUIRED" in planner.DELIB_SYSTEM and "research step" in planner.DELIB_SYSTEM
        assert "NEVER ask" in planner.DELIB_SYSTEM + planner.ROUTE_RULES
    finally:
        sess.SESSIONS_DIR = real
        limits.FORCE = None
    print("OK: question_flow — порожнє питання не дає «****», відповідь у полі продовжує планування, черга лишилась для зайнятого/плану")


if __name__ == "__main__":
    main()
