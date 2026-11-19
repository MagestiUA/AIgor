import copy
import json
import os
from pathlib import Path

# LCA_SETTINGS_FILE — окремий профіль налаштувань (тести, локальна модель) без зачіпання основного файлу
SETTINGS_FILE = Path(os.environ.get("LCA_SETTINGS_FILE") or Path.home() / ".local-code-agent" / "settings.json")
DEFAULT = {
    "theme": "auto",
    "font_chat": 14,
    "font_ui": 13,
    "last_session_id": "",     # відновлюємо останній відкритий чат після перезапуску
    # Підключення до LLM (див. agent/backend.py). Порожнє = дефолт із .env/config.
    "llm_backend": "",         # "" | "ollama" | "openai"
    "llm_host": "",
    "llm_port": "",
    "llm_api_key": "",         # лежить у файлі користувача відкритим текстом
    "llm_model": "",           # "" = перша модель із /v1/models
    "llm_context": "",         # розмір вікна моделі в токенах (локальна модель на «серверному» профілі); "" = 262144
    # «Милиці» для слабкої/нестандартної локальної моделі (agent/limits.py: просочені виклики тулів, nudge, повтор при
    # self-talk). Для Qwen3.8 і сервера вимкнено — вони там шкодять; вмикати лише для інших локальних моделей.
    "weak_model_fixes": False,
    # Навантаження GPU сервера (agent/gpumon.py): за замовчуванням увімкнено; недоступні дані — без графіка
    "gpu_monitor": True,
    "gpu_monitor_port": "8001",
}

# Кеш розібраного файлу: load() викликають десятки разів (limits.get() у циклах, обчислювані поля панелі) —
# читаємо диск лише коли файл змінився (ключ: шлях + mtime + розмір).
_CACHE: dict = {"key": None, "val": None}


def _key():
    try:
        st = SETTINGS_FILE.stat()
    except OSError:
        return None
    return (str(SETTINGS_FILE), st.st_mtime_ns, st.st_size)


def load() -> dict:
    """Завантажити налаштування; відсутній файл або невалідний → defaults."""
    key = _key()
    if key is None:
        return copy.deepcopy(DEFAULT)
    if _CACHE["key"] == key and _CACHE["val"] is not None:
        return copy.deepcopy(_CACHE["val"])
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        # Об'єднуємо дефолтні значення з завантаженими, зберігаючи всі ключі з файлу
        result = copy.deepcopy(DEFAULT)
        result.update(data)
    except Exception:
        return copy.deepcopy(DEFAULT)
    _CACHE["key"], _CACHE["val"] = key, result
    return copy.deepcopy(result)


def save(d: dict) -> None:
    """Зберегти словник налаштувань у файл."""
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Зберігаємо весь словник, щоб не втрачати додаткові (екстеншн) ключі
        SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    _CACHE["key"] = _CACHE["val"] = None            # наступний load() перечитає файл
