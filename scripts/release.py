"""Один крок випуску: збірка -> коміт версії -> перевірка секретів -> push -> публікація релізу.

    python scripts/release.py                 # підняти версію (b_0.0.N+1), зібрати, опублікувати
    python scripts/release.py --notes "текст" # свої нотатки замість списку комітів

Збирає ТІЛЬКИ dist/Igor.exe (інтерпретатором збірного venv) і одразу кладе його в GitHub Release,
звідки програма оновлюється кнопкою «Оновити». Перед push скануються джерела й уся історія на значення з .env.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = Path(r"F:\Python_Local\TestProjects\code-agents-tests\igor-build\venv\Scripts\python.exe")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_release as pr  # noqa: E402

TRAILER = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


def run(*cmd: str) -> None:
    print(">>", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> None:
    if not BUILD_PY.is_file():
        raise SystemExit(f"Нема збірного інтерпретатора: {BUILD_PY}")
    if pr.sh("git", "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("Є незакомічені зміни — закомітьте їх перед випуском.")
    run(str(BUILD_PY), "scripts/build_exe.py", "--bump")
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    run("git", "add", "VERSION")
    run("git", "commit", "-q", "-m", f"Version {ver}\n\n{TRAILER}")
    sec = pr.env_secrets()
    bad = pr.scan_sources(sec) + pr.scan_history(sec)
    if bad:
        raise SystemExit("СЕКРЕТИ — push скасовано:\n • " + "\n • ".join(bad))
    run("git", "push", "origin", "main")
    extra = ["--notes", sys.argv[sys.argv.index("--notes") + 1]] if "--notes" in sys.argv else []
    run(sys.executable, "scripts/publish_release.py", "--go", *extra)
    print(f"\nГОТОВО: реліз {ver} опубліковано — програми оновляться кнопкою «Оновити».")


if __name__ == "__main__":
    main()
