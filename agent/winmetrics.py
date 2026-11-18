"""Діагностика розкладки вікна: фізичні розміри/координати вікна, монітори й DPI (Python/Windows) плюс те, як
сторінка реально розкладається (JS: viewport, DPR, розмір документа, прямокутник кореня, зсув прокрутки).
Пишеться в igor-launcher.log у ключові моменти (старт, показ, розгортання, зміна розміру), щоб за діагностичним
архівом бачити, чому інтерфейс міг зсунутися або не заповнити вікно. Зміст чатів сюди не потрапляє."""
from __future__ import annotations

import json
import sys
import threading

PAGE_JS = """
(() => {
  const d = document.documentElement, b = document.body, vv = window.visualViewport;
  const root = document.querySelector('.fixed.inset-0') || (b && b.firstElementChild);
  const r = root ? root.getBoundingClientRect() : null;
  return JSON.stringify({
    inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight],
    client: [d.clientWidth, d.clientHeight], docScroll: [d.scrollWidth, d.scrollHeight],
    scrollXY: [scrollX, scrollY], dpr: devicePixelRatio,
    screen: [screen.width, screen.height], avail: [screen.availWidth, screen.availHeight],
    visual: vv ? [Math.round(vv.width), Math.round(vv.height), vv.scale, vv.offsetLeft, vv.offsetTop] : null,
    root: r ? [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] : null,
    fontPx: getComputedStyle(d).fontSize, ready: document.readyState,
  });
})()
"""


def system_info() -> dict:
    """Монітори й DPI з Windows (None-поля, якщо недоступно)."""
    info: dict = {"platform": sys.platform}
    if sys.platform != "win32":
        return info
    try:
        import ctypes
        u = ctypes.windll.user32
        try:
            ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(ctypes.c_int()))
        except Exception:                                   # noqa: BLE001
            pass
        info["primary"] = [u.GetSystemMetrics(0), u.GetSystemMetrics(1)]
        info["virtual"] = [u.GetSystemMetrics(78), u.GetSystemMetrics(79)]     # SM_CXVIRTUALSCREEN/CY
        info["monitors"] = u.GetSystemMetrics(80)                              # SM_CMONITORS
        try:
            info["dpi_system"] = u.GetDpiForSystem()
        except Exception:                                   # noqa: BLE001
            info["dpi_system"] = None
    except Exception as e:                                  # noqa: BLE001
        info["error"] = str(e)
    return info


def window_info(window) -> dict:
    out = {}
    for k in ("width", "height", "x", "y", "maximized", "on_top"):
        try:
            out[k] = getattr(window, k)
        except Exception:                                   # noqa: BLE001
            out[k] = None
    try:
        import webview
        out["screens"] = [{"x": s.x, "y": s.y, "w": s.width, "h": s.height} for s in webview.screens]
    except Exception:                                       # noqa: BLE001
        pass
    return out


def page_info(window) -> dict:
    try:
        raw = window.evaluate_js(PAGE_JS)
        return json.loads(raw) if raw else {"error": "порожня відповідь JS"}
    except Exception as e:                                  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def layout_problem(page: dict) -> str:
    """Короткий опис невідповідності розкладки (порожньо — усе гаразд)."""
    try:
        iw, ih = page["inner"]
        sw, sh = page["docScroll"]
        problems = []
        if sw > iw + 1 or sh > ih + 1:
            problems.append(f"документ {sw}x{sh} більший за viewport {iw}x{ih}")
        root = page.get("root")
        if root and root[3] == 0 and page.get("ready") != "complete":
            return ""                                       # інтерфейс ще не змонтовано — не помилка
        if root and (abs(root[0]) > 1 or abs(root[1]) > 1 or abs(root[2] - iw) > 2 or abs(root[3] - ih) > 2):
            problems.append(f"корінь {root} не збігається з viewport {iw}x{ih}")
        if page.get("scrollXY") and any(page["scrollXY"]):
            problems.append(f"сторінка прокручена {page['scrollXY']}")
        return "; ".join(problems)
    except Exception:                                       # noqa: BLE001
        return ""


def log_snapshot(window, log, reason: str) -> None:
    page = page_info(window)
    win = window_info(window)
    log.info("вікно[%s] win=%s page=%s", reason, json.dumps(win, ensure_ascii=False),
             json.dumps(page, ensure_ascii=False))
    bad = layout_problem(page)
    if bad:
        log.warning("розкладка вікна[%s] НЕ збігається: %s", reason, bad)


class Debounced:
    """Зводить серію подій resize у один знімок через delay секунд після останньої."""

    def __init__(self, fn, delay: float = 0.6):
        self.fn, self.delay, self._t = fn, delay, None

    def __call__(self, reason: str) -> None:
        if self._t:
            self._t.cancel()
        self._t = threading.Timer(self.delay, self.fn, args=(reason,))
        self._t.daemon = True
        self._t.start()
