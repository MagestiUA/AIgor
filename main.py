"""Точка входу Ігоря (local-code-agent): піднімає сервер і відкриває застосунок у
нативному вікні (pywebview, WebView2 на Windows) — без вкладки браузера.

Зручно з PyCharm: відкрий цей файл і натисни ▶ Run (інтерпретатор — .venv проєкту).

Режими:
  (за замовчуванням)  dev: `reflex run` у дочірньому процесі + вікно. Реальний URL
                      читаємо З ВИВОДУ дочірнього процесу (Reflex сам падає на інший порт,
                      якщо 3000/3001/8000 зайняті). Потребує Node/Bun (Reflex ставить сам).
  --prod              БЕЗ Node/Bun: зібраний `reflex export` фронтенд віддає бекенд на
                      вільному порту (agent/serve.py). Це ж режим готового .exe.
  --prod --no-window  те саме, але без вікна: друкує URL (для перевірок/тестів).
  --dev               класичний браузерний dev-режим у цьому процесі (devtools).
  --serve <port>      ВНУТРІШНІЙ: бекенд зі статикою (його запускає лаунчер як дочірній процес).

Сервер — ОКРЕМИЙ ПРОЦЕС, не потік: Granian/Reflex реєструє signal-handler, що працює
лише в головному потоці головного інтерпретатора ("signal only works in main thread").
Решта аргументів dev-режиму передається далі в `reflex run`.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
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


def _start_proc(cmd: list[str], cwd: str | None = None) -> tuple[subprocess.Popen, "queue.Queue[str]", dict]:
    """Підняти дочірній процес. Фоновий потік читає його stdout: друкує кожен рядок
    (лог лишається видимий у консолі), пише його в igor-server.log, виловлює рядок
    "App running at: <url>" (dev) і "OLLAMA_SERVER_PID:<pid>" (agent.llm._ensure_server) —
    щоб завершити Ollama при виході, але лише якщо ЦЕЙ запуск її й підняв."""
    env = {**os.environ, "REFLEX_TELEMETRY_ENABLED": "false", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    url_q: "queue.Queue[str]" = queue.Queue(maxsize=1)
    srv_state: dict = {"ollama_pid": None}
    found = False
    try:
        from agent import logs as _logs
        srv_log = _logs.raw_file_logger("igor-server")
    except Exception:
        srv_log = None

    def _pump() -> None:
        nonlocal found
        for line in proc.stdout:
            try:
                print(line, end="")
            except Exception:                     # консолі може не бути (windowed .exe)
                pass
            if srv_log:
                srv_log.info(line.rstrip())
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


def _start_server(extra_args: list[str]):
    """dev: `python main.py --dev ...` у дочірньому процесі."""
    return _start_proc([sys.executable, str(ROOT / "main.py"), "--dev", *extra_args], cwd=str(ROOT))


def _kill_tree(pid: int) -> None:
    """Завершити процес РАЗОМ з усім його деревом дочірніх процесів (Windows:
    taskkill /T). Звичайний proc.terminate()/kill() вбиває лише ОДИН PID — Reflex
    сам підіймає frontend (Bun/Next.js dev-сервер) як СВІЙ дочірній процес, і він
    лишається осиротілим/висить у Task Manager (живий кейс: процес "Bun" після
    закриття застосунку, навіть коли консоль уже сказала "Process finished")."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ping(port: int, proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT) -> bool:
    """Чекати, поки бекенд відповість на /ping (або процес помре)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _open_window(url: str, log) -> None:
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


def _show_error(text: str) -> None:
    """Повідомлення користувачеві, коли консолі немає (.exe без консолі)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Ігорь!", 0x10)
    except Exception:
        print(text)


def _run_prod(no_window: bool) -> None:
    """Режим без Node/Bun (і готового .exe): бекенд зі статикою на вільному порту."""
    from agent import logs
    log = logs.setup("igor-launcher")
    port = _free_port()
    cmd = ([sys.executable, "--serve", str(port)] if FROZEN
           else [sys.executable, str(ROOT / "main.py"), "--serve", str(port)])
    log.info("запуск (prod), порт=%d, frozen=%s", port, FROZEN)
    proc, _q, srv_state = _start_proc(cmd, cwd=None if FROZEN else str(ROOT))
    try:
        if not _wait_ping(port, proc):
            log.error("бекенд не відповів за %.0fс (код виходу=%s)", STARTUP_TIMEOUT, proc.poll())
            _show_error("Сервер Ігоря не запустився. Логи: %USERPROFILE%\\.local-code-agent\\logs\\ "
                        "(igor-server.log, igor-launcher.log).")
            return
        url = f"http://127.0.0.1:{port}"
        log.info("сервер піднявся: %s", url)
        if no_window:
            print(f"URL: {url}", flush=True)
            proc.wait()
        else:
            _open_window(url, log)
    except Exception:
        log.exception("збій лаунчера")
        raise
    finally:
        log.info("зупиняю сервер (код виходу=%s)", proc.poll())
        _stop_server(proc)
        _stop_ollama(srv_state.get("ollama_pid"))


def main() -> None:
    extra = sys.argv[1:]
    if "--serve" in extra:                          # внутрішній режим: бекенд зі статикою
        from agent import serve
        serve.serve(int(extra[extra.index("--serve") + 1]))
        return
    if "--dev" in extra:
        _run_reflex_inplace([a for a in extra if a != "--dev"])
        return
    if FROZEN or "--prod" in extra:
        _run_prod(no_window="--no-window" in extra)
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
        _open_window(url, log)
    except Exception:
        log.exception("збій лаунчера")
        raise
    finally:
        log.info("зупиняю сервер (код виходу=%s)", proc.poll())
        _stop_server(proc)
        _stop_ollama(srv_state.get("ollama_pid"))


if __name__ == "__main__":
    multiprocessing.freeze_support()     # обов'язково для .exe: воркери Granian запускаються через spawn
    main()
