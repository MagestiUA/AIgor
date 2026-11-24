"""Обробка файлів, прикріплених користувачем до повідомлення.

Архітектура (файли на диску):
  1. Користувач прикріплює файли → валідуються (classify/process) і ЗБЕРІГАЮТЬСЯ
     на диск у теці сесії: ~/.local-code-agent/attachments/<session_id>/.
  2. Моделі в контекст передається лише ШЛЯХ до теки + список імен (attachments_note),
     БЕЗ вмісту. Модель сама читає потрібні файли через тул read_file/list_dir.
  3. Чистка — ручна (видалення чіпа = видалення файлу з диска); тека сесії живе,
     поки користувач її не прибере.

classify()           — текст чи бінар.
process()            — декодування + обрізання до бюджету (вміст у `_content`).
session_dir()        — тека вкладень сесії на диску.
save()/remove()/clear()/rename_session() — операції з файлами на диску.
list_saved()         — метадані файлів, що вже лежать у теці (для відновлення chips).
attachments_note()   — шлях до теки + список повних шляхів для контексту моделі.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import images, limits

MAX_FILES = 20                  # максимальна кількість вкладень
THUMBS = ".thumbs"              # підтека з мініатюрами зображень (не вкладення)
# Розмір файлу й скільки символів зберігаємо — limits.get().max_file / file_chars (залежать від вікна моделі).

# Тека вкладень — у профілі користувача (~/.local-code-agent/attachments/<sid>/): у зібраній exe код лежить у
# тимчасовій теці розпакування, яку видаляють на виході, — вкладення (тим паче зображення з історії чату)
# мусять переживати перезапуск. Не в теці проєкту, тож Reflex dev-watcher не перезапускається при записі.
ATTACHMENTS_ROOT = Path.home() / ".local-code-agent" / "attachments"
LEGACY_ROOT = Path(__file__).resolve().parent.parent / ".attachments"     # старе місце (у репозиторії)

TEXT_EXTENSIONS = {
    ".py", ".pyw", ".pyi",
    ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".md", ".txt", ".rst", ".log", ".csv",
    ".sh", ".bash", ".zsh", ".bat", ".ps1", ".cmd",
    ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp",
    ".sql", ".xml", ".svg",
    ".gitignore", ".dockerignore",
}


def classify(filename: str, data: bytes) -> str:
    """Повернути "text" якщо файл текстовий, інакше "binary"."""
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXTENSIONS or ext == "":
        try:
            data.decode("utf-8")
            return "text"
        except (UnicodeDecodeError, ValueError):
            pass
    return "binary"


def process(filename: str, data: bytes) -> dict:
    """Валідувати й декодувати завантажений файл.
    Повертає dict: {name, size, truncated, error, _content}. Поле `_content`
    (обрізаний до limits.file_chars текст) caller дістає й пише на диск через save();
    при помилці (`error` != "") поля `_content` немає.
    """
    size = len(data)
    lim = limits.get()
    if size > lim.max_file:
        kb = size // 1024
        return {"name": filename, "size": size, "truncated": False,
                "error": f"завеликий ({kb} KB > {lim.max_file // 1024} KB)"}
    if classify(filename, data) == "binary":
        return {"name": filename, "size": size, "truncated": False,
                "error": "бінарний файл (підтримуються лише текстові)"}
    content = data.decode("utf-8", errors="replace")
    truncated = len(content) > lim.file_chars
    return {"name": filename, "size": size, "truncated": truncated,
            "error": "", "_content": content[:lim.file_chars]}


def _safe_name(name: str) -> str:
    """Тільки базове ім'я файлу — захист від traversal (../, абсолютні шляхи)."""
    return Path(name).name


def session_dir(session_id: str) -> Path:
    """Тека вкладень для сесії на диску."""
    return ATTACHMENTS_ROOT / session_id


def save(session_id: str, name: str, content: str) -> Path:
    """Зберегти текстовий вміст файлу в теку сесії. Повертає шлях до файлу."""
    d = session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe_name(name)
    p.write_text(content, encoding="utf-8")
    return p


def save_bytes(session_id: str, name: str, data: bytes, thumb_url: str = "") -> Path:
    """Зберегти бінарний вміст (зображення) у теку сесії; мініатюру (data-URL) — у .thumbs/ для чіпів."""
    d = session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe_name(name)
    p.write_bytes(data)
    if thumb_url:
        mime, b64 = images.split_data_url(thumb_url)
        if b64:
            (d / THUMBS).mkdir(exist_ok=True)
            (d / THUMBS / (_safe_name(name) + ".thumb")).write_text(thumb_url, encoding="ascii")
    return p


def read_thumb(session_id: str, name: str) -> str:
    try:
        return (session_dir(session_id) / THUMBS / (_safe_name(name) + ".thumb")).read_text(encoding="ascii")
    except OSError:
        return ""


def remove(session_id: str, name: str) -> None:
    """Видалити один файл вкладення з диска (ручна чистка користувачем)."""
    for p in (session_dir(session_id) / _safe_name(name),
              session_dir(session_id) / THUMBS / (_safe_name(name) + ".thumb")):
        try:
            p.unlink()
        except OSError:
            pass


def clear(session_id: str) -> None:
    """Видалити всю теку вкладень сесії (напр. при видаленні чату)."""
    shutil.rmtree(session_dir(session_id), ignore_errors=True)


def migrate_legacy() -> int:
    """Скопіювати вкладення зі старого місця (<репозиторій>/.attachments) у профіль користувача (лише відсутні там).
    КОПІЮЄМО, а не переносимо: оригінали лишаються недоторканими (запуск з іншим профілем/тестовим середовищем
    не може «забрати» чужі дані). Викликається при старті застосунку (не при імпорті — тести не чіпають реальні
    теки). -> скільки сесій скопійовано."""
    n = 0
    if not LEGACY_ROOT.is_dir() or LEGACY_ROOT.resolve() == ATTACHMENTS_ROOT.resolve():
        return 0
    ATTACHMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    for d in LEGACY_ROOT.iterdir():
        if d.is_dir() and not (ATTACHMENTS_ROOT / d.name).exists():
            try:
                shutil.copytree(d, ATTACHMENTS_ROOT / d.name)
                n += 1
            except OSError:
                shutil.rmtree(ATTACHMENTS_ROOT / d.name, ignore_errors=True)     # не лишаємо напівскопійовану теку
    return n


def rename_session(old_id: str, new_id: str) -> None:
    """Перенести теку вкладень old_id → new_id (міграція пре-сесійного `_tmp`)."""
    src = session_dir(old_id)
    if not src.is_dir():
        return
    dst = session_dir(new_id)
    if dst.exists():
        for f in src.iterdir():                 # злити: перенести файли (і теку .thumbs)
            target = dst / f.name
            if f.is_dir():
                target.mkdir(exist_ok=True)
                for g in f.iterdir():
                    g.replace(target / g.name)
            else:
                f.replace(target)
        shutil.rmtree(src, ignore_errors=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)


def list_saved(session_id: str) -> list[dict]:
    """Метадані файлів, що лежать у теці сесії (для відновлення chips при виборі сесії).
    На диску лежать лише валідні файли, тож error завжди ''. Для зображень image=True і thumb (data-URL)."""
    d = session_dir(session_id)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file():
            img = images.is_image_name(p.name)
            out.append({"name": p.name, "size": p.stat().st_size, "truncated": False, "error": "",
                        "image": img, "thumb": read_thumb(session_id, p.name) if img else ""})
    return out


def list_text(session_id: str) -> list[dict]:
    """Лише текстові вкладення (їх читає модель тулами/вшиванням); зображення йдуть окремо — як картинки."""
    return [a for a in list_saved(session_id) if not a.get("image")]


def total_size(session_id: str, names: list[str]) -> int:
    """Сумарний розмір (байти) перелічених вкладень сесії."""
    sizes = {n["name"]: n["size"] for n in list_saved(session_id)}
    return sum(sizes.get(n, 0) for n in names)


# Якщо ВСІ прикріплені до повідомлення файли РАЗОМ вкладаються в limits.inline, їх вміст вшивається прямо в
# повідомлення (read_combined) — без раундів читання тулом: один виклик моделі з усім вмістом одразу.
def read_combined(session_id: str, names: list[str]) -> str:
    """Прочитати кілька вкладень одразу й зклеїти в один блок (без тула
    read_attachment) — для inline-випадку, коли сумарний розмір малий."""
    d = session_dir(session_id)
    parts = []
    for n in names:
        p = d / _safe_name(n)
        if p.is_file() and not images.is_image_name(n):          # картинки — не текст
            parts.append(f"=== {n} ===\n{p.read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(parts)


def attachments_note(session_id: str, meta_list: list[dict]) -> str:
    """Контекстний блок для моделі: імена прикріплених файлів (БЕЗ вмісту). Модель
    читає потрібні через read_file(ім'я) — він знаходить вкладення за іменем у
    теці сесії (нечіткий пошук), тож копіювати довгі шляхи не треба; шукати в них — search(pattern, path=<тека>)."""
    files = [a for a in meta_list if not a.get("error") and not a.get("image")]
    if not files:
        return ""
    lines = [f"  - {a['name']}" for a in files]
    return ("Користувач прикріпив до цієї розмови такі файли (доступні через тули). Щоб "
            "побачити вміст — виклич read_file(path) з іменем файлу (можна кілька викликів одним ходом); "
            f"щоб знайти місце у великому файлі — search(pattern, path='{session_dir(session_id)}'). "
            "НЕ проси користувача надсилати їх ще раз — вони вже є на диску:\n" + "\n".join(lines))
