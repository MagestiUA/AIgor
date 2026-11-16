"""Детектор зациклення (agent/loopguard.py): ловить повтори, але НЕ рве довгу справну роботу."""
from agent.loopguard import STUCK_PREFIX, LoopGuard, context_full

ERR = "заблоковано allowlist: some-command-here"          # ≥12 симв.: змістовний результат


def feed(g, calls):
    verdicts = []
    for name, args, res in calls:
        g.observe(name, args, res)
        verdicts.append(g.check())
    return verdicts


def main() -> None:
    # 1) ДОВГА СПРАВНА РОБОТА: 200 різних дій з різними результатами — жодного втручання
    g = LoopGuard()
    v = feed(g, [("read_file", {"path": f"f{i}.py"}, f"вміст файлу номер {i} " * 3) for i in range(200)])
    assert all(x is None for x in v), "довга робота з прогресом не має зупинятись"

    # 2) A: та сама дія й той самий результат ×3 -> warn; ще ×3 після підказки -> stop
    g = LoopGuard()
    v = feed(g, [("run_shell", {"command": "x"}, ERR)] * 3)
    assert v[:2] == [None, None] and v[2][0] == "warn" and "×3" in v[2][1]
    v2 = feed(g, [("run_shell", {"command": "x"}, ERR)] * 3)
    assert v2[2] and v2[2][0] == "stop"
    assert g.stuck_message("причина").startswith(STUCK_PREFIX) and "заблоковано" in g.stuck_message("причина")

    # 3) B (випадок з діагностики): РІЗНІ команди, але та сама відповідь 4 з 6 разів
    g = LoopGuard()
    calls = [("run_shell", {"command": f"python -c print({i})"}, "SyntaxError: invalid syntax in -c ...")
             for i in range(4)]
    calls[1] = ("read_file", {"path": "a.py"}, "інший зміст файлу, інший зміст файлу")
    calls += [("run_shell", {"command": "python -c z"}, "SyntaxError: invalid syntax in -c ..."),
              ("run_shell", {"command": "python -c y"}, "SyntaxError: invalid syntax in -c ...")]
    v = feed(g, calls)
    assert any(x and x[0] == "warn" for x in v), v

    # 4) C: цикл A-B-A-B-A-B з незмінними результатами
    g = LoopGuard()
    cyc = [("read_file", {"path": "a"}, "вміст файлу a, повтор"), ("run_shell", {"command": "t"}, "FAILED test_x in 1.0s")] * 3
    v = feed(g, cyc)
    assert v[-1] and v[-1][0] == "warn" and "цикл" in v[-1][1], v

    # 5) після warn модель РОЗІРВАЛА цикл (нова дія) -> без stop; лічильник свіжий
    g = LoopGuard()
    feed(g, [("run_shell", {"command": "x"}, ERR)] * 3)
    assert g.warned
    v = feed(g, [("write_file", {"path": "fix.py", "content": "ok"}, "записано fix.py (2 символи)"),
                 ("run_shell", {"command": "pytest"}, "3 passed in 0.1s — усе добре")])
    assert v == [None, None]

    # 6) короткі результати ("rc=0") законно збігаються: послідовні команди з rc=0 — не цикл
    g = LoopGuard()
    v = feed(g, [("run_shell", {"command": f"mkdir d{i}"}, "rc=0") for i in range(8)])
    assert all(x is None for x in v)

    # 7) легітимний повтор з ІНШИМ результатом (запуск тестів після правок) — не цикл
    g = LoopGuard()
    v = feed(g, [("run_shell", {"command": "pytest"}, f"{i} failed, {10 - i} passed in 1.{i}s") for i in range(6)])
    assert all(x is None for x in v)

    # 8) D (реальний кейс з логів тестера): A→"[]", B→96/101 симв. (результат «плаває»), так ~сотні разів.
    #    C це не ловить (короткі результати, різні хеші), D — «12 викликів без нового результату»
    g = LoopGuard()
    cyc = []
    for i in range(60):
        cyc.append(("run_shell", {"command": "adb devices"}, "[]"))
        cyc.append(("run_shell", {"command": "adb shell dumpsys wifi"}, "x" * (96 if i % 2 else 101)))
    v = feed(g, cyc)
    kinds = [x[0] for x in v if x]
    assert kinds[:2] == ["warn", "stop"], kinds
    assert "жодного нового результату" in next(x[1] for x in v if x)
    first = next(i for i, x in enumerate(v) if x)
    assert first <= 16, first                      # ловимо за хвилину, а не після 700 викликів

    # 9) а нові результати (справжній прогрес) D не зупиняє, навіть коли дій мало
    g = LoopGuard()
    v = feed(g, [("run_shell", {"command": "git log -1"}, f"commit {i:03d} message") for i in range(40)])
    assert all(x is None for x in v)

    # 10) project_note — службовий тул: підряд однакові відповіді не вважаються зацикленням
    g = LoopGuard()
    v = feed(g, [("project_note", {"path": f"f{i}.py", "note": "n"}, "нотатку збережено в карті проєкту") for i in range(8)])
    assert all(x is None for x in v)

    # context_full
    assert context_full(240_000, 262_144) and not context_full(100_000, 262_144) and not context_full(10, 0)
    print("OK: loopguard — довга робота не рветься, повтор/різні команди з тим самим результатом/цикл ловляться, warn→stop")


if __name__ == "__main__":
    main()
