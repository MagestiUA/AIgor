"""Агент без UI: той самий продакшн-шлях (планувальник → PlanRunner → кроки на steploop), що й у застосунку.

Для прогонів реальних задач із метриками (scripts/run_task.py) і для тестів: замість Reflex-стану — HeadlessUI, що пише
події в консоль/список. Лічильник викликів моделі — CountingClient (обгортка над будь-яким клієнтом).
"""
from __future__ import annotations

import asyncio
import time

from . import activity, limits, orchestration as orch
from .plan_runner import StepRun
from .steploop import LoopConfig, LoopHooks, run_loop
from .toolkit import default_registry


class CountingClient:
    """Обгортка клієнта моделі: рахує виклики, токени й час (для метрик прогону). Інтерфейс — як у клієнта."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.prompt = self.out = 0
        self.seconds = 0.0

    @property
    def last_stats(self) -> dict:
        return getattr(self.inner, "last_stats", {}) or {}

    def _account(self, t0: float) -> None:
        s = self.last_stats
        self.calls += 1
        self.prompt += int(s.get("prompt", 0) or 0)
        self.out += int(s.get("out", 0) or 0)
        self.seconds += time.monotonic() - t0

    def chat(self, *a, **k):
        t0 = time.monotonic()
        try:
            return self.inner.chat(*a, **k)
        finally:
            self._account(t0)

    def chat_stream(self, *a, **k):
        t0 = time.monotonic()
        try:
            yield from self.inner.chat_stream(*a, **k)
        finally:
            self._account(t0)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _Hooks(LoopHooks):
    def __init__(self, ui: "HeadlessUI", checkpoint=None):
        super().__init__(stop_event=ui.stop_event, on_tool=ui._on_tool)
        self._cp = checkpoint

    async def checkpoint(self, actions):
        return await self._cp(actions) if self._cp else None


class HeadlessUI:
    """PlanUI для запуску без інтерфейсу: події — у echo (типово print) і в self.events."""

    def __init__(self, ctx, client, registry=None, stop_event=None, echo=print):
        self.ctx, self.client = ctx, client
        self.registry = registry or default_registry()
        self.stop_event = stop_event
        self.echo = echo or (lambda *_: None)
        self.pending = None
        self.events: list[tuple[str, str]] = []
        self.steps = 0
        self.tool_calls = 0

    def _on_tool(self, name, args, result):
        self.tool_calls += 1
        self.echo(f"    · {name}({activity.summarize_args(name, args, 100)}) → {activity.clip_result(result, 160)}")

    async def status(self, text: str) -> None:
        self.echo(f"… {text}")

    async def say(self, content: str, kind: str = "note") -> None:
        self.events.append((kind, content))
        self.echo(f"[{kind}] {content}")

    async def save_pending(self, plan) -> None:
        self.pending = plan

    def take_live_notes(self) -> list[str]:
        return []

    async def run_step(self, step_text: str, *, title: str, context: str, checkpoint=None) -> StepRun:
        from .agent_loop import SYSTEM, research_report
        user = (f"Контекст:\n{context}\n\n" if context else "") + f"Крок:\n{step_text}"
        user += await asyncio.to_thread(research_report, step_text, context, self.ctx, self.registry)
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        res = await run_loop(messages, registry=self.registry, ctx=self.ctx, client=self.client,
                             hooks=_Hooks(self, checkpoint), cfg=LoopConfig(max_iters=limits.get().hard_iters or 30))
        self.steps += 1
        touched = [p for n, a, r in res.actions if activity.result_ok(r) for p in orch.touched_paths(n, a)]
        self.echo(f"[крок] {title}: {(res.final or '')[:300]}")
        return StepRun(res.final, [f"{n} -> {r[:80]}" for n, _a, r in res.actions], list(dict.fromkeys(touched)),
                       list(res.actions))
