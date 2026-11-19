"""Субагент-дослідник: окремий цикл з ЧИСТИМ контекстом для кроків, що багато читають.

Ідея: сирі виводи (читання файлів, списки тек, пошук) лишаються всередині субагента, а в головний контекст
виконавця повертається лише КОРОТКИЙ висновок. Субагент лише читає (list_dir, read_file, read_attachment,
web_search) — нічого не змінює й не запускає команд, тож не потребує підтверджень і безпечний.

Цикл — те саме ядро, що в виконавця (agent.steploop), з іншим налаштуванням: без дочитування/nudge, без підсумку
історії, свій малий бюджет (MAX_ITERS), а замість «невдачі» (ліміт, зациклення, повне вікно) — примусовий висновок
зі зібраного.

Літерали: субагент нічого не пише, а в висновку просимо НЕ переказувати довгі значення (токени,
ключі, base64, великі рядки) — лише вказувати файл і місце, звідки їх копіювати інструментами
edit_file/create_from_source. Так шар літералів (literals.py) лишається єдиним шляхом перенесення довгих значень.
"""
from __future__ import annotations

from . import logs
from .literals import mask_long
from .toolkit import Tool, ToolContext, ToolRegistry, default_registry

READ_ONLY = ("list_dir", "read_file", "read_attachment", "web_search")
MAX_ITERS = 12            # бюджет читань субагента (далі — примусовий висновок)
REPORT_MAX = 1800

SYSTEM = (
    "You are a RESEARCH sub-agent. You only READ: list_dir, read_file, read_attachment, web_search. You cannot "
    "change files or run commands. Investigate the task you are given, then reply with a SHORT conclusion "
    "(max ~1500 characters): what you found, exact file paths and function/line names, and what the caller "
    "should do next. Rules: never paste long values (tokens, keys, base64, big strings, data arrays) - give "
    "the file path and where to find them instead; do not paste whole files, quote only the few key lines. "
    "Answer in the language of the task. When you have enough, stop calling tools and give the conclusion."
)


def readonly_registry() -> ToolRegistry:
    full = default_registry()
    r = ToolRegistry()
    for name in READ_ONLY:
        t = full._tools.get(name)
        if t:
            r.register(t)
    return r


def _clip_report(text: str) -> str:
    t = mask_long((text or "").strip())      # довгі значення — посиланням: не роздувають контекст і не псуються переказом
    if len(t) > REPORT_MAX:
        t = t[:REPORT_MAX] + f"\n…[висновок скорочено, було {len(t)} символів]"
    return t


def investigate(task: str, ctx: ToolContext, client=None, max_iters: int = MAX_ITERS) -> str:
    """Виконати дослідження в ізольованому контексті. Повертає короткий висновок + службовий рядок."""
    from .agent_loop import ITER_LIMIT_MSG, finalize          # лениво: agent_loop імпортує toolkit
    from .loopguard import CONTEXT_FULL_PREFIX
    from .steploop import LoopConfig, LoopHooks, run_loop, run_sync
    client = client or ctx.client
    if client is None:
        return "субагент недоступний: немає клієнта моделі"

    class _Hooks(LoopHooks):
        async def dispatch(self, registry, name, args, ctx_):
            if name not in READ_ONLY:
                return f"тул {name} недоступний субагенту (лише читання)"
            return await super().dispatch(registry, name, args, ctx_)

    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
    cfg = LoopConfig(max_iters=max_iters, nudge=False, summarize=False, finalize_on_stuck=True,
                     compact_keep=4, compact_head=400)     # старі виводи не мають переповнити вікно субагента
    res = run_sync(run_loop(messages, registry=readonly_registry(), ctx=ctx, client=client, hooks=_Hooks(), cfg=cfg))
    final = res.final
    if not final or final == ITER_LIMIT_MSG or final.startswith(CONTEXT_FULL_PREFIX):
        # бюджет/контекст вичерпано, зациклення чи порожня відповідь — висновок із зібраного, без тулів (а не «невдача»)
        final = finalize(messages, client) or "(висновку нема: субагент не встиг нічого з'ясувати)"
    n_calls = len(res.actions)
    logs.get("tools").info("субагент: %d дій, висновок %d симв.", n_calls, len(final))
    return f"{_clip_report(final)}\n\n[субагент: {n_calls} дій читання; сирі виводи в контекст не потрапили]"


def h_investigate(args, ctx):
    task = (args.get("task") or "").strip()
    if not task:
        return "порожнє завдання для субагента"
    return investigate(task, ctx)


INVESTIGATE_TOOL = Tool(
    "investigate",
    "Доручити ДОСЛІДЖЕННЯ окремому субагенту з чистим контекстом (лише читання: list_dir, read_file, "
    "read_attachment, web_search). Використовуй, коли треба прочитати багато файлів чи розібратись у коді/логах "
    "перед правкою: у твій контекст прийде лише короткий висновок (шляхи, функції, що робити), а не сирий "
    "вміст. Сам субагент нічого не змінює — правки й команди виконуєш ти. Задай конкретне питання.",
    {"task": {"type": "string", "description": "що з'ясувати (конкретно: де шукати, що саме потрібно знати)"}},
    ["task"], h_investigate)
