"""Діагностика розкладки вікна: розпізнавання зсуву/зайвого скролу за знімком сторінки."""
from agent import winmetrics as w


def main() -> None:
    ok = {"inner": [1800, 700], "docScroll": [1800, 700], "root": [0, 0, 1800, 700], "scrollXY": [0, 0], "ready": "complete"}
    assert w.layout_problem(ok) == ""
    assert "документ" in w.layout_problem({**ok, "docScroll": [1815, 700]})
    assert "корінь" in w.layout_problem({**ok, "root": [527, 125, 748, 375]})
    assert "прокручена" in w.layout_problem({**ok, "scrollXY": [0, 40]})
    assert w.layout_problem({**ok, "root": [0, 0, 575, 0], "ready": "loading"}) == ""     # ще не змонтовано
    assert w.layout_problem({}) == ""                                                       # битий знімок не падає
    print("OK: winmetrics — виявлення зсуву, зайвого скролу, прокрутки; не дає хибних тривог до монтування")


if __name__ == "__main__":
    main()
