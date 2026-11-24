"""Стиснення старих результатів тулів у циклі кроку."""
from agent.agent_loop import compact_tool_results


def main() -> None:
    big = "x" * 5000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": big},
            {"role": "tool", "content": big}, {"role": "tool", "content": "коротко"}] + \
           [{"role": "assistant", "content": "a"}] * 3 + [{"role": "tool", "content": big}]
    n = compact_tool_results(msgs, keep_last=2)
    assert n == 1 and "стиснуто" in msgs[2]["content"] and len(msgs[2]["content"]) < 700
    assert msgs[1]["content"] == big and msgs[3]["content"] == "коротко" and msgs[-1]["content"] == big   # user/короткі/хвіст цілі
    assert compact_tool_results(msgs, keep_last=2) == 0                                                    # повторно не чіпає

    # стиснутий результат несе ТОЧНУ адресу оригіналу: який файл/рядки перечитати, де лежить повний вивід команди
    spill = "голова виводу " * 100 + "\n…[пропущено 9000 з 12000 симв.; повний вивід у файлі C:\\t\\out-1.txt — знайти]…\n" + "хвіст " * 100
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "крок"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.py", "start_line": 40, "limit": 80}}},
                {"function": {"name": "run_shell", "arguments": {"command": "pytest -q"}}}]},
            {"role": "tool", "content": big}, {"role": "tool", "content": spill},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "search", "arguments": {"pattern": "def load", "glob": "*.py"}}},
                {"function": {"name": "run_shell", "arguments": {"command": "git log"}}}]},
            {"role": "tool", "content": big}, {"role": "tool", "content": big}] + [{"role": "assistant", "content": "a"}] * 2
    assert compact_tool_results(msgs, keep_last=2) == 4
    assert "оригінал — read_file(path='a.py', start_line=40, limit=80)" in msgs[3]["content"], msgs[3]["content"][-120:]
    assert "read_file(path='C:\\t\\out-1.txt')" in msgs[4]["content"], msgs[4]["content"][-160:]
    assert "search(pattern='def load', glob='*.py')" in msgs[6]["content"]
    assert "повтори команду run_shell('git log')" in msgs[7]["content"]
    print("OK: compact — старі результати стиснуто з точною адресою оригіналу")


if __name__ == "__main__":
    main()
