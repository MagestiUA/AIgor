"""Періодична тиха перевірка оновлень (State.poll_updates / _do_update_check): цикл, один на процес,
закритий банер не вертається щоразу, під час завантаження не заважає."""
import asyncio
import types

import lca_web.state as W
from agent import updater


class FakeState:
    def __init__(self):
        self.update_busy = False
        self.update_msg, self.update_ok = "", True
        self.update_available, self.update_latest, self.update_notes = False, "", ""
        self.update_dismissed = False
        self.app_ver = ""
        self._update_info = {}
        self.checks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def newer(v):
    return {"ok": True, "current": "b_0.0.20", "latest": v, "newer": True, "exe_url": "u", "sha_url": "s",
            "notes": "н", "error": ""}


def main() -> None:
    real_check, real_poll = updater.check, W.UPDATE_POLL_S
    try:
        # 1) цикл справді періодичний (кілька перевірок за короткий час), і зупиняється зовні (скасування)
        W.UPDATE_POLL_S = 0.05
        st = FakeState()

        async def count(silent):
            st.checks += 1
            assert silent is True                               # періодична — завжди тиха
        st._do_update_check = count
        poll = types.MethodType(W.State.poll_updates.fn, st)

        async def run1():
            try:
                await asyncio.wait_for(poll(), 0.42)
            except asyncio.TimeoutError:
                pass
        asyncio.run(run1())
        assert 4 <= st.checks <= 9, st.checks

        # 2) другий on_load витісняє перший цикл: працює рівно один
        a, b = FakeState(), FakeState()
        for s in (a, b):
            async def c(silent, s=s):
                s.checks += 1
            s._do_update_check = c

        async def run2():
            ta = asyncio.create_task(types.MethodType(W.State.poll_updates.fn, a)())
            await asyncio.sleep(0.12)
            tb = asyncio.create_task(types.MethodType(W.State.poll_updates.fn, b)())
            await asyncio.sleep(0.3)
            a_done = ta.done()
            a_before = a.checks
            await asyncio.sleep(0.2)
            a_after = a.checks
            tb.cancel()
            try:
                await tb
            except asyncio.CancelledError:
                pass
            return a_done, a_before, a_after, b.checks
        a_done, a_before, a_after, b_checks = asyncio.run(run2())
        assert a_done and a_after == a_before, (a_done, a_before, a_after)       # старий вийшов і мовчить
        assert b_checks >= 4, b_checks                                              # новий працює

        # 3) _do_update_check: знайшли → банер; закрили → не вертається на ту саму версію; нова версія → знову
        W.UPDATE_POLL_S = real_poll
        st = FakeState()
        calls = []
        updater.check = lambda cur, timeout=0: (calls.append(timeout), newer("b_0.0.21"))[1]
        do = types.MethodType(W.State._do_update_check, st)
        asyncio.run(do(True))
        assert st.update_available and st.update_latest == "b_0.0.21" and not st.update_dismissed and st.app_ver
        st.update_dismissed = True                                                   # користувач закрив банер
        asyncio.run(do(True))
        assert st.update_dismissed, "закритий банер не має вертатись щодві хвилини"
        updater.check = lambda cur, timeout=0: newer("b_0.0.22")
        asyncio.run(do(True))
        assert st.update_latest == "b_0.0.22" and not st.update_dismissed            # вийшла ще новіша — повідомляємо знову
        assert st._update_info["latest"] == "b_0.0.22"

        # 4) тиха перевірка без нових версій/при збої нічого не пише; під час завантаження (busy) — пропускає
        st = FakeState()
        updater.check = lambda cur, timeout=0: {"ok": False, "newer": False, "latest": "", "error": "мережа", "notes": ""}
        asyncio.run(types.MethodType(W.State._do_update_check, st)(True))
        assert st.update_msg == "" and not st.update_available
        st.update_busy = True
        updater.check = lambda cur, timeout=0: (_ for _ in ()).throw(AssertionError("не мало б питати під час завантаження"))
        asyncio.run(types.MethodType(W.State._do_update_check, st)(True))
        print("OK: update_poll — періодичний цикл, один на процес, закритий банер не вертається, busy пропускається")
    finally:
        updater.check, W.UPDATE_POLL_S = real_check, real_poll


if __name__ == "__main__":
    main()
