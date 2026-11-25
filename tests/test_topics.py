"""Офлайн-тест спільних тем chat-режиму (topics) зі стаб-моделлю.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_topics

ІЗОЛЯЦІЯ: підміняємо topics.TOPICS_ROOT на тимчасову теку на час тесту. Раніше
тест видаляв (shutil.rmtree) РЕАЛЬНУ .chat_topics/ у репо — кожен прогін
стирав справжні теми користувача без попередження (живий кейс: тема "Аналіз
дайджесту червня 2026" зникла після кількох прогонів цього тесту в одній
сесії). НІКОЛИ не чіпати продакшен-шлях у тесті."""
import shutil
import tempfile
from pathlib import Path

from agent import topics


class Stub:
    def __init__(self, scripted):
        self.scripted = scripted
        self.i = 0

    def chat(self, messages, tools=None, profile=None, fmt=None):
        c = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return {"content": c}


from agent import limits
limits.FORCE = limits.production()   # не залежати від налаштувань користувача


def main() -> None:
    real_root = topics.TOPICS_ROOT
    topics.TOPICS_ROOT = Path(tempfile.mkdtemp()) / ".chat_topics"
    try:
        assert topics.list_topics() == []

        # нова тема
        t1 = topics.classify_topic(
            "як спланувати похід у Карпати", "обговорили маршрут",
            [], client=Stub(['{"topic": "Карпати", "is_new": true}']),
        )
        assert t1 == "Карпати"
        note1 = topics.update_topic_note("", "який маршрут", "маршрут через Говерлу",
                                         client=Stub(["Маршрут через Говерлу."]))
        topics.save_topic(t1, note1)
        assert topics.list_topics() == ["Карпати"]
        assert topics.load_topic(t1) == "Маршрут через Говерлу."

        # модель ЛУНАЄ заголовок промпту замість чистого тексту (живий кейс) -> обрізаємо
        note_echo = topics.update_topic_note("", "хід", "відповідь",
                                             client=Stub(["Поточна нотатка:\nЧистий текст."]))
        assert note_echo == "Чистий текст.", repr(note_echo)

        # той самий echo, обгорнутий у markdown (живий кейс: основний файл теми
        # реально починався з "**Поточна нотатка:**")
        note_echo_md = topics.update_topic_note("", "хід", "відповідь",
                                                 client=Stub(["**Поточна нотатка:**\n\nЧистий текст."]))
        assert note_echo_md == "Чистий текст.", repr(note_echo_md)

        # куций хід -> модель повертає лише наш плейсхолдер "(порожньо)" -> НЕ
        # перезаписуємо існуючу нотатку сміттям (живий кейс: файл теми з вмістом
        # буквально "(порожньо)")
        note_junk = topics.update_topic_note("стара нотатка", "хід", "відповідь",
                                              client=Stub(["(порожньо)"]))
        assert note_junk == "стара нотатка", repr(note_junk)

        # той самий хід належить до ІСНУЮЧОЇ теми
        t2 = topics.classify_topic(
            "а що зі спорядженням", "трекінгові палиці", ["Карпати"],
            client=Stub(['{"topic": "Карпати", "is_new": false}']),
        )
        assert t2 == "Карпати"
        note2 = topics.update_topic_note(note1, "спорядження", "трекінгові палиці",
                                         client=Stub(["Маршрут через Говерлу; спорядження — трекінгові палиці."]))
        topics.save_topic(t2, note2)
        assert "спорядження" in topics.load_topic(t2)

        # нова, ІНША тема
        t3 = topics.classify_topic(
            "що з цінами на житло", "ціни можуть впасти", ["Карпати"],
            client=Stub(['{"topic": "Ціни на житло", "is_new": true}']),
        )
        assert t3 == "Ціни на житло"
        topics.save_topic(t3, topics.update_topic_note("", "житло", "ціни впадуть",
                                                        client=Stub(["Ціни на житло можуть впасти."])))
        assert set(topics.list_topics()) == {"Карпати", "Ціни на житло"}

        # переповнення бюджету -> другий (стискаючий) прохід
        compressed = topics.update_topic_note("x" * 200, "ще щось", "ще щось",
                                               client=Stub(["y" * 200_000, "СТИСНУТО"]), budget=1000)
        assert compressed == "СТИСНУТО"

        # невалідний JSON від моделі -> fallback "загальне"
        t4 = topics.classify_topic("щось", "щось", [], client=Stub(["не json взагалі"]))
        assert t4 == "загальне"

        # available_topics_note
        assert topics.available_topics_note([]) == ""
        assert "Карпати" in topics.available_topics_note(["Карпати", "Ціни на житло"])

        # traversal-захист у назві теми (слаг)
        topics.save_topic("../../escape", "x")
        assert not (topics.TOPICS_ROOT.parent.parent / "escape.txt").exists()

        # одна «пам'ять»: recall читає і теми (раніше окремий read_topic); тема — звичайний файл, велика — частинами
        from agent.toolkit import ToolContext, default_registry
        reg = default_registry()
        ctx = ToolContext(root=Path(tempfile.mkdtemp()))
        got = reg.dispatch("recall", {"name": "карпати"}, ctx)
        assert got.startswith("=== тема: Карпати ===") and "спорядження" in got, got[:200]
        assert "спорядження" in reg.dispatch("read_topic", {"name": "Карпат"}, ctx)       # стара назва працює
        assert "read_topic" not in reg.names() and "теми:" in reg.dispatch("recall", {"name": "нема-такої"}, ctx)
        topics.save_topic("Велика", "".join(f"рядок {i}\n" for i in range(40_000)))
        big = reg.dispatch("recall", {"name": "Велика"}, ctx)
        assert "рядки 1-" in big and "start_line=" in big and len(big) < 200_000, big[:200]
        # теки пам'яті — лише ЧИТАННЯ: пошук працює, запис туди заборонено
        assert "рядок 39999" in reg.dispatch("search", {"pattern": "рядок 39999", "path": str(topics.TOPICS_ROOT)}, ctx)
        ctx.permissions = {"edits": "auto", "shell": "off"}
        assert "поза межами" in reg.dispatch("write_file", {"path": str(topics.TOPICS_ROOT / "x.txt"), "content": "x"}, ctx)
        assert not (topics.TOPICS_ROOT / "x.txt").exists()

        # сповіщення про стиснення нотатки теми
        ev: list = []
        topics.update_topic_note("x" * 200, "ще", "ще", client=Stub(["y" * 5000, "СТИСНУТО"]), budget=1000,
                                 on_compress=lambda w, b, a: ev.append((w, b, a)))
        assert ev and ev[0][0] == "нотатка теми" and ev[0][2] == len("СТИСНУТО"), ev

        print("OK: topics — класифікація (нова/існуюча), злиття нотатки, бюджет, "
              "fallback на невалідний JSON, topics_note, захист імені файлу")
    finally:
        shutil.rmtree(topics.TOPICS_ROOT, ignore_errors=True)
        topics.TOPICS_ROOT = real_root


if __name__ == "__main__":
    main()
