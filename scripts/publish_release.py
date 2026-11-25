"""Публікація релізу на GitHub Releases (звідси «Ігорь!» оновлюється сам).

    python scripts/publish_release.py            # перевірки + показ, що буде опубліковано (нічого не створює)
    python scripts/publish_release.py --go       # створити реліз <версія> з dist/Igor.exe і Igor.exe.sha256
    ... --notes "текст"                          # свій текст нотаток замість списку комітів

Перед публікацією (репозиторій ПУБЛІЧНИЙ — реліз бачать усі):
 1. dist/Igor.exe зібрано з поточної версії (scripts/build_exe.py) і його сума збігається з Igor.exe.sha256;
 2. робоче дерево чисте, а поточний коміт уже на GitHub (тег ставиться на нього);
 3. у джерелах exe немає значень з .env (ключі, адреса сервера) та IP-адрес із нього.
Потрібен `gh` (GitHub CLI), у якому виконано `gh auth login`.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "MagestiUA/AIgor"
SOURCES = ("agent", "lca_web", "assets", "main.py", "rxconfig.py", "VERSION", "requirements.txt")
TEXT_EXT = {".py", ".md", ".txt", ".json", ".toml", ".cfg", ".ini", ".html", ".js", ".css", ".svg", ".yml", ".yaml", ""}


def sh(*cmd: str, check: bool = True) -> str:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode:
        raise SystemExit(f"Команда не вдалась: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


def env_secrets() -> list[str]:
    """Значення з .env, яких не має бути в коді: ключі/токени/адреси/IP (довжина ≥ 6)."""
    f = ROOT / ".env"
    out: list[str] = []
    if f.is_file():
        for line in f.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*)", line)
            if m:
                v = m.group(2).strip().strip("\"'")
                if len(v) >= 6 and not v.lower() in ("true", "false", "openai", "ollama"):
                    out.append(v)
    return out


def scan_sources(secrets: list[str]) -> list[str]:
    hits = []
    files = []
    for s in SOURCES:
        p = ROOT / s
        files += [x for x in p.rglob("*") if x.is_file()] if p.is_dir() else ([p] if p.is_file() else [])
    for f in files:
        if "__pycache__" in f.parts or f.suffix.lower() not in TEXT_EXT:
            continue
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for v in secrets:
            if v in t:
                hits.append(f"{f.relative_to(ROOT)}: містить значення з .env (довжина {len(v)})")
    return hits


def scan_history(secrets: list[str]) -> list[str]:
    """Значення з .env у ВСІЙ історії (усі гілки/теги): публічний репозиторій не має їх містити."""
    hits = []
    for i, v in enumerate(secrets):
        n = len(sh("git", "log", "--all", "-S" + v, "--format=%h", check=False).split())
        if n:
            hits.append(f"історія git: значення #{i} з .env є в {n} комітах")
    return hits


def main() -> None:
    go = "--go" in sys.argv
    ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    exe, shaf = ROOT / "dist" / "Igor.exe", ROOT / "dist" / "Igor.exe.sha256"
    problems = []

    if not exe.is_file() or not shaf.is_file():
        problems.append("нема dist/Igor.exe або Igor.exe.sha256 — спершу python scripts/build_exe.py")
    else:
        real = hashlib.sha256(exe.read_bytes()).hexdigest()
        if real not in shaf.read_text(encoding="ascii"):
            problems.append("сума dist/Igor.exe не збігається з Igor.exe.sha256 — перезберіть")
        mark = ROOT / "dist" / "Igor.version"
        if not mark.is_file() or mark.read_text(encoding="utf-8").strip() != ver:
            problems.append(f"dist/Igor.exe зібрано не з поточної версії {ver} — перезберіть")

    if sh("git", "status", "--porcelain", "--untracked-files=no"):
        problems.append("є незакомічені зміни в відстежуваних файлах")
    head = sh("git", "rev-parse", "HEAD")
    if not sh("git", "branch", "-r", "--contains", head, check=False):
        problems.append("поточний коміт ще не відправлено на GitHub (git push) — тег не буде на що поставити")
    if sh("git", "tag", "--list", ver):
        problems.append(f"тег {ver} уже існує")

    sec = env_secrets()
    hits = scan_sources(sec) + scan_history(sec)
    problems += [f"СЕКРЕТ: {h}" for h in hits]

    prev = sh("git", "describe", "--tags", "--abbrev=0", check=False)
    if "--notes" in sys.argv:                                  # власний текст нотаток (напр. для першого релізу)
        notes = sys.argv[sys.argv.index("--notes") + 1]
    else:
        notes = sh("git", "log", (f"{prev}..HEAD" if prev else "-20"), "--pretty=- %s")
    print(f"Версія: {ver}\nКоміт: {head[:10]}\nПопередній тег: {prev or '—'}\nНотатки релізу:\n{notes}\n")
    if problems:
        print("ПРОБЛЕМИ (реліз не створено):")
        for p in problems:
            print(" •", p)
        raise SystemExit(1)
    if not go:
        print("Усе гаразд. Для публікації: python scripts/publish_release.py --go")
        return
    sh("gh", "release", "create", ver, str(exe), str(shaf), "--repo", REPO, "--target", head,
       "--title", f"Ігорь! {ver}", "--notes", notes or ver)
    print(f"Опубліковано: https://github.com/{REPO}/releases/tag/{ver}")


if __name__ == "__main__":
    main()
