"""Швидка маршрутизація запиту БЕЗ моделі.

Раніше тут був окремий класифікатор наміру (виклик моделі edit|plan|answer|shell) перед планувальником. Тепер тип
запиту визначає сам планувальник у тому самому виклику (planner.deliberate(route=True)) — мінус один виклик моделі на
кожне повідомлення. Код лишає лише однозначні випадки, де модель не потрібна:
  * режим «правки: auto» + лог помилки (і без «не змінюй код») → виправляти (план);
  * питання про можливість / «просто питання, без коду» → обговорення (answer).
"""
from __future__ import annotations

from .orchestration import explicit_no_change, looks_like_error_log, wants_discussion


def quick_route(task: str, autonomous: bool = False, has_attachments: bool = False) -> str:
    """'edit' | 'answer' | '' (вирішить планувальник)."""
    if autonomous and looks_like_error_log(task) and not explicit_no_change(task):
        return "edit"                             # лог помилки в режимі auto — виправляємо
    # питання про можливість/«просто питання» — обговорення (в auto з прикріпленим логом — вирішує планувальник)
    if wants_discussion(task) and not (autonomous and has_attachments and not explicit_no_change(task)):
        return "answer"
    return ""
