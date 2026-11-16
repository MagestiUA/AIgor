"""Бюджети контексту/вкладень, що залежать від бекенда.

Проєкт народжувався під слабку локальну модель з малим контекстом (gemma4/Qwen3-Coder
через Ollama): усе тримається в файлах, дайджестах і підсумках розміром ~32k символів.
Сервер на H100 (Qwen3.8-27B, 256k токенів) дозволяє на порядок більше — головне, щоб
сумарно вміщалось у 256k (кеш спільний лише на 1–2 одночасних користувачів, тож
сильно економити не треба).

Символи, не токени: кирилиця+код ≈ 2.5–3.2 симв./токен.
get() читає ефективний бекенд із налаштувань щоразу (його можна змінити в UI без
перезапуску). FORCE — для тестів: зафіксувати набір незалежно від налаштувань.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    chunk: int          # символів за один read_attachment (пагінація)
    inline: int         # сумарний розмір вкладень, що вшиваємо в повідомлення без тулів
    summary: int        # бюджет підсумку розмови (convo.update_summary)
    digest: int         # бюджет дайджесту великого файлу
    topic: int          # бюджет нотатки теми (chat)
    history_msgs: int   # скільки останніх повідомлень чату йде в контекст
    max_file: int       # макс. розмір одного вкладення, байт
    file_chars: int     # скільки символів файлу зберігаємо при завантаженні
    read_chars: int     # скільки символів віддає один read_file (далі — offset)
    context_tokens: int  # розмір вікна моделі (для панелі «Контекст»)
    hard_iters: int      # запобіжник довжини кроку; 0 = оцінка моделлю (_estimate_iters), як раніше
    # Милиці під слабку модель (CRUTCHES.md A1/A2/A4/A5): виконання «просочених» викликів
    # тулів з тексту, nudge за regex-евристиками, самовільне читання вкладень за
    # «оголошеним наміром», повтор при self-talk. На сильній моделі вони шкодять:
    # живий тест T5 — 11/12 звичайних текстових відповідей з прикладом JSON було б
    # ВИКОНАНО як виклик write_file.
    weak_model_fixes: bool


LOCAL = Limits(chunk=32_000, inline=32_000, summary=32_000, digest=96_000, topic=96_000,
               history_msgs=20, max_file=200 * 1024, file_chars=12_000, read_chars=8_000, context_tokens=131_072, hard_iters=0,
               weak_model_fixes=True)

# ~256k токенів: найбільший одночасний внесок (вкладення inline ≈ 80k ток. + підсумок
# ≈ 40k при 2.5 симв./ток.) ≈ 120k, тобто < 50% вікна — решта під історію, системні
# промпти, тули й відповідь.
REMOTE = Limits(chunk=120_000, inline=200_000, summary=100_000, digest=200_000,
                topic=200_000, history_msgs=40, max_file=2 * 1024 * 1024,
                file_chars=2 * 1024 * 1024, read_chars=150_000, context_tokens=262_144, hard_iters=400, weak_model_fixes=False)

FORCE: Limits | None = None


def get(settings: dict | None = None) -> Limits:
    if FORCE is not None:
        return FORCE
    from . import backend
    return REMOTE if backend.resolve(settings)["backend"] == "openai" else LOCAL
