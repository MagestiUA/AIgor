"""Збережена геометрія вікна не має виводити його за межі екрана (мінімізований стан Windows: x/y ≈ -25600)."""
import main

SCR = (-1958, -1800, 6400, 3080)      # віртуальний екран з кількох моніторів


def main_() -> None:
    s = main.sane_geometry
    g = s({"width": 128, "height": 22, "x": -25600, "y": -25600}, SCR)
    assert g["x"] is None and g["y"] is None and (g["width"], g["height"]) == (1280, 860)
    ok = s({"width": 1280, "height": 860, "x": 100, "y": 50}, SCR)
    assert (ok["x"], ok["y"], ok["width"]) == (100, 50, 1280)
    assert s({"width": 1280, "height": 860, "x": -1900, "y": -1700}, SCR)["x"] == -1900      # другий монітор ліворуч-вгорі
    assert s({"width": 1280, "height": 860, "x": 9000, "y": 10}, SCR)["x"] is None            # монітор відключили
    assert s({"width": 1280, "height": 860, "x": 5000, "y": 5000}, None)["x"] == 5000         # екран невідомий — довіряємо
    assert s({"width": 1280, "height": 860, "x": -25600, "y": 0}, None)["x"] is None
    assert s({"width": 1280, "height": 860, "x": None, "y": 5}, SCR)["y"] is None
    # монітори різного розміру (реальний випадок b_0.0.44): великий зверху, основний 1536×976 знизу ліворуч.
    # Кут вікна (2366, 0) — у спільному прямокутнику, але під верхнім монітором і праворуч від основного: «мертва зона».
    MONS = [(-1595, -1440, 5120, 1440, False), (0, 0, 1536, 976, True)]
    bad = {"width": 2572, "height": 1398, "x": 2366, "y": 0}
    assert s(dict(bad), (-1595, -1440, 5120, 2464))["x"] == 2366                     # стара перевірка цього не бачила
    assert not main.title_on_screen(bad, MONS)
    fixed = s(dict(bad), None, MONS)
    assert fixed == {"width": 1496, "height": 936, "x": 20, "y": 20}, fixed          # по центру основного, влазить у нього
    assert main.title_on_screen(fixed, MONS)
    top = {"width": 2572, "height": 1398, "x": 968, "y": -1440}                       # на верхньому моніторі — лишаємо
    assert s(dict(top), None, MONS) == top and main.title_on_screen(top, MONS)
    assert s({"width": 1280, "height": 860, "x": 100, "y": 50}, None, MONS)["x"] == 100   # на основному — лишаємо
    edge = {"width": 1280, "height": 860, "x": 1500, "y": 100}                        # заголовок лише 36 px на екрані — мало
    assert not main.title_on_screen(edge, MONS)
    part = {"width": 1280, "height": 860, "x": 1300, "y": 100}                        # 236 px заголовка видно — можна схопити
    assert main.title_on_screen(part, MONS) and s(dict(part), None, MONS)["x"] == 1300
    assert main.title_on_screen({"width": 1280, "height": 860, "x": 100, "y": -5}, [(0, 0, 1536, 976, True)])   # на 5 px вище краю — ще схопиш
    # без позиції, але більше за основний монітор — зменшуємо й ставимо по центру; менше — лишаємо системі
    assert s({"width": 2572, "height": 1398, "x": None, "y": None}, None, MONS)["width"] == 1496
    assert s({"width": 1280, "height": 860, "x": None, "y": None}, None, MONS)["x"] is None
    print("OK: геометрія вікна — мінімізовані/позаекранні координати відкидаються, «мертві зони» між моніторами теж")


if __name__ == "__main__":
    main_()
