"""local-code-agent — веб-інтерфейс на Reflex (стиль Claude): сторінка й застосунок.

Стан і логіка — state.py, компоненти — ui.py. Тут лише збірка сторінки й app."""
import reflex as rx
from agent import logs
from .state import (State)
from .ui import (BG, confirm_dialog, context_panel, main_area, move_dialog, project_dialog, rename_dialog, sidebar)


def index() -> rx.Component:
    return rx.hstack(
        sidebar(),
        main_area(),
        context_panel(),
        rename_dialog(),
        project_dialog(),
        move_dialog(),
        confirm_dialog(),
        # fixed inset-0, а не h-screen/w-screen: 100vw включає смугу прокрутки й дає зайвий скрол документа;
        # на широких моніторах після розгортання вікна це давало зсув інтерфейсу й білі поля.
        class_name="fixed inset-0 overflow-hidden",
        style={"backgroundColor": BG},
        spacing="0",
    )


app = rx.App(
    stylesheets=["https://fonts.googleapis.com/css2?family=Newsreader:ital@0;1&display=swap"],
    head_components=[rx.el.style("html,body{margin:0;height:100%;overflow:hidden}")],
)
app.add_page(index, title=f"Ігорь! · {logs.app_version()}",
             on_load=[State.load_sessions, State.load_settings, State.check_updates(True), State.poll_updates])
