"""Точка входу local-code-agent: піднімає сервер і відкриває застосунок у
нативному вікні (pywebview, WebView2 на Windows) — без вкладки браузера.

Зручно з PyCharm: відкрий цей файл і натисни ▶ Run (інтерпретатор — .venv проєкту).

Сервер (Reflex) піднімається як ОКРЕМИЙ ПРОЦЕС, не потік: Granian/Reflex
реєструє signal-handler, що працює лише в головному потоці головного
інтерпретатора — у фоновому потоці це падає з "signal only works in main
thread of the main interpreter" (перевірено живцем). Реальний URL читаємо З
ВИВОДУ дочірнього процесу, а не хардкодимо :3000 — Reflex сам падає на інший
порт (:3002 і т.д.), якщо 3000/3001/8000 зайняті (типово — залишки попередніх
запусків чи WSL2/Hyper-V NAT-артефакти на Windows), і хардкод порту тоді
змусить нас чекати URL, який ніколи не зʼявиться.

--dev: класичний браузерний dev-режим без вікна, з devtools (те, що main.py
робив раніше). Альтернатива — run_web.ps1, який кличе reflex.exe напряму,
взагалі минаючи цей файл.
Решта аргументів передається далі в `reflex run` (напр. `--env prod`).
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
APP_URL_RE = re.compile(r"App running at: (http://\S+)")
STARTUP_TIMEOUT = 60.0
# :8000 на цій машині тримає "привид" — порт LISTENING, але PID-власник вже не
# існує (WSL2/Hyper-V NAT-артефакт). Не ліземо в WSL/Docker — просто стартуємо
# на іншому, вільному порту за дефолтом. Перекривається власним --backend-port.
DEFAULT_BACKEND_PORT = 8006

# Геометрія вікна — окремий файл від agent/settings.py (тема/шрифти), щоб два
# процеси (цей лаунчер і Reflex-бекенд) не затирали одне одному ключі: main.py
# піднімає Reflex як ОКРЕМИЙ процес, тож спільний файл без координації між
# процесами — ризик гонки запису.
WINDOW_FILE = Path.home() / ".local-code-agent" / "window.json"
DEFAULT_GEOMETRY = {"width": 1280, "height": 860, "x": None, "y": None}


def _load_geometry() -> dict:
    try:
        d = json.loads(WINDOW_FILE.read_text(encoding="utf-8"))
        geo = {**DEFAULT_GEOMETRY, **d}
        # Захист від сміттєвої збереженої геометрії (напр. зі звуженого/
        # мінімізованого стану) — не менше за min_size вікна.
        geo["width"] = max(800, int(geo.get("width") or DEFAULT_GEOMETRY["width"]))
        geo["height"] = max(600, int(geo.get("height") or DEFAULT_GEOMETRY["height"]))
        return geo
    except Exception:
        return dict(DEFAULT_GEOMETRY)


def _save_geometry(window) -> None:
    try:
        WINDOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_FILE.write_text(json.dumps({
            "width": window.width, "height": window.height,
            "x": window.x, "y": window.y,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run_reflex_inplace(extra_args: list[str]) -> None:
    """Класичний dev-режим (--dev): reflex run у ЦЬОМУ процесі (як раніше)."""
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault("REFLEX_TELEMETRY_ENABLED", "false")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    from reflex.reflex import cli
    sys.argv = ["reflex", "run", *extra_args]
    cli()


OLLAMA_PID_RE = re.compile(r"OLLAMA_SERVER_PID:(\d+)")


def _start_server(extra_args: list[str]) -> tuple[subprocess.Popen, "queue.Queue[str]", dict]:
    """Підняти сервер як дочірній процес (python main.py --dev ...). Фоновий
    потік читає його stdout: друкує кожен рядок (логи лишаються видимі в
    консолі), виловлює рядок "App running at: <url>" із РЕАЛЬНИМ портом і
    рядок "OLLAMA_SERVER_PID:<pid>" (agent.llm._ensure_server) — щоб мати PID
    Ollama-сервера й завершити його при виході (лише якщо ЦЕЙ запуск його й
    підняв; srv_state["ollama_pid"] лишається None, якщо Ollama вже працював)."""
    env = {**os.environ, "REFLEX_TELEMETRY_ENABLED": "false", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--dev", *extra_args],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    url_q: "queue.Queue[str]" = queue.Queue(maxsize=1)
    srv_state: dict = {"ollama_pid": None}
    found = False

    def _pump() -> None:
        nonlocal found
        for line in proc.stdout:
            print(line, end="")
            if not found:
                m = APP_URL_RE.search(line)
                if m:
                    found = True
                    url_q.put(m.group(1))
            m2 = OLLAMA_PID_RE.search(line)
            if m2:
                srv_state["ollama_pid"] = int(m2.group(1))
        if not found:
            url_q.put("")   # процес завершився, не дочекавшись рядка з URL

    threading.Thread(target=_pump, daemon=True).start()
    return proc, url_q, srv_state


def _kill_tree(pid: int) -> None:
    """Завершити процес РАЗОМ з усім його деревом дочірніх процесів (Windows:
    taskkill /T). Звичайний proc.terminate()/kill() вбиває лише ОДИН PID — Reflex
    сам підіймає frontend (Bun/Next.js dev-сервер) як СВІЙ дочірній процес, і він
    лишається осиротілим/висить у Task Manager (живий кейс: процес "Bun" після
    закриття застосунку, навіть коли консоль уже сказала "Process finished")."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _stop_server(proc: subprocess.Popen) -> None:
    _kill_tree(proc.pid)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _stop_ollama(pid: int | None) -> None:
    """Завершити Ollama, але ЛИШЕ якщо ЦЕЙ запуск застосунку сам її підняв (pid
    прийшов через OLLAMA_SERVER_PID у логах бекенда) — якщо Ollama вже працювала
    до старту застосунку, pid тут None і ми її не чіпаємо (могла бути потрібна
    іншим програмам)."""
    if pid:
        _kill_tree(pid)


def main() -> None:
    extra = sys.argv[1:]
    if "--dev" in extra:
        _run_reflex_inplace([a for a in extra if a != "--dev"])
        return

    if "--backend-port" not in extra:
        extra = [*extra, "--backend-port", str(DEFAULT_BACKEND_PORT)]
    from agent import logs
    log = logs.setup("igor-launcher")      # окремий файл: бекенд пише в igor.log
    log.info("запуск, аргументи=%s", extra)
    proc, url_q, srv_state = _start_server(extra)
    try:
        try:
            url = url_q.get(timeout=STARTUP_TIMEOUT)
        except queue.Empty:
            url = ""
        if not url:
            log.error("сервер не повідомив URL за %.0fс (код виходу=%s)", STARTUP_TIMEOUT, proc.poll())
            print(f"Сервер не повідомив URL за {STARTUP_TIMEOUT:.0f}с — дивіться лог вище.")
            return
        log.info("сервер піднявся: %s", url)
        import webview
        geo = _load_geometry()
        # text_select=False — дефолт pywebview, забороняє виділення/копіювання
        # тексту в усьому вікні (живий кейс: текст у чаті неможливо було виділити).
        window = webview.create_window(
            "Ігорь!", url, width=geo["width"], height=geo["height"],
            x=geo.get("x"), y=geo.get("y"), min_size=(800, 600), text_select=True,
        )
        # closing — синхронна подія (блокує закриття до завершення хендлера),
        # тож геометрія читається ДО того, як вікно (і його властивості) зникне.
        window.events.closing += lambda: _save_geometry(window)
        webview.start()
        log.info("вікно закрито користувачем")
    except Exception:
        log.exception("збій лаунчера")
        raise
    finally:
        log.info("зупиняю сервер (код виходу=%s)", proc.poll())
        _stop_server(proc)
        _stop_ollama(srv_state.get("ollama_pid"))


if __name__ == "__main__":
    main()
