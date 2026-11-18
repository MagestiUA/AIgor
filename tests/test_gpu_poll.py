"""State.poll_gpu: дані GPU йдуть у браузер ПОДІЄЮ-скриптом (а не через стан — інакше кожне опитування
перемальовувало б інтерфейс), ключ береться з середовища, вимкнено → вихід без запитів, один цикл на процес."""
import asyncio
import json
import os
import types

import lca_web.lca_web as W
from agent import backend, config, gpumon

OK = {"state": "ok", "msg": "", "ms": 120, "gpu_name": "H100", "util": 55.0, "vram_pct": 90.0, "vram_used_mb": 73728,
      "vram_total_mb": 81559, "temp": 40, "power": 90, "power_limit": 350, "running": 1, "kv_pct": 3.0,
      "series": [0, 50, None, 100]}
PREFIX = "window.__igorGpu&&window.__igorGpu("


class FakeState:
    """Свідомо БЕЗ полів gpu/gpu_bars: якщо цикл спробує записати показники в стан — тест упаде."""
    def __init__(self, enabled=True, port="8001"):
        self.gpu_enabled, self.gpu_port = enabled, port

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def payload(ev) -> dict:
    js = ev.args[0][1]._var_value
    assert js.startswith(PREFIX) and js.endswith(")"), js[:60]
    return json.loads(js[len(PREFIX):-1])


async def collect(st, timeout):
    got = []

    async def run():
        async for ev in types.MethodType(W.State.poll_gpu.fn, st)():
            got.append(ev)
    try:
        await asyncio.wait_for(run(), timeout)
    except asyncio.TimeoutError:
        pass
    return got


def main() -> None:
    real = (gpumon.fetch, backend.resolve, W.GPU_POLL_S, os.environ.get(config.REMOTE_KEY_ENV))
    calls = []
    try:
        W.GPU_POLL_S = 0.05
        os.environ[config.REMOTE_KEY_ENV] = "env-key-xyz"
        backend.resolve = lambda s=None: {"backend": "openai", "base_url": "http://srv.example:8000/v1", "model": "m", "api_key": ""}
        gpumon.fetch = lambda base, key, port, **k: (calls.append((base, key, port)), OK)[1]

        evs = asyncio.run(collect(FakeState(port="9001"), 0.3))
        assert len(evs) >= 3 and len(calls) >= 3
        assert calls[0] == ("http://srv.example:8000/v1", "env-key-xyz", "9001"), calls[:1]        # ключ — з env
        p = payload(evs[0])
        assert p["v"]["state"] == "ok" and p["v"]["util"] == "55%" and p["span"] == gpumon.HISTORY_S, p
        assert p["series"] == [0, 50, None, 100]                                                    # ряд із розривом (null)

        # вимкнено: жодних запитів і подій, цикл одразу завершується (а не висить)
        calls.clear()
        evs = asyncio.run(asyncio.wait_for(collect(FakeState(enabled=False), 1.0), 2.0))
        assert calls == [] and evs == []

        # не серверний бекенд: пояснення замість запиту
        backend.resolve = lambda s=None: {"backend": "ollama", "base_url": "", "model": "", "api_key": ""}
        evs = asyncio.run(collect(FakeState(), 0.15))
        assert calls == [] and payload(evs[0])["v"]["state"] == "na" and "OpenAI" in payload(evs[0])["v"]["hint"]

        # другий запуск витісняє перший: старий цикл більше не опитує й не шле подій
        backend.resolve = lambda s=None: {"backend": "openai", "base_url": "http://srv.example:8000/v1", "model": "m", "api_key": "k"}

        async def two():
            ea, eb = [], []

            async def consume(st, out):
                async for ev in types.MethodType(W.State.poll_gpu.fn, st)():
                    out.append(ev)
            ta = asyncio.create_task(consume(FakeState(), ea))
            await asyncio.sleep(0.12)
            tb = asyncio.create_task(consume(FakeState(), eb))
            await asyncio.sleep(0.3)
            done_a, a_n = ta.done(), len(ea)
            await asyncio.sleep(0.2)
            tb.cancel()
            try:
                await tb
            except asyncio.CancelledError:
                pass
            return done_a, a_n, len(ea), len(eb)
        done_a, a1, a2, nb = asyncio.run(two())
        assert done_a and a1 == a2 and nb >= 4, (done_a, a1, a2, nb)
        print("OK: gpu_poll — подіями-скриптом без стану, ключ із середовища, вимкнено без запитів, один цикл")
    finally:
        gpumon.fetch, backend.resolve, W.GPU_POLL_S, k = real
        if k is None:
            os.environ.pop(config.REMOTE_KEY_ENV, None)
        else:
            os.environ[config.REMOTE_KEY_ENV] = k


if __name__ == "__main__":
    main()
