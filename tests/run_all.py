"""Єдиний запуск усіх тестів: кожен tests/test_*.py окремим процесом (вони самодостатні скрипти).

  python tests/run_all.py            # усі, послідовно
  python tests/run_all.py -j 4       # паралельно (є тести з файлами/портами — за збою перезапустіть без -j)
  python tests/run_all.py loop sub   # лише тести, у назві яких є підрядок

test_settings.py — pytest-тест: запускається через `python -m pytest`, якщо pytest є, інакше пропускається.
Код виходу 0 — усе зелене. Тести ніколи не чіпають реальний профіль (див. test isolation у README розробника).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent


def run_one(path: Path, timeout: int) -> tuple[str, str, float, str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    t0 = time.time()
    if path.name == "test_settings.py":
        try:
            import pytest  # noqa: F401
        except ImportError:
            return path.stem, "SKIP", 0.0, "pytest не встановлено"
        cmd = [sys.executable, "-m", "pytest", "-q", str(path)]
    else:
        cmd = [sys.executable, str(path)]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return path.stem, "FAIL", time.time() - t0, f"таймаут {timeout} с"
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
    return path.stem, "ok" if r.returncode == 0 else "FAIL", time.time() - t0, tail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("filters", nargs="*", help="підрядки назв тестів")
    ap.add_argument("-j", type=int, default=1, help="скільки тестів паралельно")
    ap.add_argument("--timeout", type=int, default=240)
    a = ap.parse_args()
    files = sorted(p for p in TESTS.glob("test_*.py") if not a.filters or any(f in p.stem for f in a.filters))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.j)) as ex:
        results = list(ex.map(lambda p: run_one(p, a.timeout), files))
    bad = [r for r in results if r[1] == "FAIL"]
    for name, status, secs, tail in results:
        if status != "ok":
            print(f"{status:5} {name} ({secs:.0f} с)" + (f"\n      " + tail.replace("\n", "\n      ") if status == "FAIL" else f": {tail}"))
    skipped = sum(1 for r in results if r[1] == "SKIP")
    print(f"\n{len(results) - len(bad) - skipped} ok, {len(bad)} FAIL, {skipped} пропущено — {time.time() - t0:.0f} с")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
