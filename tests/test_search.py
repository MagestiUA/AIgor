"""Тул search (agent/search.py): пошук рядків замість grep у консолі. Усе — у тимчасовій теці."""
import tempfile
from pathlib import Path

from agent import search as S


def main() -> None:
    d = Path(tempfile.mkdtemp())
    (d / "src").mkdir()
    (d / "src" / "a.py").write_text("import os\n\ndef handler(req):\n    return req\n", encoding="utf-8")
    (d / "src" / "b.ts").write_text("export function handler() {}\n", encoding="utf-8")
    (d / "node_modules" / "lib").mkdir(parents=True)
    (d / "node_modules" / "lib" / "x.js").write_text("function handler() {}\n", encoding="utf-8")     # службова тека
    (d / "img.bin").write_bytes(b"\x00\x01handler\x00")                                              # бінарний
    (d / "many.txt").write_text("".join(f"hit {i}\n" for i in range(700)), encoding="utf-8")

    out = S.search(d, "handler", rel_to=d)
    assert "src/a.py:3: def handler(req):" in out and "src/b.ts:1:" in out, out
    assert "node_modules" not in out and "img.bin" not in out
    assert out.startswith("Знайдено 2 збігів у 2 файлах")

    assert "b.ts" not in S.search(d, "handler", glob="*.py", rel_to=d)
    assert "a.py" not in S.search(d, "handler", glob="src/*.ts", rel_to=d)
    assert "src/a.py:3:" in S.search(d / "src" / "a.py", "handler", rel_to=d)                     # один файл
    assert "не знайдено" in S.search(d, "HANDLER", rel_to=d)                                       # є великі → з регістром
    assert "src/a.py:3:" in S.search(d, "HANDLER", ignore_case=True, rel_to=d)                    # примусово без регістру
    assert "src/a.py:3:" in S.search(d, r"^def \w+\(", rel_to=d)                                   # регулярний вираз

    big = S.search(d, "hit", rel_to=d)
    assert "показано перші 60" in big and "≥500" in big and big.count("\n") == 60, big[:120]
    assert S.search(d, "", rel_to=d).startswith("помилка") and S.search(d, "   ", rel_to=d).startswith("помилка")
    assert "не знайдено" in S.search(d, "нема-такого", rel_to=d)
    print("OK: search — файл:рядок, службові/бінарні пропущено, glob, регістр, regex, ліміт збігів")


if __name__ == "__main__":
    main()
