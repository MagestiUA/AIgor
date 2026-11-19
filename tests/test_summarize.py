"""Стиснення історії кроку підсумком від моделі: структура повідомлень, плейсхолдери літералів, довгі значення, збої, інтеграція в цикл."""
import tempfile
from pathlib import Path

from agent import agent_loop as AL
from agent import limits, literals
from agent.toolkit import ToolContext, default_registry

BIG = "Qz9" * 60      # довге значення (180 символів)


class Sum:
    """Клієнт-заглушка: стискач повертає заданий підсумок і запам'ятовує, що йому надіслали."""
    last_stats: dict = {}

    def __init__(self, reply="Прочитано a.py, b.py; знайдено run() у core/x.py. Лишилось: правка.", boom=False):
        self.reply, self.boom, self.sent = reply, boom, []

    def chat(self, messages, tools=None, profile=None, fmt=None):
        if self.boom:
            raise RuntimeError("сервер впав")
        self.sent.append(messages)
        return {"content": self.reply, "tool_calls": []}


def history(n_pairs=6, extra=""):
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "Крок: знайди run()"}]
    for i in range(n_pairs):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": f"f{i}.py"}}}]})
        msgs.append({"role": "tool", "content": f"вміст f{i}.py " + "x" * 300 + (extra if i == 1 else "")})
    return msgs


def main() -> None:
    # 1) структура: system і перше user цілі, хвіст дослівно й не з tool, середина → одне повідомлення-підсумок
    m = history()
    tail = [dict(x) for x in m[-6:]]
    cl = Sum()
    ok, before, after = AL.summarize_history(m, cl, 65536)
    assert ok and before > after > 0
    assert m[0]["content"] == "SYS" and m[1]["content"] == "Крок: знайди run()"
    assert m[2]["role"] == "user" and "ПІДСУМОК" in m[2]["content"] and "run() у core/x.py" in m[2]["content"]
    assert m[3:] == tail and m[3]["role"] != "tool", [x["role"] for x in m]
    assert len(m) == 2 + 1 + 6
    # 2) модель-стискач бачить дії й результати, але довге значення замінено посиланням (не переказується)
    m = history(extra=" ТОКЕН " + BIG)
    cl = Sum()
    AL.summarize_history(m, cl, 65536)
    sent = cl.sent[0][1]["content"]
    assert "ACTION read_file" in sent and "RESULT" in sent and BIG not in sent and "довге значення" in sent
    # 3) плейсхолдери літералів: підсумок без них — дописуємо дослівно (зв'язок з mapping не розривається)
    m = history(extra=" дані __LIT_3__ та __LIT_7__")
    cl = Sum("Усе прочитано, нічого особливого.")
    AL.summarize_history(m, cl, 65536)
    assert "__LIT_3__" in m[2]["content"] and "__LIT_7__" in m[2]["content"]
    # ... а якщо стискач їх зберіг сам — не дублюємо
    m = history(extra=" __LIT_3__")
    AL.summarize_history(m, Sum("Файл містить __LIT_3__ (довге значення)."), 65536)
    assert m[2]["content"].count("__LIT_3__") == 1
    # 3б) справжній round-trip шару літералів через підсумок: після відновлення код байт-у-байт той самий
    src = 'import json\nDATA = json.loads("' + "A1B2" * 400 + '")\n'
    stripped, mapping = literals.strip_code(src)
    m = history(extra=" " + stripped)
    AL.summarize_history(m, Sum("Файл прочитано."), 65536)
    assert literals.restore_code(stripped, mapping) == src and all(k in m[2]["content"] for k in mapping)
    # 4) безпечні відмови: збій моделі, порожній підсумок, замало історії — історія не змінюється
    for cl, msgs in ((Sum(boom=True), history()), (Sum(""), history()), (Sum(), history(2))):
        snap = [dict(x) for x in msgs]
        assert AL.summarize_history(msgs, cl, 65536)[0] is False and msgs == snap
    # 5) розрив пари «виклик → результат» неможливий: хвіст не починається з tool
    m = history(5)
    m.insert(len(m) - 5, {"role": "tool", "content": "друга відповідь на той самий виклик"})
    AL.summarize_history(m, Sum(), 65536)
    assert m[3]["role"] != "tool"
    # 6) інтеграція в run_step: коли prompt перевищує 75% вікна — один виклик стискача, крок продовжується й завершується
    limits.FORCE = limits.REMOTE
    win = limits.REMOTE.context_tokens

    class Loop:
        last_stats: dict = {}

        def __init__(self):
            self.n, self.summ, self.msgs_after = 0, 0, None

        def chat(self, messages, tools=None, profile=None, fmt=None):
            if messages[0]["content"] == AL.SUMMARY_SYSTEM:
                self.summ += 1
                return {"content": "Прочитано f1..f6, знайдено run(). Лишилось: відповісти.", "tool_calls": []}
            self.n += 1
            self.last_stats = {"prompt": int(0.8 * win) if self.n in (8, 9) else 5000}
            if self.summ and self.msgs_after is None:
                self.msgs_after = [dict(x) for x in messages]
            if self.n >= 10:
                return {"content": "Готово: run() у core/x.py", "tool_calls": []}
            return {"content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": f"f{self.n}.py"}}}]}

    root = Path(tempfile.mkdtemp())
    for i in range(1, 12):
        (root / f"f{i}.py").write_text(f"# {i}\n" + "x = 1\n" * (40 + i), encoding="utf-8")
    cl = Loop()
    final, log = AL.run_step("Знайди run()", ToolContext(root=root), cl, default_registry(), max_iters=30)
    assert final == "Готово: run() у core/x.py" and cl.summ == 1, (final, cl.summ)             # лише один раз, не на кожну ітерацію
    assert any("ПІДСУМОК" in (x.get("content") or "") for x in cl.msgs_after)                  # наступний запит уже зі стисненою історією
    limits.FORCE = None
    print("OK: summarize — структура, плейсхолдери/літерали, довгі значення, безпечні відмови, інтеграція в цикл")


if __name__ == "__main__":
    main()
