"""edit_file/executor: для НЕ-Python файлів (Dockerfile, YAML) немає ast-розбору й strip літералів
(регресія: «invalid syntax (<unknown>, line 1)» на Dockerfile/docker-compose.yml); для .py перевірка лишається.
Модель підмінено, файли — у тимчасовій теці."""
import tempfile
from pathlib import Path

from agent import executor


class FakeClient:
    def __init__(self, answer: str):
        self.answer, self.last = answer, None

    def chat(self, messages, profile=None, **kw):
        self.last = messages
        return {"content": self.answer}


def main() -> None:
    d = Path(tempfile.mkdtemp())
    (d / "Dockerfile").write_text('FROM python:3.12\n\nCMD ["python", "main.py"]\n', encoding="utf-8")
    (d / "compose.yml").write_text("services:\n  app:\n    image: x\n", encoding="utf-8")
    (d / "m.py").write_text("x = 1\n", encoding="utf-8")

    # Dockerfile без розширення, відповідь у ```-блоці з довільною мовою
    c = FakeClient('```dockerfile\nFROM python:3.12\n\nEXPOSE 8080\nCMD ["python", "main.py"]\n```')
    r = executor.run_edit(d / "Dockerfile", "додай EXPOSE 8080", client=c, write=True)
    assert r.ok, r.error
    assert "EXPOSE 8080" in (d / "Dockerfile").read_text(encoding="utf-8")
    assert "```text" in c.last[1]["content"], c.last[1]["content"][:200]

    # YAML: не-python синтаксис (двокрапки) не має валитися в ast
    c = FakeClient("```yaml\nservices:\n  app:\n    image: x\n    ports:\n      - \"8080:8080\"\n```")
    r = executor.run_edit(d / "compose.yml", "додай ports", client=c, write=True)
    assert r.ok, r.error
    assert '"8080:8080"' in (d / "compose.yml").read_text(encoding="utf-8")
    assert "```yml" in c.last[1]["content"]

    # .py: невалідний вивід моделі й далі відхиляється, файл не змінюється
    c = FakeClient("```python\ndef broken(:\n```")
    r = executor.run_edit(d / "m.py", "зламай", client=c, write=True)
    assert not r.ok and "невалідний" in r.error
    assert (d / "m.py").read_text(encoding="utf-8") == "x = 1\n"
    print("OK: executor — не-Python файли правляться без ast, .py лишається під перевіркою")


if __name__ == "__main__":
    main()
