"""Кеш agent.settings.load(): диск читається лише після зміни файлу; копії незалежні; save() скидає кеш."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from agent import settings as S


def main() -> None:
    f = Path(tempfile.mkdtemp()) / "settings.json"
    with mock.patch.object(S, "SETTINGS_FILE", f):
        S._CACHE["key"] = S._CACHE["val"] = None
        assert S.load()["theme"] == "auto" and not f.exists()                    # немає файлу → дефолти
        S.save({"theme": "dark", "llm_context": "32768"})
        reads = []
        real = Path.read_text

        def counting(self, *a, **k):
            if self == f:
                reads.append(1)
            return real(self, *a, **k)
        with mock.patch.object(Path, "read_text", counting):
            a = S.load()
            b = S.load()
            c = S.load()
            assert a["theme"] == "dark" and a["font_chat"] == 14 and len(reads) == 1, len(reads)   # перше читання, далі кеш
            a["theme"] = "MUTATED"
            assert S.load()["theme"] == "dark" and b == c                                        # копії незалежні від кешу
            S.save({"theme": "light"})
            assert S.load()["theme"] == "light" and len(reads) == 2                               # save() скинув кеш
            f.write_text(json.dumps({"theme": "dark2", "x": 1}), encoding="utf-8")                # зміна ззовні (розмір інший)
            assert S.load()["theme"] == "dark2" and len(reads) == 3
        f.write_text("{зламано", encoding="utf-8")
        assert S.load()["theme"] == "auto"                                                       # зламаний файл → дефолти
    S._CACHE["key"] = S._CACHE["val"] = None
    print("OK: settings cache — читання лише після зміни, незалежні копії, скидання при save")


if __name__ == "__main__":
    main()
