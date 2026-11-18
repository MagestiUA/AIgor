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
    print("OK: compact")


if __name__ == "__main__":
    main()
