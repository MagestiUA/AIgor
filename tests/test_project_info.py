"""Офлайн-тест agent/project_info.py на тимчасовому git-репозиторії."""
import subprocess
import tempfile
from pathlib import Path

from agent.project_info import git_info


def _g(root, *a):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@l", *a],
                   check=True, capture_output=True)


def main() -> None:
    assert git_info("")["is_repo"] is False
    plain = Path(tempfile.mkdtemp())
    assert git_info(plain)["is_repo"] is False                       # не репозиторій

    fresh = Path(tempfile.mkdtemp())                                 # без жодного коміту
    _g(fresh, "init", "-q", "-b", "main")
    (fresh / "n.txt").write_text("1", encoding="utf-8")
    fi = git_info(fresh)
    assert fi["is_repo"] and fi["branch"] == "main" and [f["path"] for f in fi["files"]] == ["n.txt"], fi

    root = Path(tempfile.mkdtemp())
    _g(root, "init", "-q", "-b", "main")
    (root / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _g(root, "add", "-A"); _g(root, "commit", "-q", "-m", "init")
    clean = git_info(root)
    assert clean["is_repo"] and clean["branch"] == "main" and clean["files"] == [] and clean["add"] == 0

    (root / "a.py").write_text("x = 1\ny = 3\nz = 4\n", encoding="utf-8")   # +2 -1
    (root / "new.txt").write_text("hi\n", encoding="utf-8")                  # untracked
    d = git_info(root)
    byp = {f["path"]: f for f in d["files"]}
    assert byp["a.py"]["add"] == 2 and byp["a.py"]["del"] == 1 and (d["add"], d["del"]) == (2, 1), d
    assert byp["new.txt"].get("new") is True
    many = git_info(root, max_files=1)
    assert len(many["files"]) == 1 and many["more"] == 1
    print("OK: project_info — не-репозиторій, гілка, +/- зміни, untracked, обмеження списку")


if __name__ == "__main__":
    main()
