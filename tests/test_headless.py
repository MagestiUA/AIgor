"""Агент без UI (agent.headless): той самий шлях, що в застосунку — PlanRunner + кроки на steploop + реальні тули,
модель — скрипт. Перевіряємо: план виконується, файли змінено, лічильник викликів/токенів, підсумок і «Готово»."""
import asyncio
import json
import tempfile
from pathlib import Path

from agent import limits, projectmap
from agent.headless import CountingClient, HeadlessUI
from agent.memory import TaskState
from agent.plan_runner import PlanEnv, PlanRunner
from agent.toolkit import ToolContext


class Script:
    """Модель-скрипт: виконавець пише файл/читає його, рецензент каже «далі», звірка — ок, підсумок — текст."""
    def __init__(self):
        self.last_stats = {"prompt": 50, "out": 5}

    def chat(self, messages, tools=None, profile=None, fmt=None):
        sys_ = messages[0]["content"]
        last = messages[-1]
        if tools:                                                    # виконавець кроку
            if last["role"] == "tool":
                return {"content": "зроблено", "tool_calls": []}
            if "крок 1" in last["content"].rsplit("Крок:\n", 1)[-1]:            # сам крок (контекст містить увесь план)
                return {"content": "", "tool_calls": [{"function": {"name": "write_file",
                        "arguments": {"path": "out/a.py", "content": "A = 1\n"}}}]}
            return {"content": "", "tool_calls": [{"function": {"name": "replace_in_file",
                    "arguments": {"path": "out/a.py", "old": "A = 1", "new": "A = 2"}}}]}
        if "PLANNER" in sys_:
            return {"content": json.dumps({"action": "continue", "comment": ""})}
        if "review an executed plan" in sys_:
            return {"content": json.dumps({"ok": True})}
        return {"content": "- створено out/a.py, A = 2"}               # підсумок виконання


def main() -> None:
    real = projectmap.STORE_DIR, limits.FORCE
    projectmap.STORE_DIR = Path(tempfile.mkdtemp()) / "projects"
    limits.FORCE = limits.scaled(limits.REMOTE, 65536)               # як у застосунку локально (без милиць)
    try:
        root = Path(tempfile.mkdtemp())
        client = CountingClient(Script())
        events = []
        ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"}, client=client, track_project=True)
        ui = HeadlessUI(ctx, client, echo=events.append)
        plan = TaskState(task="створити a.py і змінити A")
        plan.add_step("act", "крок 1: створити out/a.py")
        plan.add_step("act", "крок 2: змінити A на 2")
        out = asyncio.run(PlanRunner(ui, client, PlanEnv(root=str(root))).execute(plan))
        assert (root / "out" / "a.py").read_text(encoding="utf-8") == "A = 2\n"
        assert [s.status for s in plan.steps] == ["done", "done"] and ui.pending is None, out
        assert ui.steps == 2 and ui.tool_calls == 2 and client.calls >= 6 and client.prompt == 50 * client.calls
        assert ("answer", "**Підсумок виконання**\n\n- створено out/a.py, A = 2") in ui.events
        assert ui.events[-1] == ("note", "Готово ✓") and "Звірка: ок" in out
        assert any("write_file" in e for e in events) and any("replace_in_file" in e for e in events)
    finally:
        projectmap.STORE_DIR, limits.FORCE = real
    print("OK: headless — PlanRunner + HeadlessUI без UI: кроки з реальними тулами, звірка, підсумок, лічильник викликів")


if __name__ == "__main__":
    main()
