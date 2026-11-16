"""Офлайн-тест config.load_dotenv: парсинг, лапки/export/коментарі, пріоритет
справжнього env. Тільки тимчасовий файл; змінні env відновлюються."""
import os
import tempfile
from pathlib import Path

from agent import config

KEYS = ("T_PLAIN", "T_QUOTED", "T_EXPORT", "T_KEEP", "T_EMPTY")


def main() -> None:
    saved = {k: os.environ.pop(k, None) for k in KEYS}
    d = Path(tempfile.mkdtemp())
    try:
        f = d / ".env"
        f.write_text('﻿# коментар\nT_PLAIN=abc\nT_QUOTED="q v"\nexport T_EXPORT = \'e\'\n'
                     'T_KEEP=з_файлу\nT_EMPTY=\nbad line\n', encoding="utf-8")
        os.environ["T_KEEP"] = "з_середовища"
        config.load_dotenv(f)
        assert os.environ["T_PLAIN"] == "abc"
        assert os.environ["T_QUOTED"] == "q v" and os.environ["T_EXPORT"] == "e"
        assert os.environ["T_KEEP"] == "з_середовища"      # справжній env не перетираємо
        assert os.environ["T_EMPTY"] == ""
        config.load_dotenv(d / "нема.env")                   # відсутній файл — тихо
        print("OK: load_dotenv — парсинг, лапки/export/BOM, пріоритет env, відсутній файл")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


if __name__ == "__main__":
    main()
