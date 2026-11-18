"""Стрім відповіді/роздумів у UI: стан оновлюється не на кожен токен (throttle), але нічого не губиться.
Реальний State._run_stream на заглушці стану й штучному генераторі."""
import asyncio
import threading
import time
import types

import lca_web.lca_web as W


class FakeState:
    def __init__(self):
        self.streaming = False
        self.stopping = False
        self.status = ""
        self.stream_content = ""
        self.stream_thinking = ""
        self.tok_out = 0
        self.tok_prompt = 0
        self.tok_eval_ns = 0
        self.tok_tps = 0.0
        self.tok_model = ""
        self.current_id = "sid-stream"
        self.flushes = 0                     # скільки разів стрім ліз у стан (async with self)
        self.snapshots: list[str] = []

    async def __aenter__(self):
        self.flushes += 1
        return self

    async def __aexit__(self, *a):
        self.snapshots.append(self.stream_content)
        return False

    def _recompute_tps(self):
        pass


def gen(n, delay=0.0, thinking_every=0):
    for i in range(n):
        if delay:
            time.sleep(delay)
        yield {"done": False, "content": f"т{i} " if not (thinking_every and i % thinking_every == 0) else "",
               "thinking": f"д{i} " if thinking_every and i % thinking_every == 0 else ""}
    yield {"done": True, "stats": {"prompt": 10, "out": n, "eval_ns": 5}, "tool_calls": None}


def run(st, g):
    fn = types.MethodType(W.State._run_stream, st)
    return asyncio.run(fn(g))


def main() -> None:
    # 1) 300 токенів за ~0.9 с: оновлень стану ≈ 0.9/0.2 + службові, а не 300; увесь текст дійшов
    st = FakeState()
    t0 = time.time()
    body, thinking, tc = run(st, gen(300, delay=0.003))
    took = time.time() - t0
    assert body == "".join(f"т{i} " for i in range(300)).strip(), "текст відповіді втрачено"
    assert st.stream_content.strip() == body and st.tok_out == 300 and st.tok_prompt == 10
    assert st.flushes < 40, st.flushes
    assert st.flushes < 300 / 5, (st.flushes, took)                     # у рази менше, ніж токенів
    # знімки лише ростуть (жодних відкатів/дублів)
    sizes = [len(s) for s in st.snapshots]
    assert sizes == sorted(sizes)

    # 2) роздуми теж докочуються до кінця (останній хвіст не губиться throttle'ом)
    st = FakeState()
    body, thinking, _ = run(st, gen(120, delay=0.002, thinking_every=2))
    assert thinking == "".join(f"д{i} " for i in range(0, 120, 2)), "роздуми обрізано"
    assert body == "".join(f"т{i} " for i in range(120) if i % 2).strip()

    # 2б) у UI під час стріму — лише хвіст роздумів (дешевий рендер), а повертається й зберігається ПОВНИЙ текст
    st = FakeState()
    st.think_snaps = []
    orig_exit = FakeState.__aexit__

    async def exit_rec(self, *a):
        self.think_snaps.append(len(self.stream_thinking))
        return await orig_exit(self, *a)

    FakeState.__aexit__ = exit_rec
    try:
        big = ({"done": False, "content": "", "thinking": "р" * 100} for _ in range(150))
        full = "р" * 100 * 150
        body, thinking, _ = run(st, (e for e in list(big) + [{"done": True, "stats": None, "tool_calls": None}]))
    finally:
        FakeState.__aexit__ = orig_exit
    assert thinking == full and st.stream_thinking == full                      # у кінці — повні роздуми
    assert max(st.think_snaps[:-1]) <= W.STREAM_THINK_TAIL, st.think_snaps       # під час стріму — лише хвіст

    # 3) стоп посеред стріму: те, що встигло накопичитись, повертається (partial), навіть якщо не дійшло до UI
    st = FakeState()
    ev = threading.Event()
    W._STOP_EVENTS[st.current_id] = ev

    def stopping_gen():
        for i in range(50):
            if i == 25:
                ev.set()
            yield {"done": False, "content": f"x{i} ", "thinking": ""}
        yield {"done": True, "stats": None, "tool_calls": None}

    body, _, _ = run(st, stopping_gen())
    assert body.startswith("x0 x1 ") and 20 <= len(body.split()) <= 27, body
    assert st.streaming is False
    W._STOP_EVENTS.pop(st.current_id, None)
    print("OK: stream_throttle — оновлення стану рідко, текст/роздуми цілі, стоп повертає накопичене")


if __name__ == "__main__":
    main()
