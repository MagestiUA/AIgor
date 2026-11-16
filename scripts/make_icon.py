"""Іконка Ігоря («Ігор, асистент божевільного вченого»): генерує assets/igor.ico та PNG.

Запуск (потрібен Pillow, лише для збірки — у requirements.txt не входить):
    python scripts/make_icon.py
Створює: assets/igor.ico (16–256 px, для .exe і вікна), assets/favicon.ico (вкладка
браузера/Reflex), assets/igor.png (512 px). Усе малюється кодом — без зовнішніх картинок.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 1024                      # робочий розмір; потім зменшуємо (LANCZOS) — гладкі краї
BG_TOP, BG_BOT = (52, 50, 47), (29, 28, 27)         # графіт, як тло інтерфейсу
ORANGE_A, ORANGE_B = (245, 158, 91), (226, 112, 52)  # теплий помаранчевий (акцент у панелі)
MUTED = (143, 139, 132)


def vgrad(size, top, bot):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = c
    return img


def rounded_mask(size, radius, inset=0):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([inset, inset, size - 1 - inset, size - 1 - inset], radius=radius, fill=255)
    return m


def bang(canvas, cx, top, h, w, color_a, color_b, dot_gap=0.075):
    """Знак оклику: заокруглена «паличка» + крапка, з вертикальним градієнтом."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = vgrad(S, color_a, color_b).convert("RGBA")
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    bar_h = int(h * 0.68)
    d.rounded_rectangle([cx - w // 2, top, cx + w // 2, top + bar_h], radius=w // 2, fill=255)
    dot_d = int(w * 1.04)
    dot_y = top + bar_h + int(S * dot_gap)
    d.ellipse([cx - dot_d // 2, dot_y, cx + dot_d // 2, dot_y + dot_d], fill=255)
    layer.paste(grad, (0, 0), mask)
    canvas.alpha_composite(layer)


def base(bg_top=BG_TOP, bg_bot=BG_BOT, radius=int(S * 0.22)):
    img = vgrad(S, bg_top, bg_bot).convert("RGBA")
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), rounded_mask(S, radius))
    # тонка світла кромка зверху — «обʼєм»
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle([3, 3, S - 4, S - 4], radius=radius - 3, outline=(255, 255, 255, 28), width=5)
    out.alpha_composite(rim)
    return out


def igor_icon():          # МЕМ: Ігор — асистент божевільного вченого («Так, хазяїне?»)
    c = base((62, 38, 96), (24, 16, 40))
    L = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(L)
    DARK, SKIN, SKIN2 = (38, 52, 34), (156, 204, 118), (128, 176, 96)
    # блискавка за головою
    d.polygon([(120, 80), (270, 80), (205, 250), (310, 250), (95, 560), (150, 330), (60, 330)], fill=(255, 214, 64, 255))
    d.polygon([(120, 80), (270, 80), (205, 250), (310, 250), (95, 560), (150, 330), (60, 330)], outline=(255, 244, 170, 255), width=8)
    # болти по боках голови
    for sx in (1, -1):
        cx = 512 + sx * 335
        d.rounded_rectangle([cx - 40, 520, cx + 40, 575], radius=14, fill=(150, 156, 166, 255), outline=DARK + (255,), width=8)
        d.rounded_rectangle([cx + sx * 36 - 14, 505, cx + sx * 36 + 14, 590], radius=8, fill=(180, 186, 196, 255), outline=DARK + (255,), width=8)
    # голова
    d.ellipse([190, 205, 834, 885], fill=SKIN + (255,), outline=DARK + (255,), width=16)
    d.ellipse([230, 640, 794, 860], fill=SKIN2 + (255,))      # тінь підборіддя
    d.ellipse([190, 205, 834, 885], outline=DARK + (255,), width=16)
    # чуб
    for (x0, y0, x1, y1) in ((470, 222, 430, 120), (515, 212, 535, 100), (560, 222, 630, 130)):
        d.line([(x0, y0), (x1, y1)], fill=(70, 46, 36, 255), width=26)
        d.ellipse([x1 - 13, y1 - 13, x1 + 13, y1 + 13], fill=(70, 46, 36, 255))
    # шов на лобі
    zz = [(340, 262), (380, 292), (420, 258), (460, 292), (500, 258), (540, 292), (580, 258), (620, 292), (660, 262)]
    d.line(zz, fill=DARK + (255,), width=10, joint="curve")
    for (x, y) in zz[1:-1:2]:
        d.line([(x, y - 18), (x, y + 18)], fill=DARK + (255,), width=8)
    # брови (одна піднята)
    d.line([(262, 372), (470, 318)], fill=DARK + (255,), width=30)
    d.line([(572, 404), (738, 378)], fill=DARK + (255,), width=30)
    # очі: різного розміру
    d.ellipse([262, 392, 492, 622], fill=(255, 255, 255, 255), outline=DARK + (255,), width=14)
    d.ellipse([400, 480, 470, 550], fill=(20, 20, 20, 255))
    d.ellipse([418, 494, 438, 514], fill=(255, 255, 255, 255))
    d.ellipse([582, 440, 722, 580], fill=(255, 255, 255, 255), outline=DARK + (255,), width=14)
    d.ellipse([640, 492, 690, 542], fill=(20, 20, 20, 255))
    d.pieslice([575, 434, 728, 587], 180, 360, fill=SKIN + (255,))      # зведене повіко
    d.line([(578, 510), (726, 500)], fill=DARK + (255,), width=12)
    # ніс-картоплина
    d.ellipse([478, 590, 578, 700], fill=SKIN2 + (255,), outline=DARK + (255,), width=10)
    # крива посмішка з одним зубом
    d.line([(330, 760), (420, 806), (520, 796), (620, 756), (700, 716)], fill=DARK + (255,), width=20, joint="curve")
    d.rounded_rectangle([470, 794, 524, 856], radius=10, fill=(255, 255, 255, 255), outline=DARK + (255,), width=8)
    c.alpha_composite(L)
    # фірмовий оклик
    bang(c, 880, 150, 260, 62, ORANGE_A, ORANGE_B, dot_gap=0.03)
    return c


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "assets"
    out.mkdir(exist_ok=True)
    big = igor_icon()
    big.resize((512, 512), Image.LANCZOS).save(out / "igor.png")
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    ico = big.resize((256, 256), Image.LANCZOS)
    ico.save(out / "igor.ico", sizes=sizes)
    ico.save(out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("OK:", ", ".join(p.name for p in (out / "igor.ico", out / "favicon.ico", out / "igor.png")))


if __name__ == "__main__":
    main()
