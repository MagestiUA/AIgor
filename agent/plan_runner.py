"""Виконання плану код-режиму — ядро агента, БЕЗ UI.

Раніше ця логіка жила в lca_web/state.py (Reflex-стан на ~2700 рядків): виконати кроки, переглянути план після кожного,
звірити результат, скласти підсумок. Через це реальний продакшн-шлях не можна було запустити без інтерфейсу, а тести
прив'язували методи стану до заглушок. Тепер (стандартний поділ «ядро агента ↔ адаптер UI»):
  * PlanRunner — уся логіка: кроки бачать результати попередніх; виконані пропускаються (відновлення з місця
    зупинки); невдалий крок ЗУПИНЯЄ план і лишає його очікуваним; планувальник переглядає звіт після кожного кроку
    (і посеред довгого — checkpoint), враховує коментарі користувача по ходу; наприкінці — звірка (синтаксис змінених .py
    + узгодженість кроків, обмежений повтор) і підсумок виконання;
  * PlanUI — що потрібно від середовища: показати статус і повідомлення, зберегти план як очікуваний, забрати коментарі
    користувача, виконати ОДИН крок (tool-loop). Реалізації: lca_web (UI) і HeadlessUI (скрипти, прогони задач).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from . import limits, orchestration as orch, projectmap, sessionmem
from . import stepreview as review
from .agent_loop import is_incomplete
from .convo import as_context
from .memory import render_for_planner
from .stepreview import check_consistency


@dataclass
class StepRun:
    final: str
    log: list = field(default_factory=list)          # короткий журнал дій ('' — дій не було)
    touched: list = field(default_factory=list)      # файли, які крок змінив
    actions: list = field(default_factory=list)      # [(тул, args, результат)] — для точного звіту планувальнику


class PlanUI(Protocol):
    stop_event: object

    async def status(self, text: str) -> None: ...
    async def say(self, content: str, kind: str = "note") -> None: ...
    async def save_pending(self, plan) -> None: ...
    def take_live_notes(self) -> list[str]: ...
    async def run_step(self, step_text: str, *, title: str, context: str, checkpoint=None) -> StepRun: ...


@dataclass
class PlanEnv:
    """Що бачить кожен крок, окрім самого плану (збирається один раз на старті плану)."""
    root: str
    roots_note: str = ""
    autonomous: bool = False          # «правки: auto» — виправляти, а не лише описувати
    summary: str = ""                 # підсумок розмови
    session_id: str = ""              # для індексу нотаток сесії
    tail: str = ""                    # дослівний хвіст розмови на старті плану
    att_note: str = ""                # перелік вкладень


def step_context(env: PlanEnv, plan, step) -> str:
    """Контекст кроку: загальна задача + ПОВНИЙ план із результатами вже виконаних кроків і змінених файлів
    (render_for_planner) — кроки бачать, що зробили попередні, а не лише галочки; підсумок і хвіст розмови, нотатки,
    вкладення.

    ПОРЯДОК — під кеш префікса (vLLM prefix caching / llama.cpp): спершу стабільне між кроками (тека, режим, карта,
    підсумок, нотатки, хвіст розмови, вкладення), наприкінці — змінне (задача й план із результатами, що оновлюються
    після кожного кроку). Інакше зміна плану змушувала б модель заново обробляти карту й підсумок на кожному кроці."""
    ctx = f"Project root: {env.root}\n{env.roots_note}"
    if env.autonomous:
        ctx = orch.AUTONOMY_NOTE + "\n\n" + ctx
    pmap = projectmap.context_block(env.root)             # свіжа: попередній крок міг змінити файли
    if pmap:
        ctx += "\n\n" + pmap
    if env.summary:
        ctx += f"\n{as_context(env.summary)}"
    notes = sessionmem.index_block(env.session_id) if env.session_id else ""   # лише індекс, зміст — тулом recall
    if notes:
        ctx += "\n\n" + notes
    if env.tail:
        ctx += "\n\n" + env.tail.strip()
    if env.att_note:
        ctx += "\n" + env.att_note
    ctx += (f"\n\nЗагальна задача: {plan.task}\n"
            f"План і результати вже виконаних кроків (ти виконуєш ЛИШЕ крок #{step.id}):\n"
            f"{render_for_planner(plan)}")
    return ctx


def tail_for_plan(tail: str) -> str:
    cap = max(2000, limits.get().summary // 4)
    return tail if len(tail) <= cap else "…" + tail[-cap:]


def _note_facts(plan, notes: list[str]) -> None:
    plan.facts["коментарі користувача"] = " | ".join(
        [x for x in [plan.facts.get("коментарі користувача", "")] if x] + [n[:300] for n in notes])[-600:]


class PlanRunner:
    def __init__(self, ui: PlanUI, client, env: PlanEnv):
        self.ui, self.client, self.env = ui, client, env

    def _stopped(self) -> bool:
        ev = getattr(self.ui, "stop_event", None)
        return ev is not None and ev.is_set()

    async def review(self, plan, step, ok, final: str, may_change: bool, user_notes: list[str] | None = None):
        """Планувальник дивиться на ТОЧНІ факти кроку (команди й вивід) і вирішує: далі / скоригувати / зупинитись.
        Його коротке пояснення виходить у чат («що сталося, що далі»). Збій моделі → просто «далі»."""
        await self.ui.status(f"Планувальник оцінює крок #{step.id}…")
        try:
            dec = await asyncio.to_thread(lambda: review.review_step(
                render_for_planner(plan), step, ok, final, step.report, self.client, user_notes))
        except Exception as e:                                  # noqa: BLE001 — планувальник не критичний
            from . import logs
            logs.get("agent").warning("перегляд плану після кроку #%s не вдався: %s", step.id, e)
            return review.Decision()
        if not may_change and dec.action == "revise":
            dec.action = "continue"
        if dec.comment:
            await self.ui.say(f"💭 {dec.comment}", "note")
        return dec

    async def execute(self, plan) -> str:
        """Виконати кроки плану. -> зріз плану для підсумку розмови (render_for_planner + результат звірки)."""
        stopped, failed, final = False, None, ""
        touched_by_step: dict[int, list[str]] = {}
        total = len(plan.steps)
        done_before = sum(1 for s in plan.steps if s.status == "done")
        if 0 < done_before < total:
            nxt = next(s for s in plan.steps if s.status != "done")
            await self.ui.say(f"▶ Продовжую план з кроку #{nxt.id} (виконано {done_before} з {total}).")
        replans = 0                                            # скільки разів планувальник уже коригував план
        while True:
            step = next((s for s in plan.steps if s.status != "done"), None)   # план може рости/мінятись
            if step is None:
                break
            total = len(plan.steps)
            if self._stopped():                                # перервано між кроками
                stopped = True
                break
            await self.ui.status(f"Крок #{step.id}/{total}: {step.description[:50]}")
            if step.is_noop:
                plan.set_result(step.id, "done", "примітка")
                continue
            cp_holder: dict = {"notes": [], "dec": None}

            async def checkpoint(acts, _step=step, _h=cp_holder):
                """Проміжний звіт планувальнику посеред кроку: точні факти дій на цей момент."""
                live = self.ui.take_live_notes()
                if live:
                    _h["notes"] += live
                    _note_facts(plan, live)
                _step.report = review.facts_report(acts)
                may_cp = bool(live) or replans < review.MAX_REPLANS
                dec = await self.review(plan, _step, None, "", may_cp, live)
                if dec.action == "done":         # посеред кроку «усе вже готово» — хай виконавець завершить крок,
                    dec.action = "continue"      # а пропуск решти вирішимо після кроку (коментар піде виконавцю)
                if dec.action != "continue":
                    _h["dec"] = dec
                return dec

            run = await self.ui.run_step(step.description, title=step.description[:60],
                                         context=step_context(self.env, plan, step),
                                         checkpoint=checkpoint if len(plan.steps) > 1 else None)
            final = run.final
            ok = bool(run.log) and not is_incomplete(final)
            plan.set_result(step.id, "done" if ok else "failed", final or "—")
            step.report = review.facts_report(run.actions)       # точні факти: команда → вивід, файли
            if run.touched:
                touched_by_step[step.id] = list(run.touched)
                plan.facts["змінені файли"] = ", ".join(dict.fromkeys(
                    f for fl in touched_by_step.values() for f in fl))[:500]
            more = any(s.status != "done" for s in plan.steps)
            notes = cp_holder["notes"] + self.ui.take_live_notes()   # коментарі користувача, написані під час кроку
            if cp_holder["notes"]:
                notes = list(dict.fromkeys(notes))
            if notes:
                _note_facts(plan, notes)
            if ((len(plan.steps) > 1 and (more or not ok)) or notes) and not self._stopped():
                may = bool(notes) or replans < review.MAX_REPLANS      # коментар користувача обмеженням не глушимо
                dec = cp_holder["dec"] or await self.review(plan, step, ok, final, may, notes)
                if review.apply_decision(plan, dec, step.id, may):
                    replans += 1
                    if step.status == "pending":               # невдалий крок переписано — пробуємо знову
                        continue
                if (not ok and dec.action in ("continue", "done") and dec.comment and (final or "").strip()
                        and not is_incomplete(final) and not step.report.strip()):
                    # Крок без жодної дії (напр. «виправ помилки, якщо є» — помилок нема): правило «без дій = невдача»
                    # ловить вигадане виконання, але планувальник бачив факти (дій не було) і текст виконавця й прийняв
                    # крок («continue» з поясненням). Ліміт/зациклення/контекст сюди не потрапляють (is_incomplete).
                    plan.set_result(step.id, "done", final)
                    ok = True
                if dec.action == "done" and ok:
                    # Планувальник бачить, що задачу вже виконано (крок зробив і роботу наступних): решту кроків
                    # пропускаємо й завершуємо звичайним шляхом — звірка, підсумок, «Готово». Раніше для цього був лише
                    # «stop», і план зависав як незавершений із кнопкою «Виконати план».
                    skipped = [s for s in plan.steps if s.status != "done"]
                    for s in skipped:
                        plan.set_result(s.id, "done", "⏭ пропущено: задачу вже виконано попередніми кроками")
                    if skipped:
                        await self.ui.say("⏭ Пропускаю кроки " + ", ".join(f"#{s.id}" for s in skipped)
                                          + " — задачу вже виконано попередніми кроками.")
                    break
                if dec.action == "stop" and ok:
                    await self.ui.save_pending(plan)
                    return render_for_planner(plan)
            if not ok:
                failed = step
                break

        if failed is not None:
            await self.ui.say(f"⛔ Крок #{failed.id} не завершено: {(final or '—')[:240]}\n\nРешту кроків призупинено, "
                              "щоб не будувати на зламаному. Натисніть «Виконати» — продовжу з цього кроку, "
                              "або «Відхилити».")
            await self.ui.save_pending(plan)
            return render_for_planner(plan)

        verify_note = ""
        if stopped:
            if any(s.status != "done" for s in plan.steps):
                await self.ui.save_pending(plan)              # можна відновити кнопкою «Виконати»
        elif touched_by_step:
            verify_note = await self.verify(plan, touched_by_step)
        report = ""
        if not stopped and sum(1 for s in plan.steps if not s.is_noop) >= 2:
            await self.ui.status("Готую підсумок…")
            report = await asyncio.to_thread(lambda: review.final_summary(plan, self.client, verify_note))
        if report:                                             # що саме зроблено, а не лише «Готово»
            await self.ui.say("**Підсумок виконання**\n\n" + report, "answer")
        await self.ui.say("⛔ Зупинено — решту можна продовжити кнопкою «Виконати»" if stopped else "Готово ✓")
        return render_for_planner(plan) + (f"\n{verify_note}" if verify_note else "")

    async def verify(self, plan, touched_by_step: dict) -> str:
        """Звірка після всіх кроків: (1) змінені .py компілюються; (2) LLM-перевірка узгодженості
        кроків (stepreview.check_consistency). Проблему виправляємо ПОВТОРОМ одного кроку з
        уточненням — не більше 2 раундів, щоб не крутитись. Повертає рядок для підсумку."""
        root = self.env.root

        def all_files() -> list[str]:
            return list(dict.fromkeys(f for fl in touched_by_step.values() for f in fl))

        await self.ui.status("Перевіряю результат…")
        await self.ui.say("🔍 Перевіряю результат: синтаксис змінених файлів і узгодженість кроків…")
        for _ in range(2):
            bad = await asyncio.to_thread(lambda: orch.syntax_report(root, all_files()))
            if bad:
                path, msg = bad[0]
                note = f"У файлі {path} синтаксична помилка: {msg}. Виправ її, не ламаючи решту."
                step_id = orch.pick_step(touched_by_step, [p for p, _ in bad])
            else:
                plan.facts["синтаксис"] = "ок (змінені .py компілюються)"
                verdict = await asyncio.to_thread(lambda: check_consistency(plan, self.client))
                step_id = verdict.get("step_id")
                if verdict["ok"] or not any(s.id == step_id for s in plan.steps):
                    await self.ui.say("✓ Звірка пройшла: кроки узгоджені, змінені файли без синтаксичних помилок.")
                    return "Звірка: ок."
                note = verdict.get("note") or "узгодь результат кроків"
            step = next((s for s in plan.steps if s.id == step_id), plan.steps[-1])
            await self.ui.say(f"⚠ Звірка: {note[:300]}\n\nПовторюю крок #{step.id}.")
            run = await self.ui.run_step(step.description + f"\nУточнення після перевірки: {note}",
                                         title=f"повтор: {step.description[:50]}",
                                         context=step_context(self.env, plan, step))
            plan.set_result(step.id, "done" if (run.log and not is_incomplete(run.final)) else "failed", run.final or "—")
            touched_by_step.setdefault(step.id, []).extend(run.touched)
        bad = await asyncio.to_thread(lambda: orch.syntax_report(root, all_files()))
        if bad:
            await self.ui.say(f"⚠ Після повторів лишилась синтаксична помилка у {bad[0][0]}: {bad[0][1]}. "
                              "Перегляньте вручну.")
            return f"Звірка: лишилась помилка у {bad[0][0]}."
        return "Звірка: проблеми виправлено повтором."
