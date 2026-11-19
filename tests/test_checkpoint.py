"""Проміжна перевірка планувальником кожні CHECKPOINT_EVERY дій посеред кроку."""
import asyncio
import types

import lca_web.lca_web as W
from agent import limits
from agent.agent_loop import CHECKPOINT_EVERY, is_incomplete
from agent.stepreview import Decision, build_review_prompt

limits.FORCE = limits.REMOTE


class FakeState:
    def __init__(self):
        self.current_id = "sid-cp"
        self.n = 0
        self._step_touched, self._step_actions = [], []

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
        pass

    async def _dispatch_tool(self, reg, name, args, ctx):
        self.n += 1
        return "rc=0 унікальний результат " + "abcdefghijklmnopqrstuvwxyz"[self.n % 26] * self.n


class Client:
    last_stats: dict = {}

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.calls.append([dict(m) for m in messages])
        return {"content": "", "tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": f"echo {len(self.calls)}"}}}]}


def run(decisions):
    st, cl, seen = FakeState(), Client(), []

    async def cp(acts):
        seen.append(len(acts))
        return decisions[len(seen) - 1] if len(seen) <= len(decisions) else Decision("revise", "міняю підхід")

    ctx = types.SimpleNamespace(client=cl)
    final, log = asyncio.run(types.MethodType(W.State._run_tool_step, st)("крок", ctx, title="t", context="c", checkpoint=cp))
    return final, seen, cl


def main() -> None:
    # continue → виконавець бачить підказку й працює далі; на другій перевірці планувальник перериває
    final, seen, cl = run([Decision("continue", "тримай курс")])
    assert seen == [CHECKPOINT_EVERY, 2 * CHECKPOINT_EVERY], seen
    assert final.startswith("крок перервано планувальником") and is_incomplete(final) and "міняю підхід" in final
    assert any("тримай курс" in m["content"] for m in cl.calls[-1] if m["role"] == "user")
    # без checkpoint — як раніше (ліміт ітерацій), перевірок нема
    assert "ТРИВАЄ" in build_review_prompt("план", types.SimpleNamespace(id=1, description="d"), None, "", "факти")
    limits.FORCE = None
    print("OK: checkpoint — звіт кожні %d дій, continue/revise" % CHECKPOINT_EVERY)


if __name__ == "__main__":
    main()
