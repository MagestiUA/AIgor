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
    print("OK: геометрія вікна — мінімізовані/позаекранні координати відкидаються")


if __name__ == "__main__":
    main_()
