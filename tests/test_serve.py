"""agent/serve.py: підстановка порту в зібраний фронтенд (без запуску сервера).
Усе у тимчасових теках; ~/.local-code-agent/runtime не чіпаємо."""
import os
import tempfile
import time
from pathlib import Path

from agent import serve


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "client"
    (src / "assets").mkdir(parents=True)
    (src / "index.html").write_text(f"<script>fetch('http://{serve.SENTINEL_HOST}/ping')</script>", encoding="utf-8")
    (src / "assets" / "app.js").write_text(f'const EVENT="ws://{serve.SENTINEL_HOST}/_event";', encoding="utf-8")
    (src / "assets" / "app.js.gz").write_bytes(bytes([31, 139]) + b"-stale-gzip")      # має зникнути
    (src / "logo.png").write_bytes(bytes([137]) + b"PNG" + serve.SENTINEL_HOST.encode())
    rx = tmp / "rxconfig.py"; rx.write_text("# cfg", encoding="utf-8")
    assets = tmp / "assets_src"; assets.mkdir(); (assets / "a.txt").write_text("a", encoding="utf-8")
    base = tmp / "runtime"
    app = tmp / "app_pkg"; (app / "__pycache__").mkdir(parents=True)
    (app / "lca_web.py").write_text("app = 1", encoding="utf-8"); (app / "__pycache__" / "x.pyc").write_bytes(b"x")

    rt = serve.prepare_runtime(8123, src, rx, assets, base=base, app_src=app)
    cl = rt / ".web" / "build" / "client"
    assert rt == base / "p8123"
    html = (cl / "index.html").read_text(encoding="utf-8")
    assert "127.0.0.1:8123/ping" in html and serve.SENTINEL_HOST not in html
    assert 'ws://127.0.0.1:8123/_event' in (cl / "assets" / "app.js").read_text(encoding="utf-8")
    assert not (cl / "assets" / "app.js.gz").exists(), "застарілі .gz мають бути видалені"
    assert serve.SENTINEL_HOST.encode() in (cl / "logo.png").read_bytes()   # бінарні не чіпаємо
    assert (rt / "rxconfig.py").read_text(encoding="utf-8") == "# cfg"
    assert (rt / "assets" / "a.txt").exists()
    assert (rt / "lca_web" / "lca_web.py").is_file() and not (rt / "lca_web" / "__pycache__").exists()
    # оригінал не змінено
    assert serve.SENTINEL_HOST in (src / "index.html").read_text(encoding="utf-8")

    # повторний запуск з іншим портом не ламає перший; старі теки (>2 діб) чистяться
    rt2 = serve.prepare_runtime(8124, src, rx, base=base)
    assert (rt / "rxconfig.py").exists() and "127.0.0.1:8124" in (rt2 / ".web/build/client/index.html").read_text(encoding="utf-8")
    old = base / "p9999"; old.mkdir()
    past = time.time() - 3 * 86400
    os.utime(old, (past, past))
    serve.prepare_runtime(8125, src, rx, base=base)
    assert not old.exists() and rt.exists()
    print("OK: serve — підстановка порту, .gz видалено, бінарні не чіпаємо, оригінал цілий, чистка старих")


if __name__ == "__main__":
    main()
