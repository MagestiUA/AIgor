"""Офлайн-тест tool-loop (стаб моделі повертає заскриптовані виклики тулів).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_agent_loop
"""
import tempfile
from pathlib import Path

from agent.agent_loop import (pending_continuation, run_step, _looks_like_leaked_reasoning,
                              extract_intended_attachments)
from agent.toolkit import ToolContext


class LoopStub:
    """Підставна модель: повертає заздалегідь задані відповіді по черзі."""
    def __init__(self, scripted: list[dict]):
        self.scripted = scripted
        self.i = 0

    def chat(self, messages, tools=None, profile=None, fmt=None):
        m = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return m


from agent import limits
limits.FORCE = limits.LOCAL   # не залежати від налаштувань користувача


def main() -> None:
    root = Path(tempfile.mkdtemp())
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "allowlist"})

    # Сценарій: модель кличе write_file, потім завершує без виклику.
    stub = LoopStub([
        {"tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "new.txt", "content": "hello"}}}]},
        {"content": "Готово: створив new.txt", "tool_calls": []},
    ])
    final, log = run_step("створи new.txt", ctx, client=stub, max_iters=5)
    assert (root / "new.txt").read_text(encoding="utf-8") == "hello", "файл не створено циклом"
    assert "Готово" in final
    assert any("write_file" in entry for entry in log)

    # Ліміт ітерацій: модель щоразу кличе тул і не завершує.
    inf = LoopStub([{"tool_calls": [{"function": {"name": "list_dir", "arguments": {}}}]}])
    final2, log2 = run_step("безкінечно", ctx, client=inf, max_iters=3)
    assert "ліміт" in final2 and len(log2) == 3, (final2, log2)

    # Порожня відповідь без tool_calls (модель "видихнулась") -> nudge -> реальна дія.
    empty_then_call = LoopStub([
        {"content": "", "tool_calls": []},
        {"tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "after_nudge.txt", "content": "ok"}}}]},
        {"content": "Готово", "tool_calls": []},
    ])
    final3, log3 = run_step("створи after_nudge.txt", ctx, client=empty_then_call, max_iters=5)
    assert (root / "after_nudge.txt").read_text(encoding="utf-8") == "ok", "nudge не витягнув крок"
    assert "Готово" in final3

    # Просочений виклик тула як текст (<tool_call>/<tools> у content) -> nudge -> дія.
    leaked_then_call = LoopStub([
        {"content": '<tools>\n{"name": "write_file"}\n</tools>', "tool_calls": []},
        {"tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "after_leak.txt", "content": "ok"}}}]},
        {"content": "Готово", "tool_calls": []},
    ])
    final4, log4 = run_step("створи after_leak.txt", ctx, client=leaked_then_call, max_iters=5)
    assert (root / "after_leak.txt").read_text(encoding="utf-8") == "ok", "nudge не витягнув крок (leak)"
    assert "Готово" in final4

    # Просочений виклик з ПОВНИМИ аргументами -> розпарсити й ВИКОНАТИ напряму,
    # без nudge-раунду взагалі (живий кейс: модель повторює той самий зламаний
    # формат і ПІСЛЯ nudge — retry не зарадить, бо вже маємо все потрібне з тексту).
    leaked_full_args = LoopStub([
        {"content": '{"name": "write_file", "arguments": '
                    '{"path": "after_recover.txt", "content": "recovered"}}', "tool_calls": []},
        {"content": "Готово", "tool_calls": []},
    ])
    final5, log5 = run_step("створи after_recover.txt", ctx, client=leaked_full_args, max_iters=5)
    assert (root / "after_recover.txt").read_text(encoding="utf-8") == "recovered", "не розпарсило й не виконало"
    assert any("write_file" in entry for entry in log5)
    assert "Готово" in final5

    # Той самий розпарсюваний випадок, але з малформованим зайвим '}' наприкінці
    # (живий кейс на qwen3-coder) — брейс-каунтинг має зупинитись на балансі й
    # ігнорувати хвіст.
    leaked_malformed = LoopStub([
        {"content": '{"name": "write_file", "arguments": '
                    '{"path": "after_malformed.txt", "content": "ok2"}}}', "tool_calls": []},
        {"content": "Готово", "tool_calls": []},
    ])
    final6, log6 = run_step("створи after_malformed.txt", ctx, client=leaked_malformed, max_iters=5)
    assert (root / "after_malformed.txt").read_text(encoding="utf-8") == "ok2", "не впоралось з малформованим хвостом"
    assert "Готово" in final6

    # pending_continuation: великий файл прочитано не повністю -> модель пише прозою
    # замість виклику -> нюджимо НЕЗАЛЕЖНО від should_nudge (контент не порожній і
    # не просочений, інакше вважався б валідним завершенням).
    assert pending_continuation([
        {"role": "tool", "content": "=== big.txt (0-32000 з 70000) ===\nabc\n\n"
         "…[лишилось 38000 символів. Щоб продовжити, виклич "
         "read_attachment(name='big.txt', offset=32000)]"},
    ])
    assert not pending_continuation([{"role": "tool", "content": "=== small.txt ===\nok"}])
    assert not pending_continuation([])
    att_dir = Path(tempfile.mkdtemp())
    (att_dir / "big.txt").write_text("x" * 40_000, encoding="utf-8")
    ctx_att = ToolContext(root=root, permissions={"edits": "auto", "shell": "allowlist"},
                          attachments_dir=att_dir)
    # Модель прочитала перший шматок, потім ЗАМІСТЬ продовження пагінації написала
    # прозою "продовжую читати" (живий кейс: повторюється так 15+ разів підряд) —
    # авто-відновлення саме дочитує решту, без участі моделі взагалі.
    continue_then_done = LoopStub([
        {"tool_calls": [{"function": {"name": "read_attachment",
                                      "arguments": {"name": "big.txt", "offset": 0}}}]},
        {"content": "Продовжую читати файл.", "tool_calls": []},   # не порожньо, не просочено
        {"content": "Готово, прочитав усе.", "tool_calls": []},
    ])
    final7, log7 = run_step("прочитай big.txt", ctx_att, client=continue_then_done, max_iters=6)
    assert "Готово" in final7, final7
    assert sum(1 for e in log7 if "read_attachment" in e) == 2, log7   # offset=0 + авто-продовження offset=32000

    # Наративне оголошення наміру викликати тул без structured tool_calls
    # ("I will call read_attachment for all 6 files first.") -> nudge -> дія.
    # Живий кейс: gather-фаза _run_tool_chat — модель просто зупинялась тут,
    # "нічого не відбувалось".
    intent_then_call = LoopStub([
        {"content": "I will call read_attachment for all 6 files first.", "tool_calls": []},
        {"tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "after_intent.txt", "content": "ok"}}}]},
        {"content": "Готово", "tool_calls": []},
    ])
    final8, log8 = run_step("створи after_intent.txt", ctx, client=intent_then_call, max_iters=5)
    assert (root / "after_intent.txt").read_text(encoding="utf-8") == "ok", "nudge не витягнув крок (intent-only)"
    assert "Готово" in final8

    # extract_intended_attachments: модель цитує конкретні імена -> повертає лише
    # згадані (порядок як у known_names); жодне ім'я текстуально не співпало ("усі 6
    # файлів" без перерахування) -> fallback на ВСІ відомі файли; немає відомих файлів -> [].
    known = ["a.md", "b.md", "c.md"]
    assert extract_intended_attachments("I will call read_attachment for b.md and a.md", known) \
        == ["a.md", "b.md"]
    assert extract_intended_attachments("I will call read_attachment for all 6 files first.", known) \
        == known
    assert extract_intended_attachments("I will call read_attachment for all files.", []) == []

    # _looks_like_leaked_reasoning: живий кейс — gemma4 видала англомовний self-talk
    # ("Wait,"/"Self-Correction"/"*Actually*") прямо у видиму відповідь замість
    # чистого тексту -> 3+ маркери = просочення; звичайна відповідь (навіть з
    # одиничним випадковим "Wait") -> не просочення.
    leaked_body = ('```text\nWait, let me reconsider.\nSelf-Correction: actually no.\n'
                   '*Actually* I should re-check.\n```\nЗрозумів, ось відповідь.')
    assert _looks_like_leaked_reasoning(leaked_body), leaked_body
    assert not _looks_like_leaked_reasoning("Зрозумів, ось готова відповідь українською.")
    assert not _looks_like_leaked_reasoning("Wait — це звичайне слово в коді, не просочення.")

    # Сильна модель (limits.REMOTE, weak_model_fixes=False): текст лишається текстом.
    # Живий кейс T5: відповідь із прикладом JSON {"name": "write_file", ...} НЕ має
    # виконуватись як виклик і НЕ має провокувати nudge; порожня відповідь — nudge лишається.
    from agent.agent_loop import recover_tool_calls, should_nudge
    limits.FORCE = limits.REMOTE
    try:
        ex = 'Ось приклад: {"name": "write_file", "arguments": {"path": "demo.txt", "content": "x"}}'
        assert recover_tool_calls({"content": ex}) == ([], False)
        assert not should_nudge(ex, False)
        assert not should_nudge("I will call read_attachment for all files", False)
        assert should_nudge("", False) and not should_nudge("", True)
        strong = LoopStub([{"content": ex, "tool_calls": []}])
        final9, log9 = run_step("покажи приклад JSON", ctx, client=strong, max_iters=3)
        assert not (root / "demo.txt").exists() and not log9 and "приклад" in final9
    finally:
        limits.FORCE = limits.LOCAL
    # ...а для слабої (LOCAL) той самий текст, як і раніше, виконується (зворотна сумісність)
    assert recover_tool_calls({"content": ex})[1] is True

    # ── LoopGuard у run_step (сервер: limits.REMOTE, hard_iters=400, без оцінки кроку) ──
    from agent.agent_loop import is_incomplete
    from agent.loopguard import STUCK_PREFIX

    class Recorder:
        """Скриптова модель, що пам'ятає, які повідомлення бачила на кожному виклику."""
        def __init__(self, fn):
            self.fn, self.n, self.seen = fn, 0, []

        def chat(self, messages, tools=None, profile=None, fmt=None):
            self.seen.append([m.get("content", "") for m in messages if m.get("role") == "user"])
            self.n += 1
            return self.fn(self, messages)

    def read_missing(path):
        return {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": path}}}]}

    limits.FORCE = limits.REMOTE
    try:
        # 1) модель вічно б'ється в одну стіну: підказка після 3-го, зупинка після 6-го виклику
        loop = Recorder(lambda s, m: read_missing("нема.py"))
        f, lg = run_step("залип", ctx, client=loop, max_iters=None)
        assert f.startswith(STUCK_PREFIX) and len(lg) == 6 and loop.n == 6, (f, len(lg), loop.n)
        assert any("repeating the same actions" in u for u in loop.seen[3]), "після 3-го виклику має бути підказка"
        assert is_incomplete(f)

        # 2) після підказки модель змінює підхід і завершує — зупинки немає
        def recovering(s, m):
            warned = any("repeating the same actions" in (x.get("content") or "") for x in m if x.get("role") == "user")
            return {"content": "Блокер описано: файлу немає.", "tool_calls": []} if warned else read_missing("нема.py")
        ok = Recorder(recovering)
        f2, lg2 = run_step("залип і виправився", ctx, client=ok, max_iters=None)
        assert f2 == "Блокер описано: файлу немає." and not is_incomplete(f2) and len(lg2) == 3

        # 3) довга справна робота (60 різних дій — більше за колишній ліміт) НЕ зупиняється
        def long_work(s, m):
            return read_missing(f"f{s.n}.py") if s.n < 60 else {"content": "Готово, 60 кроків.", "tool_calls": []}
        import agent.agent_loop as _AL
        _rf, _AL.READ_FORCE = _AL.READ_FORCE, 10**6      # тут перевіряємо LoopGuard; ліміт «читань підряд» має свій тест
        lw = Recorder(long_work)
        f3, lg3 = run_step("довго", ctx, client=lw, max_iters=None)
        assert f3 == "Готово, 60 кроків." and len(lg3) == 59, (f3, len(lg3))
        _AL.READ_FORCE = _rf
    finally:
        limits.FORCE = limits.LOCAL
    assert is_incomplete("досягнуто ліміту ітерацій") and not is_incomplete("Готово")

    print("OK: tool-loop виконує тули через реєстр, завершується, ліміт ітерацій тримає, nudge витягує порожні/просочені відповіді, парсинг просочених викликів виконує дію напряму, pending_continuation тримає пагінацію великих файлів, _looks_like_leaked_reasoning ловить просочений self-talk")
    print(f"  журнал кроку 1: {log}")


if __name__ == "__main__":
    main()
