"""Субагент-дослідник: окремий цикл з ЧИСТИМ контекстом для кроків, що багато читають.

Ідея (перший етап перебудови): сирі виводи (читання файлів, списки тек, пошук) лишаються всередині
субагента, а в головний контекст виконавця повертається лише КОРОТКИЙ висновок. Субагент лише
читає (list_dir, read_file, read_attachment, web_search) — нічого не змінює й не запускає команд,
тож не потребує підтверджень і безпечний.

Літерали: субагент нічого не пише, а в висновку просимо НЕ переказувати довгі значення (токени,
ключі, base64, великі рядки) — лише вказувати файл і місце, звідки їх копіювати інструментами
edit_file/create_from_source. Так шар літералів (literals.py) лишається єдиним шляхом перенесення довгих значень.
"""
from __future__ import annotations

import re

from . import config, limits, logs
from .literals import mask_long
from .loopguard import LoopGuard
from .toolkit import Tool, ToolContext, ToolRegistry, default_registry

READ_ONLY = ("list_dir", "read_file", "read_attachment", "web_search")
MAX_ITERS = 12            # бюджет читань субагента (далі — примусовий висновок)
WARN_AT = 8
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
    from .agent_loop import _args, recover_tool_calls          # лениво: agent_loop імпортує toolkit
    client = client or ctx.client
    if client is None:
        return "субагент недоступний: немає клієнта моделі"
    from .agent_loop import COMPACT_RATIO, compact_tool_results, finalize
    from .loopguard import context_full
    reg = readonly_registry()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
    guard, n_calls, final = LoopGuard(), 0, ""
    log = logs.get("tools")
    win = limits.get().context_tokens
    for it in range(max_iters):
        msg = client.chat(messages, tools=reg.schema(), profile=config.EXECUTOR)
        calls, recovered = recover_tool_calls(msg)
        if not calls:
            final = (msg.get("content") or "").strip()
            break
        messages.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""), "tool_calls": calls})
        for call in calls:
            name = call.get("function", {}).get("name", "")
            args = _args(call)
            result = reg.dispatch(name, args, ctx) if name in READ_ONLY else f"тул {name} недоступний субагенту (лише читання)"
            n_calls += 1
            messages.append({"role": "tool", "content": result})
            guard.observe(name, args, result)
        verdict = guard.check()
        if verdict:
            if verdict[0] == "stop":
                break                                           # зациклився — нижче просимо висновок зі зібраного
            messages.append({"role": "user", "content": "Не повторюй ті самі дії. Дай висновок із того, що вже знайдено."})
        elif it + 1 == WARN_AT:
            messages.append({"role": "user", "content": "Ти вже прочитав достатньо. Якщо можеш відповісти — дай висновок зараз, без нових викликів."})
        ls = getattr(client, "last_stats", None) or {}
        if ls and win and ls.get("prompt", 0) >= COMPACT_RATIO * win:
            compact_tool_results(messages, keep_last=4, head=400)     # старі виводи не мають переповнити вікно субагента
            if context_full(ls.get("prompt", 0), win):
                break
    if not final:
        # бюджет/контекст вичерпано — висновок із зібраного, без тулів (а не «невдача»)
        final = finalize(messages, client) or "(висновку нема: субагент не встиг нічого з'ясувати)"
    log.info("субагент: %d дій, висновок %d симв.", n_calls, len(final))
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
