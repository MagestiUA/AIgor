"""agent/activity.py: опис викликів і результатів для стрічки подій."""
from agent.activity import clip_result, result_ok, summarize_args


def main() -> None:
    assert summarize_args("run_shell", {"command": "  adb devices  "}) == "adb devices"
    assert summarize_args("read_file", {"path": "a.py", "offset": 500}) == "a.py (з позиції 500)"
    assert summarize_args("write_file", {"path": "x.py", "content": "abc"}) == "x.py (3 симв.)"
    assert summarize_args("web_search", {"query": "reflex   tips"}) == "reflex tips"
    assert summarize_args("edit_file", {"path": "m.py", "instruction": "перейменуй"}).startswith("m.py — перейменуй")
    assert summarize_args("невідомий", {"k": "v"}) == '{"k": "v"}'
    assert summarize_args("run_shell", {"command": "x" * 900}).endswith("…")
    assert clip_result("") == "(порожній результат)"
    assert clip_result("ok") == "ok"
    long = clip_result("a" * 2000)
    assert long.startswith("a" * 600) and "усього 2000 симв." in long
    for bad in ("помилка інструмента x: boom", "заблоковано allowlist: x", "не знайдено програму 'cd'",
                "файл не знайдено: a.py", "timeout 120s — зупинено", "команду відхилено користувачем"):
        assert not result_ok(bad), bad
    assert result_ok("rc=0") and result_ok("=== a.py ===\nтекст") and result_ok("")
    print("OK: activity — опис викликів, стислий результат, ознака помилки")


if __name__ == "__main__":
    main()
