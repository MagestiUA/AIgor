"""State.poll_gpu: ключ береться з середовища (як для моделі), вимкнено → вихід і без запитів, один цикл на процес."""
import asyncio
import os
import types

import lca_web.lca_web as W
from agent import backend, config, gpumon

OK = {"state": "ok", "msg": "", "ms": 120, "gpu_name": "H100", "util": 55.0, "vram_pct": 90.0, "vram_used_mb": 73728,
      "vram_total_mb": 81559, "temp": 40, "power": 90, "power_limit": 350, "running": 1, "kv_pct": 3.0, "hist": [0, 50, 100]}


class FakeState:
    def __init__(self, enabled=True, port="8001"):
        self.gpu_enabled, self.gpu_port = enabled, port
        self.gpu, self.gpu_bars = {}, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def main() -> None:
    real = (gpumon.fetch, backend.resolve, W.GPU_POLL_S, os.environ.get(config.REMOTE_KEY_ENV))
    calls = []
    try:
        W.GPU_POLL_S = 0.05
        os.environ[config.REMOTE_KEY_ENV] = "env-key-xyz"
        backend.resolve = lambda s=None: {"backend": "openai", "base_url": "http://srv.example:8000/v1", "model": "m", "api_key": ""}
        gpumon.fetch = lambda base, key, port, **k: (calls.append((base, key, port)), OK)[1]
        poll = W.State.poll_gpu.fn

        async def run(st, t):
            try:
                await asyncio.wait_for(types.MethodType(poll, st)(), t)
            except asyncio.TimeoutError:
                pass

        st = FakeState(port="9001")
        asyncio.run(run(st, 0.3))
        assert len(calls) >= 3 and calls[0] == ("http://srv.example:8000/v1", "env-key-xyz", "9001"), calls[:1]   # ключ — з env
        assert st.gpu["state"] == "ok" and st.gpu["util"] == "55%" and st.gpu_bars == ["2%", "50%", "100%"], (st.gpu, st.gpu_bars)

        # вимкнено: жодних запитів, цикл одразу завершується (а не висить)
        calls.clear()
        st = FakeState(enabled=False)
        asyncio.run(asyncio.wait_for(types.MethodType(poll, st)(), 1.0))
        assert calls == []

        # не серверний бекенд: пояснення замість запиту
        backend.resolve = lambda s=None: {"backend": "ollama", "base_url": "", "model": "", "api_key": ""}
        st = FakeState()
        asyncio.run(run(st, 0.15))
        assert calls == [] and st.gpu["state"] == "na" and "OpenAI" in st.gpu["hint"], st.gpu

        # другий запуск витісняє перший: старий цикл більше не опитує
        backend.resolve = lambda s=None: {"backend": "openai", "base_url": "http://srv.example:8000/v1", "model": "m", "api_key": "k"}
        a, b = FakeState(), FakeState()

        async def two():
            ta = asyncio.create_task(types.MethodType(poll, a)())
            await asyncio.sleep(0.12)
            tb = asyncio.create_task(types.MethodType(poll, b)())
            await asyncio.sleep(0.3)
            done_a = ta.done()
            tb.cancel()
            try:
                await tb
            except asyncio.CancelledError:
                pass
            return done_a
        assert asyncio.run(two())
        print("OK: gpu_poll — ключ із середовища, вимкнено без запитів, не-серверний бекенд, один цикл")
    finally:
        gpumon.fetch, backend.resolve, W.GPU_POLL_S, k = real
        if k is None:
            os.environ.pop(config.REMOTE_KEY_ENV, None)
        else:
            os.environ[config.REMOTE_KEY_ENV] = k


if __name__ == "__main__":
    main()
