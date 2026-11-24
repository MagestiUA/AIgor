"""Прогін реальної задачі БЕЗ UI тим самим шляхом, що й у застосунку, з метриками.

    python scripts/run_task.py --root D:/proj "додай функцію X у a.py"
    python scripts/run_task.py --root D:/proj --tasks tasks.txt          # кілька задач (по одній у рядку)

Шлях: planner.deliberate(route=True) → (plan) PlanRunner + HeadlessUI → кроки на steploop. Модель — з налаштувань
застосунку (agent/backend.py: локальна Ollama або сервер). Метрики: маршрут, кроки й статуси, виклики моделі, токени,
виклики тулів, час. Правки виконуються одразу (edits=auto), консоль — режим smart (небезпечні команди без
підтвердження ВІДХИЛЯЮТЬСЯ — нікого спитати). Запускайте на копії проєкту.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import backend, planner, projectmap  # noqa: E402
from agent.headless import CountingClient, HeadlessUI  # noqa: E402
from agent.plan_runner import PlanEnv, PlanRunner  # noqa: E402
from agent.project import load_project_doc, scan_structure  # noqa: E402
from agent.toolkit import ToolContext  # noqa: E402


def run_one(root: Path, task: str, quiet: bool = False) -> dict:
    client = CountingClient(backend.make_client())
    echo = (lambda *_: None) if quiet else print
    t0 = time.monotonic()
    pmap = projectmap.context_block(root)
    ctx_text = (pmap + "\n\n" if pmap else "") + scan_structure(root)
    dec = planner.deliberate(task, ctx_text, client, load_project_doc(root), route=True)
    out = {"task": task, "route": dec["action"], "reasoning": dec.get("reasoning", "")}
    echo(f"▶ маршрут: {dec['action']} — {dec.get('reasoning', '')}")
    tctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "smart"}, client=client,
                       confirm=lambda text: False, track_project=True)
    ui = HeadlessUI(tctx, client, echo=echo)
    if dec["action"] == "plan" and dec["state"] and dec["state"].steps:
        plan = dec["state"]
        result = asyncio.run(PlanRunner(ui, client, PlanEnv(root=str(root))).execute(plan))
        out.update(steps=[(s.id, s.status, s.description[:80]) for s in plan.steps],
                   done=all(s.status == "done" for s in plan.steps), pending=ui.pending is not None,
                   result=result[-600:])
    elif dec["action"] == "shell":
        run = asyncio.run(ui.run_step(task, title=task[:60], context=f"Project root: {root}"))
        out.update(final=run.final[-600:], done=bool(run.log))
    else:
        out.update(question=dec.get("question", ""), options=[o["label"] for o in dec.get("options") or []])
    out.update(llm_calls=client.calls, prompt_tokens=client.prompt, out_tokens=client.out, step_runs=ui.steps,
               tool_calls=ui.tool_calls, seconds=round(time.monotonic() - t0, 1), llm_seconds=round(client.seconds, 1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", default="")
    ap.add_argument("--root", required=True)
    ap.add_argument("--tasks", help="файл із задачами, по одній у рядку")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    tasks = [t.strip() for t in Path(a.tasks).read_text(encoding="utf-8").splitlines() if t.strip()] if a.tasks else [a.task]
    results = [run_one(root, t, a.quiet) for t in tasks if t]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r.get("done", r["route"] in ("answer", "clarify", "choose")) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
