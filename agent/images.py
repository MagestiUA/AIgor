"""Зображення в повідомленнях (вставка Ctrl+V, перетягування, скріпка) для моделей із зором.

Потік: файл → prepare() (перевірка за сигнатурою, зменшення довгої сторони до MAX_SIDE, мініатюра) → зберігається
в теці вкладень сесії (agent/attachments.py) → при запиті до моделі data_urls() віддає картинки, а клієнти
(llm_openai / llm) кладуть їх у повідомлення користувача: OpenAI — `content` зі списком частин
`{"type": "image_url"}`, Ollama — поле `images` (base64). У внутрішніх повідомленнях картинки лежать у ключі
`images` (список data-URL), а `content` лишається рядком — решті коду (підсумки, токени, історія) нічого не ламається.

Pillow потрібен для зменшення й мініатюр; без нього працюємо спрощено (оригінал як є, ліміт розміру жорсткіший).
"""
from __future__ import annotations

import base64
import io
import re
from datetime import datetime
from pathlib import Path

try:                                   # Pillow не обов'язковий: без нього — спрощений режим
    from PIL import Image, ImageOps
    PIL_OK = True
except Exception:                      # noqa: BLE001
    PIL_OK = False

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_RAW_BYTES = 20 * 1024 * 1024       # більше від користувача не приймаємо
MAX_PLAIN_BYTES = 6 * 1024 * 1024      # без Pillow зменшити не можемо — ліміт жорсткіший
MAX_SIDE = 1568                        # довга сторона для моделі (більше — зайві токени без користі)
KEEP_BYTES = 4 * 1024 * 1024           # не перекодовуємо, якщо вже в межах розміру й сторони
THUMB_SIDE = 160
THUMB_FALLBACK_BYTES = 60 * 1024       # без Pillow оригінал править за мініатюру, лише якщо малий
MAX_IMAGES_PER_REQUEST = 4             # скільки картинок віддаємо моделі за один запит (контекст/вартість)
GENERIC_NAMES = {"image", "blob", "unnamed", "untitled", ""}   # імена, які дає браузер вставленим зображенням


def is_image_name(name: str) -> bool:
    return Path(name or "").suffix.lower() in IMAGE_EXTS


def sniff(data: bytes) -> str | None:
    """MIME за сигнатурою (розширенню не довіряємо — файл може бути чимось іншим)."""
    h = data[:16]
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "image/webp"
    if h[:2] == b"BM":
        return "image/bmp"
    return None


_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp"}


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def friendly_name(name: str, mime: str, existing: set[str] | list[str] = (), now: datetime | None = None) -> str:
    """Ім'я для збереження: стандартні імена браузера («image.png» у вставлених скриншотів) замінюємо на
    «screenshot-ГГММСС.png», розширення — за реальним форматом, дублікати — «(2)»."""
    p = Path(name or "")
    stem = p.stem if p.suffix else (name or "")
    ext = _EXT.get(mime) or (p.suffix.lower() if p.suffix.lower() in IMAGE_EXTS else ".png")
    if stem.strip().lower() in GENERIC_NAMES:
        stem = "screenshot-" + (now or datetime.now()).strftime("%H%M%S")
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", stem).strip() or "image"
    taken = {str(x).lower() for x in existing}
    cand, n = stem + ext, 2
    while cand.lower() in taken:
        cand, n = f"{stem} ({n}){ext}", n + 1
    return cand


def _thumb(img) -> str:
    t = img.copy()
    t.thumbnail((THUMB_SIDE, THUMB_SIDE))
    if t.mode in ("RGBA", "LA", "P"):                       # прозорість → на темний фон, як у чаті
        t = t.convert("RGBA")
        bg = Image.new("RGB", t.size, (38, 38, 36))
        bg.paste(t, mask=t.split()[-1])
        t = bg
    else:
        t = t.convert("RGB")
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=72)
    return data_url(buf.getvalue(), "image/jpeg")


def prepare(filename: str, data: bytes, existing: set[str] | list[str] = ()) -> dict:
    """Перевірити й підготувати зображення. -> {name, size, mime, width, height, thumb, error, _data}.
    `_data` — байти для запису на диск (можуть бути зменшені/перекодовані); при error поля `_data` нема."""
    size = len(data)
    base = {"name": filename, "size": size, "mime": "", "width": 0, "height": 0, "thumb": "", "error": ""}
    if size > MAX_RAW_BYTES:
        return {**base, "error": f"завелике ({size // (1024 * 1024)} МБ > {MAX_RAW_BYTES // (1024 * 1024)} МБ)"}
    mime = sniff(data)
    if not mime:
        return {**base, "error": "це не зображення (підтримуються PNG, JPEG, GIF, WebP, BMP)"}
    if not PIL_OK:
        if size > MAX_PLAIN_BYTES:
            return {**base, "error": f"завелике для вставки без зменшення ({size // (1024 * 1024)} МБ)"}
        return {**base, "name": friendly_name(filename, mime, existing), "mime": mime, "size": size,
                "thumb": data_url(data, mime) if size <= THUMB_FALLBACK_BYTES else "", "_data": data}
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        rotated = img.getexif().get(0x0112, 1) not in (0, 1)     # EXIF-орієнтація (фото з телефона)
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        out_mime = mime
        if mime in ("image/gif", "image/bmp") or (mime == "image/webp" and has_alpha):
            out_mime = "image/png"                          # першим кадром/без втрат
        elif mime == "image/webp":
            out_mime = "image/jpeg"
        scaled = max(w, h) > MAX_SIDE
        if scaled:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        out = data
        if scaled or rotated or out_mime != mime or size > KEEP_BYTES:
            buf = io.BytesIO()
            if out_mime == "image/png":
                img.save(buf, "PNG", optimize=True)
            else:
                img.convert("RGB").save(buf, "JPEG", quality=88)
            out = buf.getvalue()
            if out_mime == "image/png" and len(out) > 8 * 1024 * 1024:       # величезний PNG → JPEG
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=85)
                out, out_mime = buf.getvalue(), "image/jpeg"
        nw, nh = img.size
        return {**base, "name": friendly_name(filename, out_mime, existing), "mime": out_mime, "size": len(out),
                "width": nw, "height": nh, "thumb": _thumb(img), "_data": out}
    except Exception:                                       # noqa: BLE001 — пошкоджений/екзотичний файл
        return {**base, "error": "не вдалося прочитати зображення (файл пошкоджено?)"}


def data_urls(session_dir: Path, names: list[str], limit: int = MAX_IMAGES_PER_REQUEST) -> list[str]:
    """data-URL картинок із теки сесії (для відправки моделі); останні `limit`, відсутні файли пропускаємо."""
    out: list[str] = []
    for n in [x for x in names if is_image_name(x)][-limit:]:
        p = Path(session_dir) / Path(n).name
        try:
            data = p.read_bytes()
        except OSError:
            continue
        mime = sniff(data)
        if mime:
            out.append(data_url(data, mime))
    return out


def split_data_url(url: str) -> tuple[str, str]:
    """'data:image/png;base64,XXXX' → ('image/png', 'XXXX'); для Ollama потрібен чистий base64."""
    m = re.match(r"data:([^;,]+);base64,(.*)$", url or "", re.DOTALL)
    return (m.group(1), m.group(2)) if m else ("", "")
