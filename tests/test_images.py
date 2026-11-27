"""Зображення в повідомленнях: перевірка за сигнатурою, зменшення/мініатюра, сховище, клієнти моделей, обробка завантаження."""
import asyncio
import io
import tempfile
import types
from datetime import datetime
from pathlib import Path

from PIL import Image

import lca_web.state as W
from agent import attachments as A
from agent import images as I
from agent import llm, llm_openai


def png(w=64, h=48, color=(200, 30, 30), alpha=False) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", (w, h), color + ((128,) if alpha else ())).save(buf, "PNG")
    return buf.getvalue()


def jpeg(w=64, h=48, orientation=None) -> bytes:
    im = Image.new("RGB", (w, h), (10, 120, 200))
    buf = io.BytesIO()
    if orientation:
        ex = im.getexif()
        ex[0x0112] = orientation
        im.save(buf, "JPEG", exif=ex)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


def open_img(b: bytes):
    return Image.open(io.BytesIO(b))


class UF:
    """Мінімальний rx.UploadFile."""
    def __init__(self, filename, data):
        self.filename, self._d = filename, data

    async def read(self):
        return self._d


def main() -> None:
    # 1) сигнатури; розширенню не віримо
    assert I.sniff(png()) == "image/png" and I.sniff(jpeg()) == "image/jpeg" and I.sniff(b"GIF89a....") == "image/gif"
    assert I.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp" and I.sniff(b"BM" + b"\x00" * 20) == "image/bmp"
    assert I.sniff(b"hello world") is None and I.sniff(b"") is None
    assert I.is_image_name("a.PNG") and I.is_image_name("x.jpeg") and not I.is_image_name("a.py") and not I.is_image_name("")

    # 2) імена: стандартне «image.png» з буфера → screenshot-ЧЧММСС, дублікати → (2), розширення за реальним форматом
    now = datetime(2026, 11, 18, 14, 5, 9)
    assert I.friendly_name("image.png", "image/png", [], now) == "screenshot-140509.png"
    assert I.friendly_name("image.png", "image/png", ["screenshot-140509.png"], now) == "screenshot-140509 (2).png"
    assert I.friendly_name("фото.jpg", "image/png") == "фото.png"                        # формат переважає розширення
    assert I.friendly_name("a/b:c.png", "image/png") == "b_c.png" and I.friendly_name("", "image/jpeg", [], now) == "screenshot-140509.jpg"

    # 3) prepare: звичайна → без змін байтів; мініатюра є; великі → зменшені до MAX_SIDE; GIF/BMP → PNG; EXIF-орієнтація
    r = I.prepare("a.png", png(64, 48))
    assert not r["error"] and r["_data"] == png(64, 48) and (r["width"], r["height"]) == (64, 48) and r["thumb"].startswith("data:image/jpeg;base64,")
    big = I.prepare("big.png", png(4000, 2000))
    assert not big["error"] and max(open_img(big["_data"]).size) == I.MAX_SIDE and big["mime"] == "image/png" and big["size"] < 4000 * 2000
    buf = io.BytesIO()
    Image.new("RGB", (30, 20), (1, 2, 3)).save(buf, "GIF")
    g = I.prepare("anim.gif", buf.getvalue())
    assert g["mime"] == "image/png" and g["name"].endswith(".png") and I.sniff(g["_data"]) == "image/png"
    rot = I.prepare("phone.jpg", jpeg(60, 20, orientation=6))                              # 6 = повернути на 90°
    assert (rot["width"], rot["height"]) == (20, 60) and open_img(rot["_data"]).size == (20, 60), rot["width"]
    assert I.prepare("t.png", png(alpha=True))["thumb"].startswith("data:image/jpeg")        # прозорість → мініатюра без помилок

    # 4) відмови: не зображення, пошкоджене, завелике — зрозумілі помилки, `_data` нема
    for name, data, frag in (("x.png", b"not an image at all", "не зображення"),
                             ("x.png", png()[:40], "не вдалося прочитати"),
                             ("x.png", b"\x89PNG\r\n\x1a\n" + b"0" * (I.MAX_RAW_BYTES + 1), "завелике")):
        e = I.prepare(name, data)
        assert frag in e["error"] and "_data" not in e, (name, e["error"])

    # 5) без Pillow: оригінал як є, ліміт жорсткіший, мініатюра лише для малих
    I.PIL_OK = False
    try:
        r = I.prepare("a.png", png())
        assert not r["error"] and r["_data"] == png() and r["thumb"].startswith("data:image/png")
        assert "завелике" in I.prepare("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * (I.MAX_PLAIN_BYTES + 1))["error"]
    finally:
        I.PIL_OK = True

    # 6) сховище: бінарне збереження, мініатюра, прапорці, текстові потоки не бачать картинок
    tmp = Path(tempfile.mkdtemp())
    real_root = A.ATTACHMENTS_ROOT
    A.ATTACHMENTS_ROOT = tmp / "att"
    try:
        res = I.prepare("image.png", png(), set())
        A.save_bytes("S1", res["name"], res.pop("_data"), res["thumb"])
        A.save("S1", "notes.txt", "текст нотаток")
        saved = {a["name"]: a for a in A.list_saved("S1")}
        assert saved[res["name"]]["image"] and saved[res["name"]]["thumb"] == res["thumb"] and not saved["notes.txt"]["image"]
        assert [a["name"] for a in A.list_text("S1")] == ["notes.txt"]
        note = A.attachments_note("S1", A.list_saved("S1"))
        assert "notes.txt" in note and res["name"] not in note                              # картинку не пропонуємо читати тулом
        assert "текст нотаток" in A.read_combined("S1", ["notes.txt", res["name"]]) and "PNG" not in A.read_combined("S1", [res["name"]])
        urls = I.data_urls(A.session_dir("S1"), [res["name"], "notes.txt", "нема.png"])
        assert len(urls) == 1 and urls[0].startswith("data:image/png;base64,") and I.split_data_url(urls[0])[0] == "image/png"
        A.rename_session("S1", "S2")                                                        # міграція _tmp → сесія: і файли, і .thumbs
        assert A.read_thumb("S2", res["name"]) == res["thumb"] and (A.session_dir("S2") / res["name"]).is_file()
        A.remove("S2", res["name"])
        assert not (A.session_dir("S2") / res["name"]).exists() and A.read_thumb("S2", res["name"]) == ""

        # 6б) міграція зі старого місця КОПІЮЄ (оригінали лишаються), не чіпає вже наявне й повторно нічого не робить
        legacy = tmp / "legacy"
        (legacy / "OLD1").mkdir(parents=True)
        (legacy / "OLD1" / "a.txt").write_text("старе", encoding="utf-8")
        (legacy / "OLD2").mkdir()
        (legacy / "OLD2" / "b.txt").write_text("друге", encoding="utf-8")
        A.save("OLD2", "b.txt", "вже є у профілі")                                      # наявна сесія не перезаписується
        real_legacy, A.LEGACY_ROOT = A.LEGACY_ROOT, legacy
        try:
            assert A.migrate_legacy() == 1 and A.migrate_legacy() == 0
        finally:
            A.LEGACY_ROOT = real_legacy
        assert (A.session_dir("OLD1") / "a.txt").read_text(encoding="utf-8") == "старе"
        assert (legacy / "OLD1" / "a.txt").is_file() and (legacy / "OLD2" / "b.txt").is_file()      # оригінали на місці
        assert (A.session_dir("OLD2") / "b.txt").read_text(encoding="utf-8") == "вже є у профілі"

        # 7) обмеження кількості картинок на запит: останні MAX_IMAGES_PER_REQUEST
        d = A.session_dir("S3")
        for i in range(7):
            A.save_bytes("S3", f"p{i}.png", png(color=(i * 30, 0, 0)))
        assert len(I.data_urls(d, [f"p{i}.png" for i in range(7)])) == I.MAX_IMAGES_PER_REQUEST

        # 8) клієнти моделей
        u = I.data_url(png(), "image/png")
        oa = llm_openai._to_openai_messages([{"role": "system", "content": "s"},
                                             {"role": "user", "content": "що це?", "images": [u, u]},
                                             {"role": "assistant", "content": "a", "images": [u]},      # лише user
                                             {"role": "user", "content": "без картинок"}])
        assert oa[1]["content"][0] == {"type": "text", "text": "що це?"} and oa[1]["content"][1]["image_url"]["url"] == u and len(oa[1]["content"]) == 3
        assert oa[2]["content"] == "a" and oa[3]["content"] == "без картинок" and oa[0]["content"] == "s"
        ol = llm._ollama_messages([{"role": "user", "content": "x", "images": [u]}, {"role": "user", "content": "y"}])
        assert ol[0]["images"] == [I.split_data_url(u)[1]] and "data:" not in ol[0]["images"][0] and "images" not in ol[1]

        # 9) обробка завантаження в State: картинка → диск + чіп із мініатюрою; два однакових «image.png» не затирають одне одного
        class St:
            current_id = "S9"
            attachments: list = []
        st = St()
        st.attachments = []
        up = types.MethodType(W.State.handle_upload.fn, st)
        asyncio.run(up([UF("image.png", png(40, 30)), UF("image.png", png(50, 30, (0, 200, 0))), UF("code.py", b"print(1)\n"),
                        UF("fake.png", "це не картинка".encode("utf-8"))]))
        names = [a["name"] for a in st.attachments]
        assert len(names) == 4 and len(set(names)) == 4, names
        img_chips = [a for a in st.attachments if a["image"] and not a["error"]]
        assert len(img_chips) == 2 and all(a["thumb"].startswith("data:image/jpeg") for a in img_chips)
        assert all((A.session_dir("S9") / a["name"]).is_file() for a in img_chips)
        txt = next(a for a in st.attachments if a["name"] == "code.py")
        assert not txt["image"] and txt["thumb"] == "" and (A.session_dir("S9") / "code.py").is_file()
        bad = next(a for a in st.attachments if a["name"] == "fake.png")
        assert "не зображення" in bad["error"] and bad["image"]

        # 10) які картинки йдуть моделі: лише з ДВОХ ОСТАННІХ повідомлень користувача (поточне + попереднє)
        class Msg:
            current_id = "S3"
            messages = [{"role": "user", "attachments": ["p0.png"]}, {"role": "assistant", "attachments": []},
                        {"role": "user", "attachments": ["p1.png", "note.txt"]}, {"role": "user", "attachments": ["p2.png"]},
                        {"role": "user", "attachments": []}]
        got = types.MethodType(W.State._recent_image_urls, Msg())()
        want = I.data_urls(A.session_dir("S3"), ["p2.png"])                 # попереднє повідомлення; давніші — ні
        assert got == want and len(got) == 1, len(got)
        # регресія: скріншоти давно в історії НЕ чіпляються до нового повідомлення без картинок
        Msg.messages = ([{"role": "user", "attachments": ["p0.png", "p1.png"]}]
                        + [{"role": r, "attachments": []} for r in ("assistant", "user", "assistant", "user")])
        assert types.MethodType(W.State._recent_image_urls, Msg())() == []
        # поточне повідомлення з картинкою + попереднє з картинкою — обидві (уточнення до щойно надісланого скріншота)
        Msg.messages = [{"role": "user", "attachments": ["p1.png"]}, {"role": "assistant", "attachments": []},
                        {"role": "user", "attachments": ["p2.png"]}]
        assert types.MethodType(W.State._recent_image_urls, Msg())() == I.data_urls(A.session_dir("S3"), ["p1.png", "p2.png"])
        Msg.current_id = ""
        assert types.MethodType(W.State._recent_image_urls, Msg())() == []
    finally:
        A.ATTACHMENTS_ROOT = real_root
    print("OK: images — сигнатури, зменшення/EXIF/мініатюри, імена, сховище, клієнти OpenAI/Ollama, завантаження, вибір картинок")


if __name__ == "__main__":
    main()
