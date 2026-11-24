"""run_shell через PowerShell (фідбек b_0.0.1: цикл на «не знайдено: [WinError 2]»).
Windows-only; зовнішніх мереж/пристроїв не чіпає (ssh лише на закритий локальний порт)."""
import os
import shutil
import time

from agent import limits, tools
from agent.toolkit import ToolContext, default_registry

limits.FORCE = limits.production()


def run(cmd, t=40):
    return tools.run_shell(cmd, cwd=".", allow_all=True, use_shell=True, timeout=t)


def main() -> None:
    if os.name != "nt":
        print("OK: shell_powershell — пропущено (не Windows)")
        return
    # переклад && поза лапками
    assert tools.ps_chain("a && b") == "a; if ($?) { b }"
    assert tools.ps_chain("a && b && c") == "a; if ($?) { b; if ($?) { c } }"
    assert tools.ps_chain('adb shell "cd /sdcard && ls"') == 'adb shell "cd /sdcard && ls"'
    assert tools.ps_chain("echo 'x && y' && echo z") == "echo 'x && y'; if ($?) { echo z }"
    assert tools.ps_chain("a &&") == "a &&" and tools.ps_chain("plain") == "plain"

    # ланцюжки, PowerShell-синтаксис, кирилиця, коди виходу
    assert run("echo hi && echo there").stdout.split() == ["hi", "there"]
    assert run("cmd /c exit 3 && echo UNREACHABLE").returncode == 3
    assert run("echo ok && cmd /c exit 4").returncode == 4
    assert run("Write-Output 'Привіт, світе'").stdout.strip() == "Привіт, світе"
    assert run("(Get-Location).Path").stdout.strip()
    # лапки: екрановані внутрішні лапки доходять до зовнішньої програми
    out = run("python -c \"import sys; print(sys.argv[1:])\" 'say \\\"hi\\\"'").stdout.strip()
    assert out == "['say \"hi\"']", out

    # зависання: stdin закрито — інтерактивна програма падає, а не висить
    t0 = time.time()
    r = run("python -c \"input('пароль: ')\"", t=20)
    assert r.returncode != 0 and time.time() - t0 < 15, (r.returncode, time.time() - t0)

    # таймаут вбиває дерево й віддає зрозуміле повідомлення; демон-фон не блокує повернення
    t0 = time.time()
    r = run("Start-Sleep 30", t=3)
    assert r.returncode == -1 and "timeout" in r.stderr and time.time() - t0 < 12
    t0 = time.time()
    r = run("Start-Process -WindowStyle Hidden powershell -ArgumentList '-NoProfile','-Command','Start-Sleep 20'; echo started", t=15)
    assert "started" in r.stdout and time.time() - t0 < 10, "фоновий процес із успадкованими дескрипторами не має блокувати"

    # ssh/adb: якщо встановлені — швидка відмова замість зависання (без реальних хостів/пристроїв)
    if shutil.which("ssh"):
        t0 = time.time()
        r = run("ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no nobody@127.0.0.1 'echo hi'", t=20)
        assert r.returncode != 0 and time.time() - t0 < 15
    if shutil.which("adb"):
        t0 = time.time()
        r = run("adb version", t=20)
        assert r.returncode == 0 and "Android Debug Bridge" in r.stdout and time.time() - t0 < 15

    # без оболонки (allowlist): зрозуміла помилка з ім'ям програми та підказкою
    r = tools.run_shell("cd x && y", cwd=".", allow_all=True)
    assert "'cd'" in r.stderr and "БЕЗ оболонки" in r.stderr

    # через тул: smart-режим виконує PowerShell-команду
    ctx = ToolContext(root=os.getcwd(), permissions={"edits": "off", "shell": "smart"})
    from pathlib import Path
    ctx.root = Path(os.getcwd())
    res = default_registry().dispatch("run_shell", {"command": "echo a && echo b"}, ctx)
    assert res.split() == ["a", "b"], res
    print("OK: shell_powershell — && , лапки, UTF-8, stdin закрито, таймаут/демони, ssh/adb, повідомлення без оболонки")


if __name__ == "__main__":
    main()
