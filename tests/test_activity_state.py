"""Жива стрічка подій у State: старт -> завершення, статуси, обмеження довжини, час останньої події."""
import lca_web.state as W


def main() -> None:
    s = W.State(_reflex_internal_init=True)
    assert s.activity == [] and s.activity_last == ""
    a = s._act_start("tool", "run_shell", "adb devices")
    b = s._act_start("llm", "Запит до моделі", "повідомлень у контексті: 4")
    assert [e["status"] for e in s.activity] == ["run", "run"] and s.activity_last
    s._act_done(a, "0.2с · List of devices attached", ok=True)
    s._act_done(b, "1.1с · ↑900 ↓40", ok=False)
    by_id = {e["id"]: e for e in s.activity}
    assert by_id[str(a)]["status"] == "ok" and "List of devices" in by_id[str(a)]["detail"]
    assert by_id[str(b)]["status"] == "err"
    assert by_id[str(a)]["title"] == "run_shell" and by_id[str(a)]["kind"] == "tool"
    # довгий результат обрізається, стрічка не росте безмежно (80 останніх)
    long_id = s._act_start("tool", "read_file", "x")
    s._act_done(long_id, "z" * 5000)
    assert len([e for e in s.activity if e["id"] == str(long_id)][0]["detail"]) <= 900
    for i in range(120):
        s._act_start("tool", "read_file", f"f{i}.py")
    assert len(s.activity) == 80 and s.activity[-1]["detail"] == "f119.py"
    # всі значення рядкові (потрібно для rx.foreach по list[dict[str, str]])
    assert all(isinstance(v, str) for e in s.activity for v in e.values())
    print("OK: activity_state — старт/завершення, статуси, обрізання, ліміт 80, рядкові значення")


if __name__ == "__main__":
    main()
