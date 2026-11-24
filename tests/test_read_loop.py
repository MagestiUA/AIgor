"""Режими чату й аналізу на СПІЛЬНОМУ ядрі циклу (State._run_read_loop → agent.steploop.run_loop).

Реальні State._run_read_loop / _run_stream / _synthesize_with_retry / _needs_digest на заглушці стану; модель — скрипт.
Перевіряємо: сильна модель — один цикл із роздумами й стрімом, тули читання виконуються, відповідь — остання репліка;
слабка (weak_model_fixes) — збір без роздумів + окремий синтез; набір тулів лише для читання; велике вкладення частинами
дочитується само й зливається в дайджест; маленьке — без дайджесту; порожня відповідь → синтез; стоп користувача."""
import asyncio
import tempfile
import threading
import types
from dataclasses import replace
from pathlib import Path

import lca_web.state as W
from agent import config, limits
from agent.toolkit import ToolContext


class FakeState:
    def __init__(self, cid="sid-read"):
        self.current_id = cid
        self.status = ""
        self.notes: list[tuple[str, str, str]] = []
        self.streaming, self.stream_content, self.stream_thinking = False, "", ""
        self.tok_out = self.tok_prompt = self.tok_eval_ns = 0
        self.tok_tps, self.tok_model, self.stopping, self.ctx_live = 0.0, "", False, 0
        self.added = []
        for name in ("_run_read_loop", "_run_stream", "_synthesize_with_retry", "_needs_digest", "_digest_attachment_chunk",
                     "_dispatch_tool", "_dispatch_tool_inner", "_recompute_tps"):
            setattr(self, name, types.MethodType(getattr(W.State, name), self))
        self._READONLY_TOOLS, self.CHAT_FINALIZE = W.State._READONLY_TOOLS, W.State.CHAT_FINALIZE
        self._PAGINATED_HEADER_RE, self._CONTINUATION_SUFFIX_RE = W.State._PAGINATED_HEADER_RE, W.State._CONTINUATION_SUFFIX_RE

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _act_start(self, *a, **k):
        return 1

    def _act_done(self, *a, **k):
        pass

    def _add_stats(self, s):
        self.added += list(s)

    def _append(self, role, content, kind="text", thinking="", **kw):
        self.notes.append((kind, content, thinking))

    def _apply_compress_events(self, cid, events):
        pass


class Client:
    """Скриптована модель: chat (без стріму) і chat_stream (події як у llm.chat_stream)."""

    def __init__(self, script):
        self.script, self.calls, self.last_stats = list(script), [], {"prompt": 100, "out": 10}

    def _next(self, messages, tools, profile):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": [t["function"]["name"] for t in tools or []],
                           "think": bool((profile or {}).get("think")), "stream": False})
        return self.script.pop(0) if self.script else {"content": "кінець"}

    def chat(self, messages, tools=None, profile=None, fmt=None):
        m = self._next(messages, tools, profile)
        return {"role": "assistant", "content": m.get("content", ""), **({"tool_calls": m["tool_calls"]} if m.get("tool_calls") else {})}

    def chat_stream(self, messages, tools=None, profile=None, fmt=None, stop_event=None):
        m = self._next(messages, tools, profile)
        self.calls[-1]["stream"] = True
        if m.get("thinking"):
            yield {"content": "", "thinking": m["thinking"], "done": False}
        if m.get("content"):
            yield {"content": m["content"], "thinking": "", "done": False}
        yield {"content": "", "thinking": "", "done": True, "tool_calls": m.get("tool_calls"), "stats": {"prompt": 100, "out": 10}}


def call(name, **args):
    return {"tool_calls": [{"function": {"name": name, "arguments": args}}]}


def run(st, client, msgs, tctx, floor=0):
    return asyncio.run(st._run_read_loop(msgs, tctx, client, floor))


def main() -> None:
    proj = Path(tempfile.mkdtemp())
    (proj / "app.py").write_text("def handler():\n    return 42\n", encoding="utf-8")
    att = Path(tempfile.mkdtemp())
    real_digest = W.convo.update_digest
    W.convo.update_digest = lambda prev, chunk, client=None, **kw: (prev + f"|дайджест {len(chunk)}").strip("|")
    try:
        base = replace(limits.REMOTE, weak_model_fixes=False)
        limits.FORCE = base

        # 1) сильна модель: роздуми + стрім, тули читання (search → read_file), відповідь — остання репліка циклу
        st = FakeState()
        cl = Client([{"thinking": "шукаю", **call("search", pattern="def handler")},
                     call("read_file", path="app.py", start_line=1, limit=5),
                     {"thinking": "знайшов", "content": "Функція handler повертає 42 (app.py:1)."}])
        tctx = ToolContext(root=proj, permissions={"edits": "off", "shell": "off"}, session_id="sid-read")
        out = run(st, cl, [{"role": "system", "content": "s"}, {"role": "user", "content": "що повертає handler?"}], tctx)
        assert out == "Функція handler повертає 42 (app.py:1).", out
        assert all(c["stream"] and c["think"] for c in cl.calls) and len(cl.calls) == 3, cl.calls
        assert set(cl.calls[0]["tools"]) == set(W.State._READONLY_TOOLS), cl.calls[0]["tools"]          # лише читання
        assert not {"write_file", "run_shell", "edit_file", "replace_in_file"} & set(cl.calls[0]["tools"])
        tool_msgs = [m["content"] for m in cl.calls[2]["messages"] if m["role"] == "tool"]
        assert "app.py:1: def handler():" in tool_msgs[0] and "return 42" in tool_msgs[1], tool_msgs
        kinds = [k for k, _, _ in st.notes]
        assert kinds == ["step", "step", "answer"] and st.notes[-1][2] == "знайшов", st.notes
        assert not st.added                                                   # токени стріму рахує _run_stream

        # 2) слабка модель: збір БЕЗ роздумів (без стріму), потім окремий синтез із роздумами
        limits.FORCE = replace(base, weak_model_fixes=True)
        st = FakeState()
        cl = Client([call("read_file", path="app.py"), {"content": "чернетка без роздумів"},
                     {"thinking": "синтез", "content": "Повертає 42."}])
        out = run(st, cl, [{"role": "system", "content": "s"}, {"role": "user", "content": "що повертає?"}], tctx)
        assert out == "Повертає 42." and [c["stream"] for c in cl.calls] == [False, False, True], cl.calls
        assert not cl.calls[0]["think"] and cl.calls[2]["think"] and st.added       # без стріму — токени додано тут
        limits.FORCE = base

        # 3) велике вкладення (більше за limits.inline): модель прочитала частину й «відповідає» — решту дочитуємо самі,
        #    кожна частина зливається в дайджест; маленьке вкладення — як є, без дайджесту
        small = replace(base, chunk=4000, inline=6000, read_chars=4000)
        limits.FORCE = small
        (att / "big.txt").write_text("".join(f"рядок {i} " + "x" * 80 + "\n" for i in range(150)), encoding="utf-8")
        (att / "small.txt").write_text("коротко\n", encoding="utf-8")
        st = FakeState()
        # ~13.4k символів при частині 4000 → 4 частини: модель читає першу, решту (3) дочитуємо замість її «читаю далі»
        cl = Client([call("read_file", path="big.txt"), {"content": "читаю далі…"}, {"content": "читаю далі…"},
                     {"content": "читаю далі…"}, {"content": "Готово: 150 рядків."}])
        tctx_att = ToolContext(root=att, permissions={"edits": "off", "shell": "off"}, attachments_dir=att)
        out = run(st, cl, [{"role": "system", "content": "s"}, {"role": "user", "content": "підсумуй big.txt"}],
                  tctx_att, floor=8)
        steps = [t for k, _, t in st.notes if k == "step"]
        assert len(steps) == 4 and all("дайджест прочитаного дотепер" in s for s in steps), [s[:60] for s in steps]
        assert "лишилось" in steps[0] and "лишилось" not in steps[-1]                      # дочитано до кінця
        assert out == "Готово: 150 рядків.", out
        st = FakeState()
        cl = Client([call("read_file", path="small"), {"content": "Там написано «коротко»."}])
        assert run(st, cl, [{"role": "user", "content": "що в small?"}], tctx_att) == "Там написано «коротко»."
        assert "коротко" in st.notes[0][2] and "дайджест" not in st.notes[0][2]
        limits.FORCE = base

        # 4) модель нічого не сказала (лише думала) → синтез із нагадуванням дати відповідь
        st = FakeState()
        cl = Client([{"thinking": "м-м-м", "content": ""}, {"thinking": "м-м-м", "content": ""},
                     {"thinking": "ок", "content": "Відповідь після нагадування."}])
        out = run(st, cl, [{"role": "user", "content": "питання"}], tctx)
        assert out == "Відповідь після нагадування." and st.notes[-1][0] == "answer", (out, st.notes)

        # 5) стоп користувача між викликами — цикл зупиняється, відповіді-заглушки нема
        st = FakeState("sid-stop")
        ev = threading.Event()
        W._STOP_EVENTS["sid-stop"] = ev

        class StopClient(Client):
            def chat_stream(self, *a, **k):
                ev.set()                                                # «Стоп» натиснуто під час стріму
                yield from super().chat_stream(*a, **k)

        cl = StopClient([call("read_file", path="app.py"), {"content": "не мало дійти"}])
        out = run(st, cl, [{"role": "user", "content": "x"}], tctx)
        assert out == "" and len(cl.calls) == 1 and st.notes[-1] == ("note", "⛔ Зупинено.", ""), (out, st.notes)
        W._STOP_EVENTS.pop("sid-stop", None)

        # 6) поріг читань рахується з розміру вкладень, без виклику моделі
        assert W.State._read_floor("", ["a"]) == 0 and W.State._read_floor("cid", []) == 0
    finally:
        W.convo.update_digest = real_digest
        limits.FORCE = None
    print("OK: read_loop — чат/аналіз на спільному ядрі: стрім із роздумами, слабка модель зі синтезом, лише тули читання, "
          "дочитування великого вкладення з дайджестом, порожня відповідь → синтез, стоп")


if __name__ == "__main__":
    main()
