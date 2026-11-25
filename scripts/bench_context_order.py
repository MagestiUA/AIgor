"""Вимір: чи прискорює новий порядок контексту кроку префікс-кеш сервера (vLLM).

Старий порядок: тека → ЗАДАЧА І ПЛАН (змінюється після кожного кроку) → карта → підсумок → нотатки.
Новий порядок (plan_runner.step_context): тека → карта → підсумок → нотатки → … → задача й план у КІНЦІ.
Префікс-кеш повторно використовує лише спільний початок запиту; у старому порядку він обривався на плані, тож карту й
підсумок модель обробляла заново на кожному кроці. Скрипт ганяє 6 «кроків» підряд (план між ними росте, як у справжньому
виконанні) в обох порядках і порівнює час запиту (max_tokens=1 → це майже чистий prefill) та кешовані токени.

    python scripts/bench_context_order.py [--trials 3] [--steps 6]

Модель і сервер — з налаштувань застосунку (agent/backend.py), нічого не пишеться в профіль (карта проєкту — у тимчасову
теку). Ніякого вмісту чатів: контекст синтетичний + карта цього репозиторію.
"""
from __future__ import annotations

import argparse
import random
import re
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from agent import backend, config, limits, projectmap  # noqa: E402
from agent import plan_runner as PR  # noqa: E402
from agent.agent_loop import SYSTEM  # noqa: E402
from agent.convo import as_context  # noqa: E402
from agent.memory import TaskState, render_for_planner  # noqa: E402
from agent.toolkit import default_registry  # noqa: E402

WORDS = ("файл функція клас модуль тест помилка виправлено додано змінено крок результат перевірка конфіг сервер запит "
         "відповідь контекст токен проєкт план виконавець планувальник стиснення підсумок розмова нотатка вкладення").split()


def fake_summary(chars: int, seed: int = 1) -> str:
    rnd = random.Random(seed)
    lines, n = [], 0
    while n < chars:
        ln = "- " + " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(12, 30))) + f" ({rnd.choice(WORDS)}{rnd.randint(1, 99)}.py:{rnd.randint(1, 900)})"
        lines.append(ln)
        n += len(ln) + 1
    return "\n".join(lines)


def old_order(env: PR.PlanEnv, plan, step) -> str:
    """Порядок контексту кроку ДО рефакторингу (план одразу після теки)."""
    ctx = (f"Project root: {env.root}\n{env.roots_note}\nЗагальна задача: {plan.task}\n"
           f"План і результати вже виконаних кроків (ти виконуєш ЛИШЕ крок #{step.id}):\n"
           f"{render_for_planner(plan)}")
    pmap = projectmap.context_block(env.root)
    if pmap:
        ctx += "\n\n" + pmap
    if env.summary:
        ctx += f"\n{as_context(env.summary)}"
    return ctx


def prefix_counters(base: str) -> tuple[float, float] | None:
    """(попадання, запити) префікс-кешу з /metrics vLLM — запасний шлях, якщо usage не віддає cached_tokens."""
    try:
        txt = requests.get(re.sub(r"/v1$", "", base) + "/metrics", timeout=5).text
    except requests.RequestException:
        return None
    def total(name: str) -> float | None:
        vals = re.findall(rf"^{re.escape(name)}(?:{{[^}}]*}})?\s+([0-9.eE+-]+)", txt, re.M)
        return sum(float(v) for v in vals) if vals else None
    h, q = total("vllm:prefix_cache_hits_total"), total("vllm:prefix_cache_queries_total")
    return (h, q) if h is not None and q is not None else None


def request(client, ctx: str, step_text: str, tools: list) -> tuple[float, int, int | None]:
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Контекст:\n{ctx}\n\nКрок:\n{step_text}"}]
    p = client._payload(msgs, tools, config.EXECUTOR, None, False)
    p["max_tokens"] = 1
    before = prefix_counters(client.base_url)
    t0 = time.monotonic()
    r = client._post_with_retry(p)
    client._check(r)
    dt = time.monotonic() - t0
    u = r.json().get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is None and before:
        after = prefix_counters(client.base_url)
        if after:
            cached = int(after[0] - before[0])
    return dt, int(u.get("prompt_tokens", 0) or 0), cached


def run_condition(client, build, summary: str, steps: int, tools: list, tag: str = "") -> list[tuple[float, int, int | None]]:
    plan = TaskState(task="Додати експорт у CSV, покрити тестами й оновити документацію")
    for i in range(1, steps + 1):
        plan.add_step("act", f"Крок {i}: виконати частину задачі над модулем export{i}.py")
    env = PR.PlanEnv(root=str(ROOT), roots_note=f"(прогін {uuid.uuid4().hex})", summary=summary)   # унікальний початок → кеш «холодний»
    out = []
    for step in plan.steps:
        t0 = time.monotonic()
        ctx = build(env, plan, step)
        prep = time.monotonic() - t0
        out.append(request(client, ctx, step.description, tools))
        dt, ptok, cached = out[-1]
        print(f"  {tag} крок{step.id}: запит {dt:.2f} с, промпт {ptok} ток., кеш {cached}, підготовка {prep:.2f} с", flush=True)
        plan.set_result(step.id, "done", f"Крок виконано: змінено export{step.id}.py, додано функцію та перевірку; усі тести проходять. " * 2)
        step.report = f"$ pytest -q tests/test_export{step.id}.py\n" + "." * 40 + "\n12 passed in 0.8s\n✎ replace_in_file export.py: замінено в export.py" * 3
    return out


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--steps", type=int, default=6)
    a = ap.parse_args()
    projectmap.STORE_DIR = Path(tempfile.mkdtemp()) / "projects"      # профіль користувача не чіпаємо
    # Карта проєкту під час виміру не змінюється — рахуємо ОДИН раз (інакше кожен крок скануватиме теку й вноситиме
    # паузи між запитами, а нам потрібна чиста різниця порядку контексту)
    fixed_map = projectmap.context_block(str(ROOT))
    projectmap.context_block = lambda *_a, **_k: fixed_map
    cfg = backend.resolve()
    if cfg["backend"] != "openai":
        print("Вимір має сенс для сервера з префікс-кешем (vLLM): у налаштуваннях зараз локальна модель.")
        return 2
    client = backend.make_client()
    tools = default_registry().schema()
    print(f"Модель: {client.model}; вікно за налаштуваннями: {limits.get().context_tokens}")
    try:
        request(client, "прогрів", "прогрів", tools)
        _, ptok, cached = request(client, "прогрів", "прогрів", tools)
    except Exception as e:                                             # noqa: BLE001
        print(f"Сервер недоступний: {e}")
        return 2
    print(f"Розмір системного промпта+тулів ≈ {ptok} ток.; кешовані токени {'повідомляються' if cached is not None else 'НЕ повідомляються (час — єдина ознака)'}")

    for label, chars in (("типовий: підсумок ≈ 3k симв.", 3000), ("довга розмова: підсумок ≈ 25k симв.", 25000)):
        res: dict[str, list[list]] = {"старий": [], "новий": []}
        for t in range(a.trials):
            order = [("старий", old_order), ("новий", PR.step_context)]
            for name, build in (order if t % 2 == 0 else order[::-1]):     # чергуємо, щоб шум сервера ділився порівну
                res[name].append(run_condition(client, build, fake_summary(chars, seed=t + 1), a.steps, tools,
                                               tag=f"[{label[:7]} · {name} · прогін {t + 1}]"))
        n1 = res["новий"][0][1][1]
        print(f"\n=== {label}; промпт кроку ≈ {n1} ток. (карта ≈ {len(fixed_map) // 3} ток.) ===")
        print("час запиту, с (медіана з %d прогонів)       " % a.trials + "".join(f"  крок{i + 1}" for i in range(a.steps)) + "   Σ кроків 2..N   кеш кроків 2..N")
        for name in ("старий", "новий"):
            times = [med([r[s][0] for r in res[name]]) for s in range(a.steps)]
            cached = [med([r[s][2] for r in res[name]]) for s in range(a.steps)]
            ptoks = [med([r[s][1] for r in res[name]]) for s in range(a.steps)]
            cc = sum(c or 0 for c in cached[1:])
            pp = sum(ptoks[1:])
            cache_txt = f"{cc}/{pp} ток. ({100 * cc // max(pp, 1)}%)" if any(c is not None for c in cached) else "—"
            print(f"{name:<44}" + "".join(f"  {t:5.2f}" for t in times) + f"   {sum(times[1:]):8.2f} с     {cache_txt}")
        old_t = sum(med([r[s][0] for r in res['старий']]) for s in range(1, a.steps))
        new_t = sum(med([r[s][0] for r in res['новий']]) for s in range(1, a.steps))
        print(f"→ нове швидше на кроках 2..N на {old_t - new_t:.2f} с ({100 * (old_t - new_t) / old_t:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
