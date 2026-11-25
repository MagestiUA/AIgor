"""Константи й профілі агента.

Профілі ролей — лише РЕЖИМ виклику (думання, температура, семплінг), а не модель: і локально (Ollama), і на сервері
працює ОДНА модель на всі ролі, а вікно (num_ctx) береться з limits — однакові правила для обох бекендів.
(Раніше профілі несли ще й власну модель і num_ctx — з часів gemma4 + Qwen3-Coder; у режимі однієї моделі ці поля
ігнорувались і були лише шумом — docs/uk/CRUTCHES.md B5.)
  EXECUTOR      — виконання (tool-loop, JSON-рішення планувальника): думання ВИМКНЕНО, DRY проти повторів.
  PLANNER       — аналіз і відповіді (чат/аналіз коду): думання УВІМКНЕНО.
  CHAT_EXECUTOR — синонім EXECUTOR (лишився для сумісності викликів).
"""
from __future__ import annotations

HOST  = "http://127.0.0.1:11434"

# Локальний режим застосунку: ОДНА модель на всі ролі й власний розмір вікна (за бенчмарком, див. LOCAL_CTX нижче).
LOCAL_MODEL = "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL"   # те саме сімейство, що на сервері (Qwen3.8-27B)
LOCAL_CTX = 65536
MODEL = LOCAL_MODEL               # дефолт OllamaClient (скрипти, тести); застосунок бере модель із налаштувань
# Швидкий вибір у налаштуваннях: (назва, модель Ollama, вікно). Uncensored — та сама вага, що на сервері, але на 16 ГБ
# частина шарів іде на CPU, тому вікно менше (замір: 24k ≈ 28 ток/с, 64k ≈ 15–20).
LOCAL_PRESETS = (
    ("Qwen3.8-27B (швидка, 64k)", LOCAL_MODEL, LOCAL_CTX),
    ("Qwen3.8-27B Uncensored (як на сервері, 24k)", "orcarouter/Qwen3.8-27B-Uncensored:q3_K_L", 24576),
)

REQUEST_TIMEOUT = 600

def load_dotenv(path=None) -> None:
    """Мінімальний .env-лоадер (без залежності): KEY=VALUE, '#'-коментарі, опційні
    лапки/`export`. Справжні змінні середовища мають пріоритет (setdefault) — .env
    лише доповнює. Файл — у корені репо (поруч з main.py), у git не потрапляє."""
    from pathlib import Path
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    try:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            _os.environ.setdefault(k, v)


# Бекенд LLM: "ollama" (локально, дефолт) | "openai" (орендований vLLM на H100,
# OpenAI-сумісний API; клієнт — llm_openai.OpenAIClient). Перемикання: env LCA_BACKEND.
# При "openai" профілі нижче лишаються (think/temperature працюють), а їхні
# ollama-специфічні поля (model, num_ctx, DRY) ігноруються — модель одна: REMOTE_MODEL.
# Ключ — з env LLM_API_KEY (у код/репо НЕ кладемо).
import os as _os
load_dotenv()       # .env може містити й LCA_BACKEND, і LLM_API_KEY — читаємо ДО них
BACKEND = _os.environ.get("LCA_BACKEND", "ollama").lower()
# Адреса сервера НЕ вшита в код (репозиторій публічний): її задають у Налаштуваннях застосунку
# або змінною LCA_REMOTE_URL (напр. в .env: LCA_REMOTE_URL=http://host:8000/v1).
REMOTE_BASE_URL = _os.environ.get("LCA_REMOTE_URL", "")
REMOTE_MODEL = _os.environ.get("LCA_REMOTE_MODEL", "Qwen3.8-27B-Uncensored")
REMOTE_KEY_ENV = "LLM_API_KEY"

# run_shell allow-list: дозволені префікси команд (точний токен або "<prefix> ...").
# Усе інше блокується БЕЗ виконання. Виконуємо через shell=False (без ланцюжків).
ALLOWED_SHELL = (
    # Python / pip
    "python", "py",
    "pip install", "pip uninstall", "pip list", "pip show", "pip freeze",
    # linters / tests / formatters
    "pytest", "ruff", "flake8", "mypy", "black", "isort",
    # git (безпечні операції)
    "git status", "git diff", "git log", "git show",
    "git add", "git commit", "git pull", "git push",
    "git checkout", "git branch", "git merge", "git stash",
    "git init", "git clone",
    # інші мови / пакетні менеджери
    "npm", "node", "npx",
    "cargo", "rustc",
    "go",
)
SHELL_TIMEOUT = 120

# DRY-семплінг (Don't Repeat Yourself) — лише для EXECUTOR (тул-цикл): карає повтор
# цілих ФРАЗ/послідовностей (напр. та сама невдала git-команда раз за разом), не
# окремих токенів — тож не псує код, як грубий repeat_penalty. М'який multiplier
# (емпірично 1.8 repeat_penalty ламав слова; DRY 0.3 зберігає зв'язність). Виклики з
# format=schema захищені граматикою — DRY не зробить JSON невалідним. PLANNER (проза,
# think=on) лишаємо без DRY. options мерджаться в payload у llm.chat/chat_stream.
_DRY = {"dry_multiplier": 0.3, "dry_base": 1.75, "dry_allowed_length": 2}

# Профілі ролей: режим виклику (модель і вікно — спільні, див. docstring)
EXECUTOR = {"think": False, "temperature": 0.2, "options": _DRY}
PLANNER  = {"think": True, "temperature": 0.3}
CHAT_EXECUTOR = EXECUTOR