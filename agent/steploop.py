"""Єдине ядро циклу «модель → інструменти» для ОДНОГО кроку.

Раніше цей цикл жив у трьох копіях (agent_loop.run_step, lca_web._run_tool_step, subagent.investigate), і кожну
зміну (читання без дій, стиснення, підсумок, примусовий висновок, перевірки планувальника) доводилось вносити по
кілька разів. Тепер логіка одна — тут; відмінності середовищ винесено в LoopHooks:

  * UI (lca_web)     — async-диспетчер із підтвердженнями shell, картки активності, коментарі користувача по ходу,
                       проміжні перевірки планувальника, нотатки про стиснення в чаті;
  * headless         — agent_loop.run_step (скрипти й тести): типові хуки без побічних ефектів;
  * субагент         — subagent.investigate: лише читання, свій бюджет, висновок замість «невдачі».

Ядро асинхронне; блокуюче (виклик моделі, підсумок) іде через hooks.run → to_thread. Синхронні обгортки
користуються run_sync().
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Callable

from . import config, limits, logs
from .agent_loop import (CHECKPOINT_EVERY, COMPACT_HEAD, COMPACT_KEEP, COMPACT_RATIO, ITER_LIMIT_MSG, NUDGE_TEXT,
                         READ_NUDGE, SUMMARIZE_RATIO, ReadStreak, _args, compact_tool_results, finalize,
                         pending_continuation, recover_tool_calls, should_nudge, summarize_history)
from .loopguard import CHECKPOINT_PREFIX, CONTEXT_FULL_PREFIX, WARN_TEXT, LoopGuard, context_full

STOPPED_MSG = "зупинено користувачем"


@dataclass
class LoopConfig:
    max_iters: int
    nudge: bool = True                 # дочитування великих вкладень і підштовхування моделі (виконавець)
    summarize: bool = True             # підсумок від моделі з SUMMARIZE_RATIO вікна
    finalize_on_stuck: bool = False    # зациклення → висновок зі зібраного (субагент), а не «крок зупинено»
    compact_keep: int = COMPACT_KEEP
    compact_head: int = COMPACT_HEAD


@dataclass
class LoopResult:
    final: str
    actions: list = field(default_factory=list)     # [(тул, args, результат)] у порядку виконання
    stats: list = field(default_factory=list)       # client.last_stats кожного виклику моделі


class LoopHooks:
    """Шар між ядром і середовищем. Типові методи — без побічних ефектів (headless)."""

    def __init__(self, stop_event=None, on_tool: Callable | None = None):
        self.stop_event = stop_event
        self.on_tool = on_tool

    def should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    async def run(self, fn, *args):
        """Блокуюча робота (виклик моделі, підсумок) — поза циклом подій."""
        return await asyncio.to_thread(fn, *args)

    async def dispatch(self, registry, name: str, args: dict, ctx) -> str:
        return await self.run(lambda: registry.dispatch(name, args, ctx))

    def live_messages(self) -> list[str]:
        """Нові повідомлення користувача, написані ПІД ЧАС виконання (UI)."""
        return []

    async def before_llm(self, messages: list):
        return None

    async def after_llm(self, token, stats: dict, n_calls: int) -> None:
        pass

    async def after_tool(self, name: str, args: dict, result: str) -> None:
        if self.on_tool:
            self.on_tool(name, args, result)

    async def guard_warning(self, reason: str) -> None:
        pass

    async def summarize(self, messages: list, client, window: int):
        """Підсумок від моделі замість середини історії -> (стиснуто?, символів до, після)."""
        done, b4, af = await self.run(summarize_history, messages, client, window)
        if done:
            logs.get("agent").info("історію кроку стиснуто підсумком: %d → %d символів", b4, af)
            if self.on_tool:
                self.on_tool("compress", {}, f"історію кроку стиснуто підсумком: {b4} → {af} символів")
        return done, b4, af

    async def checkpoint(self, actions: list):
        """Проміжний звіт планувальнику -> Decision | None (None = перевірок немає)."""
        return None


async def run_loop(messages: list, *, registry, ctx, client, hooks: LoopHooks, cfg: LoopConfig) -> LoopResult:
    """Крутити цикл, поки модель не завершить крок / не спрацює запобіжник. `messages` змінюється на місці."""
    guard, reads = LoopGuard(), ReadStreak()
    actions: list[tuple[str, dict, str]] = []
    stats: list[dict] = []
    final, nudged, since_cp = ITER_LIMIT_MSG, False, 0
    log = logs.get("agent")

    async def conclude(compact_first: bool = False) -> str:
        if compact_first:
            compact_tool_results(messages, keep_last=2, head=300)
        return await hooks.run(finalize, messages, client)

    for _ in range(cfg.max_iters):
        if hooks.should_stop():
            final = STOPPED_MSG
            break
        for text in hooks.live_messages():
            messages.append({"role": "user", "content":
                             "Коментар користувача, написаний ПІД ЧАС виконання (врахуй, якщо стосується цього "
                             "кроку; планувальник також його побачить і скоригує план):\n" + text})
        token = await hooks.before_llm(messages)
        msg = await hooks.run(lambda: client.chat(messages, tools=registry.schema(), profile=config.EXECUTOR))
        ls = dict(getattr(client, "last_stats", None) or {})
        if ls:
            stats.append(ls)
        await hooks.after_llm(token, ls, len(msg.get("tool_calls") or []))
        calls, recovered = recover_tool_calls(msg)
        if not calls:
            content = (msg.get("content") or "").strip()
            if not cfg.nudge:
                final = content
                break
            # Великий файл дочитується частинами — якщо модель не продовжила пагінацію сама, дочитуємо БЕЗ її участі
            # (текстовий nudge ненадійний: бачили 15+ ідентичних повторів). Див. pending_continuation.
            cont = pending_continuation(messages)
            if cont is not None:
                calls, recovered = [cont], True
            # Підштовхуємо РІВНО ОДИН раз (модель почитала файл і видала порожню відповідь); зламаний/порожній текст
            # НЕ повертаємо моделі як assistant — вона копіює власну попередню відповідь. Див. should_nudge.
            elif should_nudge(content, nudged):
                nudged = True
                log.info("nudge: порожня/просочена відповідь без tool_calls")
                messages.append({"role": "user", "content": NUDGE_TEXT})
                continue
            else:
                final = content
                break
        # recovered=True: тул викликано з розпарсеного просоченого тексту — сам зламаний текст не ехо-имо
        messages.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""), "tool_calls": calls})
        for call in calls:
            name = call.get("function", {}).get("name", "")
            args = _args(call)
            result = await hooks.dispatch(registry, name, args, ctx)
            actions.append((name, args, result))
            messages.append({"role": "tool", "content": result})
            guard.observe(name, args, result)
            reads.observe(name)
            await hooks.after_tool(name, args, result)
        verdict = guard.check()
        if verdict:
            kind, reason = verdict
            log.warning("зациклення (%s): %s | останній результат: %s", kind, reason,
                        guard.last_result[:120].replace(chr(10), " "))
            if kind == "stop":
                final = "" if cfg.finalize_on_stuck else guard.stuck_message(reason)
                break
            messages.append({"role": "user", "content": WARN_TEXT.format(reason=reason, last=guard.last_result[:300])})
            await hooks.guard_warning(reason)
        rv = reads.verdict()
        if rv == "force":
            out = await conclude()
            if out:
                final = out
                break
        elif rv == "warn":
            messages.append({"role": "user", "content": READ_NUDGE})
        since_cp += len(calls)
        if since_cp >= CHECKPOINT_EVERY:                       # проміжний звіт планувальнику (лише UI)
            since_cp = 0
            dec = await hooks.checkpoint(list(actions))
            if dec is not None:
                if dec.action != "continue":
                    final = f"{CHECKPOINT_PREFIX}: {dec.comment or 'планувальник змінює підхід'}"
                    break
                if dec.comment:
                    messages.append({"role": "user", "content":
                                     "Планувальник переглянув виконані дії й каже продовжувати: " + dec.comment})
        ls = dict(getattr(client, "last_stats", None) or {})
        win = limits.get().context_tokens
        if ls and win and ls.get("prompt", 0) >= COMPACT_RATIO * win:
            freed = compact_tool_results(messages, keep_last=cfg.compact_keep, head=cfg.compact_head)   # дешево
            if cfg.summarize and ls.get("prompt", 0) >= SUMMARIZE_RATIO * win:                         # не вистачило
                done, _b4, _af = await hooks.summarize(messages, client, win)
                if done:
                    continue
            if freed and context_full(ls.get("prompt", 0), win):
                continue                                       # стиснули — даємо кроку шанс завершитись
        if ls and context_full(ls.get("prompt", 0), limits.get().context_tokens):
            log.warning("контекст майже заповнений: %s токенів", ls.get("prompt"))
            if actions and not reads.acted:                    # лише читали — висновок із зібраного (після стиснення)
                out = await conclude(compact_first=True)
                if out:
                    final = out
                    break
            final = f"{CONTEXT_FULL_PREFIX} ({ls.get('prompt')} токенів). Розбийте задачу на менші кроки."
            break
    else:
        log.warning("крок досяг ліміту ітерацій (%d)", cfg.max_iters)
        if actions and not reads.acted:                        # лише читали: замість «невдачі» — висновок зі зібраного
            out = await conclude()
            if out:
                final = out
    return LoopResult(final=final, actions=actions, stats=stats)


def run_sync(coro):
    """Виконати корутину з синхронного коду. Якщо в цьому потоці вже крутиться цикл подій — у окремому потоці."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def target():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as e:      # noqa: BLE001 — перекидаємо в потік-виклик
            box["e"] = e
    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]
