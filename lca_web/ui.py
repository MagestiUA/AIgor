"""Компоненти інтерфейсу (Reflex): сайдбар, чат, панелі, діалоги. Стан — у state.py."""
import reflex as rx
from reflex_base.style import set_color_mode
from agent import chat_projects as CP
from agent import config
from .state import (INPUT_ID, State)


SCROLL_SETUP_JS = """
(() => {
  // Виміряти ширину скролбара -> CSS-змінна (для вирівнювання поля вводу зі
  // скрольованим чатом: чат втрачає ширину на скролбар, поле вводу — ні).
  const sd = document.createElement('div');
  sd.style.cssText = 'overflow:scroll;width:50px;height:50px;position:absolute;top:-9999px';
  document.body.appendChild(sd);
  document.documentElement.style.setProperty('--sbw', (sd.offsetWidth - sd.clientWidth) + 'px');
  document.body.removeChild(sd);

  const c = document.getElementById('chat-scroll');
  if (!c || c._lcaObs) return;
  let stick = true;
  const nearBottom = () => c.scrollHeight - c.scrollTop - c.clientHeight < 120;
  const btn = document.getElementById('scroll-down-btn');
  const updBtn = () => { if (btn) btn.style.display = nearBottom() ? 'none' : 'inline-flex'; };
  c.addEventListener('scroll', () => { stick = nearBottom(); updBtn(); });
  const obs = new MutationObserver(() => { if (stick) c.scrollTop = c.scrollHeight; updBtn(); });
  obs.observe(c, {childList: true, subtree: true, characterData: true});
  c._lcaObs = obs;
  c.scrollTop = c.scrollHeight;
  updBtn();
})();
"""
SCROLL_BOTTOM_JS = "var c=document.getElementById('chat-scroll'); if(c){c.scrollTop=c.scrollHeight;}"
BG = "#262624"
PANEL = "#171615"
INPUT = "#30302e"
BORDER = "border-white/10"
SERIF = {"fontFamily": "Newsreader, Georgia, serif"}



def nav_item(icon: str, label: str, on_click=None) -> rx.Component:
    return rx.hstack(
        rx.icon(icon, size=16, class_name="text-gray-400"),
        rx.text(label, class_name="text-sm text-gray-200"),
        on_click=on_click,
        class_name="items-center gap-3 px-2 py-1.5 rounded-lg hover:bg-white/5 "
                   "cursor-pointer w-full",
    )


def session_item(s: dict) -> rx.Component:
    active = State.current_id == s["id"]
    return rx.hstack(
        rx.hstack(
            rx.icon("message-square", size=14, class_name="text-gray-500 shrink-0"),
            rx.text(s["title"], class_name="text-sm text-gray-200 truncate"),
            on_click=lambda: State.select_chat(s["id"]),
            class_name="items-center gap-2 grow min-w-0 cursor-pointer",
        ),
        rx.menu.root(
            rx.menu.trigger(
                rx.icon("ellipsis", size=14,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0"),
            ),
            rx.menu.content(
                rx.menu.item("Перейменувати",
                             on_click=lambda: State.open_rename(s["id"], s["title"])),
                rx.cond(
                    s["kind"] == "chat",
                    rx.menu.item("Перенести…", on_click=lambda: State.open_move_dialog(s["id"])),
                    rx.fragment(),
                ),
                rx.menu.item("Видалити", on_click=lambda: State.delete_chat(s["id"]),
                             class_name="text-red-400"),
            ),
        ),
        class_name=rx.cond(active, "bg-white/10", "hover:bg-white/5")
        + " items-center gap-1 px-2 py-1.5 rounded-lg w-full",
    )


def rename_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Перейменувати чат"),
            rx.input(value=State.rename_input, on_change=State.set_rename_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_rename_open(False)),
                rx.button("Зберегти", on_click=State.save_rename),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.rename_open,
        on_open_change=State.set_rename_open,
    )


def project_item(p: dict) -> rx.Component:
    """Рядок проекту в сайдбарі: клік — фільтр чатів цього проекту; 3-крапки —
    редагувати промпт / видалити."""
    active = State.current_project_id == p["id"]
    return rx.hstack(
        rx.hstack(
            rx.icon("folder", size=14, class_name="text-gray-500 shrink-0"),
            rx.text(p["name"], class_name="text-sm text-gray-200 truncate"),
            on_click=lambda: State.select_project(p["id"]),
            class_name="items-center gap-2 grow min-w-0 cursor-pointer",
        ),
        rx.menu.root(
            rx.menu.trigger(
                rx.icon("ellipsis", size=14,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0"),
            ),
            rx.menu.content(
                rx.menu.item("Редагувати", on_click=lambda: State.open_edit_project(p["id"])),
                rx.menu.item("Видалити", on_click=lambda: State.delete_project_event(p["id"]),
                             class_name="text-red-400"),
            ),
        ),
        class_name=rx.cond(active, "bg-white/10", "hover:bg-white/5")
        + " items-center gap-1 px-2 py-1.5 rounded-lg w-full",
    )


def project_dialog() -> rx.Component:
    """Створення/редагування проекту: назва + промпт (до 5000 символів), що
    вшивається в системний промпт кожного повідомлення чатів цього проекту."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title(rx.cond(State.project_edit_id != "", "Редагувати проект", "Новий проект")),
            rx.input(placeholder="Назва проекту", value=State.project_name_input,
                     on_change=State.set_project_name_input, class_name="w-full mt-2"),
            rx.text_area(
                placeholder="Інструкції проекту — додаються до кожного повідомлення в чатах "
                            "цього проекту (до 5000 символів)",
                value=State.project_prompt_input, on_change=State.set_project_prompt_input,
                max_length=CP.PROMPT_MAX_CHARS, rows="8", class_name="w-full mt-2",
            ),
            rx.text("До 5000 символів — зайве буде обрізано",
                    class_name="text-xs text-gray-500 mt-1"),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_project_dialog_open(False)),
                rx.button("Зберегти", on_click=State.save_project_dialog),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.project_dialog_open,
        on_open_change=State.set_project_dialog_open,
    )


def move_dialog() -> rx.Component:
    """Перенести чат у проект (чи "без проекту")."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Перенести чат у проект"),
            rx.vstack(
                rx.box(
                    rx.text("Без проекту", class_name="text-sm text-gray-200"),
                    on_click=lambda: State.move_chat_to_project(""),
                    class_name="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer w-full",
                ),
                rx.foreach(
                    State.projects,
                    lambda p: rx.box(
                        rx.text(p["name"], class_name="text-sm text-gray-200"),
                        on_click=lambda: State.move_chat_to_project(p["id"]),
                        class_name="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer w-full",
                    ),
                ),
                class_name="w-full gap-1 mt-2 max-h-72 overflow-y-auto",
            ),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_move_dialog_open(False)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.move_dialog_open,
        on_open_change=State.set_move_dialog_open,
    )


def confirm_dialog() -> rx.Component:
    """Попап для shell=ask: підтвердити запуск команди. Керований лише кнопками
    (без on_open_change), щоб закриття не лишило воркер у вічному очікуванні."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Виконати команду в консолі?"),
            rx.code_block(State.confirm_text, language="powershell",
                          class_name="w-full mt-2"),
            rx.flex(
                rx.button("Відхилити", variant="soft", color_scheme="red",
                          on_click=State.confirm_no),
                rx.button("Виконати", on_click=State.confirm_yes),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.confirm_open,
    )


def mode_switch() -> rx.Component:
    """Верхній перемикач режимів Чат | Код."""
    def tab(label: str, m: str, icon: str) -> rx.Component:
        active = State.mode == m
        return rx.hstack(
            rx.icon(icon, size=14),
            rx.text(label, class_name="text-sm"),
            on_click=lambda: State.set_mode(m),
            class_name=rx.cond(active, "bg-white/10 text-gray-100", "text-gray-400 hover:bg-white/5")
            + " items-center justify-center gap-1.5 grow rounded-md py-1.5 cursor-pointer",
        )
    return rx.hstack(
        tab("Чат", "chat", "message-circle"),
        tab("Код", "code", "code"),
        class_name="w-full gap-1 p-1 rounded-lg bg-white/[0.03] mb-1",
    )


def llm_connection_section() -> rx.Component:
    """Підключення до LLM: локальна Ollama або OpenAI-сумісний сервер (vLLM)."""
    def field(label, ctrl):
        return rx.hstack(rx.text(label, class_name="text-sm text-gray-300 w-24 shrink-0"),
                         ctrl, class_name="items-center gap-3 w-full")
    remote = rx.vstack(
        field("Адреса", rx.input(value=State.llm_host, on_change=State.set_llm_host,
                                 placeholder="напр. llm.example.com", class_name="flex-1")),
        field("Порт", rx.input(value=State.llm_port, on_change=State.set_llm_port,
                               placeholder="8000", class_name="flex-1")),
        field("API-ключ", rx.input(
            value=State.llm_key_input, on_change=State.set_llm_key_input, type="password",
            placeholder=rx.cond(State.llm_key_saved, "збережено (введіть, щоб замінити)", "ваш ключ"),
            class_name="flex-1")),
        field("Модель", rx.input(value=State.llm_model, on_change=State.set_llm_model,
                                 placeholder="авто (перша на сервері)", class_name="flex-1")),
        gap="2", class_name="w-full",
    )
    ctx_field = field("Вікно (токени)", rx.input(
        value=State.llm_context, on_change=State.set_llm_context, class_name="flex-1",
        placeholder=rx.cond(State.llm_backend == "openai", "пусто = 262144 (вікно сервера)", "пусто = за замовчуванням (65536)")))
    local = rx.vstack(
        field("Модель", rx.input(value=State.llm_model, on_change=State.set_llm_model, class_name="flex-1",
                                 placeholder="пусто = Qwen3.8-27B (UD-Q3_K_XL)")),
        rx.hstack(*[rx.button(name, variant="soft", size="1", on_click=State.pick_local_preset(i))
                    for i, (name, _m, _c) in enumerate(config.LOCAL_PRESETS)],
                  class_name="flex-wrap gap-2 pl-[108px]"),
        gap="2", class_name="w-full",
    )
    return rx.vstack(
        rx.text("Підключення до LLM", class_name="text-xs text-gray-500 uppercase tracking-wide"),
        rx.segmented_control.root(
            rx.segmented_control.item("Локально (Ollama)", value="ollama"),
            rx.segmented_control.item("Сервер (OpenAI API)", value="openai"),
            value=State.llm_backend, on_change=State.set_llm_backend, size="2", class_name="w-full",
        ),
        rx.cond(State.llm_backend == "openai", remote, local),
        ctx_field,
        rx.text("Вікно визначає, коли чат стискається: бюджети підсумків і вкладень масштабуються під нього. Для локальної "
                "моделі не ставте більше, ніж вміщає відеопам'ять (інакше модель вилізе на CPU й сповільниться).",
                class_name="text-xs text-gray-500"),
        rx.hstack(
            rx.button("Перевірити", variant="soft", on_click=State.test_llm_connection,
                      loading=State.llm_testing),
            rx.button("Зберегти", on_click=State.save_llm_settings),
            rx.button("Діагностика", variant="outline", on_click=State.export_diagnostics,
                      title="Зібрати логи в архів, щоб надіслати розробнику"),
            class_name="gap-2",
        ),
        rx.cond(State.llm_test_msg != "",
                rx.text(State.llm_test_msg,
                        class_name=rx.cond(State.llm_test_ok, "text-xs text-green-400",
                                           "text-xs text-red-400"))),
        gap="2", class_name="w-full mb-4",
    )


def gpu_settings_section() -> rx.Component:
    """Налаштування -> Навантаження сервера: вмикач панелі GPU та порт агента моніторингу."""
    return rx.vstack(
        rx.text("Моніторинг сервера", class_name="text-xs text-gray-500 uppercase tracking-wide"),
        rx.hstack(
            rx.switch(checked=State.gpu_enabled, on_change=State.set_gpu_monitor, size="2"),
            rx.text("Відображати навантаження ГПУ", class_name="text-sm text-gray-300"),
            class_name="items-center gap-3"),
        rx.text("Якщо на сервері піднято сумісний агент моніторингу навантаження: раз на 10 с Ігорь читає "
                "GET /metrics.json (той самий ключ, що й для моделі) і показує зверху правої панелі, чи "
                "відповідає сервер та навантаження ГПУ (завантаження, Вт, температура, VRAM). Адреса сервера — "
                "з підключення до LLM; працює лише з бекендом «Сервер (OpenAI API)». Увімкнено за замовчуванням; "
                "якщо агента нема, замість графіка буде напис «Дані навантаження GPU недоступні».",
                class_name="text-xs text-gray-500"),
        rx.cond(State.gpu_enabled,
                rx.hstack(rx.text("Порт агента", class_name="text-sm text-gray-300 w-24 shrink-0"),
                          rx.input(value=State.gpu_port, on_change=State.set_gpu_port, placeholder="8001",
                                   class_name="flex-1"),
                          class_name="items-center gap-3 w-full")),
        gap="2", class_name="w-full mb-4",
    )


def update_section() -> rx.Component:
    """Налаштування -> Оновлення: поточна версія, ручна перевірка, кнопка «Оновити»."""
    return rx.vstack(
        rx.text("Оновлення", class_name="text-xs text-gray-500 uppercase tracking-wide"),
        rx.hstack(
            rx.text("Версія: ", State.app_ver, class_name="text-sm text-gray-300"),
            rx.spacer(),
            rx.button("Перевірити оновлення", variant="soft", on_click=State.check_updates(False),
                      loading=State.update_busy & ~State.update_available),
            rx.cond(State.update_available,
                    rx.button("Оновити до ", State.update_latest, on_click=State.install_update,
                              loading=State.update_busy),
                    rx.fragment()),
            class_name="w-full items-center gap-2 flex-wrap",
        ),
        rx.cond(State.update_msg != "",
                rx.text(State.update_msg,
                        class_name=rx.cond(State.update_ok, "text-xs text-green-400", "text-xs text-red-400"))),
        rx.cond((State.update_notes != "") & State.update_available,
                rx.text(State.update_notes, class_name="text-xs text-gray-400 whitespace-pre-wrap max-h-32 overflow-y-auto")),
        gap="2", class_name="w-full mb-4",
    )


def update_banner() -> rx.Component:
    """Плашка про нову версію над вкладками режимів (на старті з тихої перевірки). Ставить лише за кліком."""
    return rx.cond(
        State.update_available & ~State.update_dismissed,
        rx.vstack(
            rx.hstack(
                rx.icon("download", size=14, class_name="text-sky-400 shrink-0"),
                rx.text("Доступна ", State.update_latest, class_name="text-xs text-gray-200 flex-1"),
                rx.icon("x", size=13, class_name="text-gray-500 cursor-pointer shrink-0",
                        on_click=State.dismiss_update),
                class_name="w-full items-center gap-1.5",
            ),
            rx.button(rx.cond(State.update_busy, "Оновлюю…", "Оновити"), size="1", variant="soft",
                      on_click=State.install_update, loading=State.update_busy, class_name="w-full"),
            rx.cond(State.update_msg != "",
                    rx.text(State.update_msg, class_name=rx.cond(State.update_ok, "text-[11px] text-gray-400",
                                                                  "text-[11px] text-red-400"))),
            class_name="w-full gap-1.5 p-2 mb-1 rounded-lg bg-sky-500/10 border border-sky-500/20",
        ),
        rx.fragment(),
    )


def settings_dialog() -> rx.Component:
    def font_slider(label, value, on_change, min_v, max_v):
        return rx.hstack(
            rx.text(label, class_name="text-sm text-gray-300 w-24 shrink-0"),
            rx.slider(
                value=[value],
                on_change=on_change,
                min=min_v,
                max=max_v,
                step=1,
                class_name="flex-1",
            ),
            rx.text(value, "px", class_name="text-sm text-gray-400 w-10 shrink-0 text-right"),
            class_name="items-center gap-3 w-full",
        )

    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.hstack(
                    rx.heading("Налаштування", size="4"),
                    rx.spacer(),
                    rx.dialog.close(
                        rx.icon_button(rx.icon("x", size=16), variant="ghost", size="1"),
                    ),
                    class_name="w-full items-center mb-4",
                ),
                llm_connection_section(),
                gpu_settings_section(),
                update_section(),
                # Тема
                rx.text("Тема", class_name="text-xs text-gray-500 uppercase tracking-wide mb-1"),
                rx.segmented_control.root(
                    rx.segmented_control.item("Світла", value="light"),
                    rx.segmented_control.item("Темна", value="dark"),
                    rx.segmented_control.item("Авто", value="system"),
                    value=State.theme,
                    # set_color_mode — фронтенд-тригер, що реально перемикає тему (next-themes).
                    # set_theme лише зберігає вибір у State/на диск; без set_color_mode тема
                    # візуально не мінялась (це й був баг «перемикачі нічого не роблять»).
                    on_change=lambda v: [State.set_theme(v), set_color_mode(v)],
                    size="2", class_name="mb-4 w-full",
                ),
                # Шрифти
                rx.text("Шрифти", class_name="text-xs text-gray-500 uppercase tracking-wide mb-2"),
                font_slider("Чат", State.font_chat,
                            lambda v: State.set_font_chat(v), 12, 32),
                font_slider("Інтерфейс", State.font_ui,
                            lambda v: State.set_font_ui(v), 11, 24),
                gap="4", class_name="w-full px-2",
            ),
            style={"backgroundColor": PANEL, "maxWidth": "560px", "width": "92vw"},
        ),
        open=State.settings_open,
        on_open_change=State.toggle_settings,
    )


def project_section() -> rx.Component:
    """Проекти chat-режиму (аналог Claude/ChatGPT Projects): список + "+ Новий
    проект". Лише mode="chat" — код-режим має власну прив'язку до робочої теки."""
    return rx.cond(
        State.is_chat,
        rx.fragment(
            rx.hstack(
                rx.text("Проекти", class_name="text-xs text-gray-500 uppercase tracking-wide"),
                rx.spacer(),
                rx.icon("plus", size=13, on_click=State.open_new_project,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer"),
                class_name="items-center mt-5 mb-1 px-2",
            ),
            rx.cond(
                State.projects,
                rx.vstack(rx.foreach(State.projects, project_item), class_name="w-full gap-0.5"),
                rx.text("Поки немає проектів", class_name="text-sm text-gray-500 px-2"),
            ),
        ),
        rx.fragment(),
    )


def sidebar() -> rx.Component:
    return rx.flex(
        settings_dialog(),
        update_banner(),
        mode_switch(),
        nav_item("plus", "Новий чат", State.new_chat),
        project_section(),
        rx.cond(
            State.is_chat & (State.current_project_id != ""),
            rx.fragment(
                rx.hstack(
                    rx.icon("arrow-left", size=12),
                    rx.text("Усі чати", class_name="text-xs"),
                    on_click=lambda: State.select_project(""),
                    class_name="items-center gap-1 text-gray-500 hover:text-gray-200 cursor-pointer "
                               "px-2 mt-3",
                ),
                # "+ Новий чат" САМЕ в межах поточного проекту — між назвою проекту
                # (вище, в project_section) і списком його чатів (нижче); відокремлено
                # від загального "+ Новий чат" нагорі, який теж створює в поточному
                # проекті (State.new_chat вже враховує current_project_id), але тут це
                # видно в контексті, без потреби гортати сайдбар нагору.
                rx.hstack(
                    rx.icon("plus", size=12),
                    rx.text("Новий чат", class_name="text-xs"),
                    on_click=State.new_chat,
                    class_name="items-center gap-1 text-gray-500 hover:text-gray-200 cursor-pointer "
                               "px-2 mt-1.5",
                ),
            ),
            rx.text("Recents", class_name=rx.cond(State.is_chat, "mt-2", "mt-5")
                    + " text-xs text-gray-500 uppercase tracking-wide mb-1 px-2"),
        ),
        rx.vstack(
            rx.cond(
                State.visible_sessions,
                rx.foreach(State.visible_sessions, session_item),
                rx.text("Поки немає чатів", class_name="text-sm text-gray-400 px-2"),
            ),
            class_name=rx.cond(State.is_chat & (State.current_project_id != ""), "pl-3", "")
            + " flex-1 w-full gap-0.5 overflow-y-auto mt-1",
        ),
        rx.spacer(),
        # self-start: не розтягувати на всю ширину колонки (інакше контур-пігулка йде
        # через увесь сайдбар). icon_button — квадрат за розміром іконки, ліворуч.
        rx.icon_button(
            rx.icon("settings", size=18),
            on_click=State.toggle_settings,
            variant="ghost",
            color_scheme="gray",
            size="2",
            class_name="self-start mb-2 ml-1 rounded-full hover:bg-white/10 text-gray-400",
        ),
        direction="column",
        class_name="w-64 max-[900px]:w-48 shrink-0 h-full p-2 gap-0.5",
        style={"backgroundColor": PANEL, "borderRight": "1px solid rgba(255,255,255,0.08)"},
    )


def folder_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("folder", size=14), State.folder_name, type="button",
                      variant="ghost", size="1", class_name="text-gray-300 gap-1"),
        ),
        rx.dialog.content(
            rx.dialog.title("Робоча тека проєкту"),
            rx.input(placeholder="Абсолютний шлях до папки",
                     value=State.folder_input, on_change=State.set_folder_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.dialog.close(rx.button("Зберегти", on_click=State.save_folder)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
    )


def ref_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("plus", size=12), "файл", type="button", variant="ghost",
                      size="1", class_name="text-gray-400"),
        ),
        rx.dialog.content(
            rx.dialog.title("Файл-джерело (read-only)"),
            rx.input(placeholder="Абсолютний шлях до файлу",
                     value=State.ref_input, on_change=State.set_ref_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.dialog.close(rx.button("Додати", on_click=State.add_ref)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
    )


def _code_controls() -> rx.Component:
    """Контроли лише для Код-режиму: тека, дозволи, план наперед."""
    return rx.hstack(
        folder_dialog(),
        rx.hstack(
            rx.text("правки", class_name="text-xs text-gray-500"),
            rx.select(["ask", "auto"], value=State.edits, on_change=State.set_edits,
                      size="1", variant="soft", width="5rem"),
            class_name="items-center gap-1",
        ),
        rx.hstack(
            rx.text("консоль", class_name="text-xs text-gray-500"),
            rx.select.root(
                rx.select.trigger(variant="soft", width="6.2rem"),
                rx.select.content(
                    rx.select.item("smart", value="smart"),
                    rx.select.item("ask", value="ask"),
                    rx.select.item("auto", value="auto"),
                    rx.select.item("allowlist", value="allowlist"),
                    rx.select.item("off", value="off"),
                ),
                value=State.shell, on_change=State.set_shell,
                size="1",
            ),
            class_name="items-center gap-1",
        ),
        class_name="items-center gap-2 flex-wrap",
    )


def attachment_chips() -> rx.Component:
    """Рядок чіпів прикріплених файлів під textarea."""
    def chip(a: dict) -> rx.Component:
        has_error = a["error"] != ""
        return rx.hstack(
            rx.cond(a["thumb"] != "",
                    rx.image(src=a["thumb"], class_name="w-7 h-7 rounded object-cover shrink-0"),
                    rx.icon("file", size=12, class_name=rx.cond(has_error, "text-red-400", "text-gray-400"))),
            rx.text(a["name"], class_name=rx.cond(has_error, "text-red-300", "text-gray-300")
                    + " text-xs truncate max-w-28"),
            rx.cond(has_error,
                    rx.text(a["error"], class_name="text-xs text-red-400 truncate max-w-24"),
                    rx.fragment()),
            rx.icon("x", size=11,
                    class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0",
                    on_click=lambda: State.remove_attachment(a["name"])),
            class_name="items-center gap-1 px-2 py-0.5 rounded-full border "
                       + rx.cond(has_error, "border-red-500/30 bg-red-900/20",
                                 "border-white/10 bg-white/5"),
        )
    return rx.cond(
        State.attachments,
        rx.hstack(
            rx.foreach(State.attachments, chip),
            class_name="flex-wrap gap-1.5 mt-2",
        ),
        rx.fragment(),
    )


def controls_bar() -> rx.Component:
    return rx.hstack(
        rx.upload(
            rx.icon_button(rx.icon("paperclip", size=15), type="button", variant="ghost",
                           size="1", class_name="text-gray-400"),
            id="file_upload",
            multiple=True,
            on_drop=State.handle_upload(rx.upload_files(upload_id="file_upload")),
            accept={
                "text/plain": [".txt", ".md", ".csv", ".log", ".rst"],
                "text/x-python": [".py", ".pyw", ".pyi"],
                "application/json": [".json", ".jsonc"],
                "text/javascript": [".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"],
                "text/html": [".html", ".htm"],
                "text/css": [".css", ".scss", ".sass"],
                "text/yaml": [".yaml", ".yml"],
                "application/toml": [".toml"],
                "text/x-sh": [".sh", ".bash", ".zsh", ".bat", ".ps1"],
                "application/xml": [".xml", ".svg"],
                "application/sql": [".sql"],
                # зображення для моделей із зором (скріпка, Ctrl+V і перетягування йдуть тим самим шляхом)
                "image/png": [".png"],
                "image/jpeg": [".jpg", ".jpeg"],
                "image/gif": [".gif"],
                "image/webp": [".webp"],
                "image/bmp": [".bmp"],
            },
            no_drag=True,
            # Прибрати дефолтну рамку/padding rx.upload (вони задаються як ПРЯМІ
            # props через setdefault, не через style) — лишити тільки іконку-кнопку.
            border="none",
            padding="0",
            width="fit-content",
        ),
        rx.cond(State.is_chat, rx.fragment(), _code_controls()),
        rx.spacer(),
        rx.cond(
            State.is_chat,
            rx.hstack(rx.icon("globe", size=14, class_name="text-gray-500"),
                      rx.text("веб-пошук", class_name="text-xs text-gray-500"),
                      class_name="items-center gap-1"),
            rx.hstack(
                rx.text("план наперед", class_name="text-xs text-gray-500"),
                rx.switch(checked=State.plan_first, on_change=State.set_plan_first, size="1"),
                class_name="items-center gap-2",
            ),
        ),
        rx.cond(
            State.busy & (State.queued_text == "") & ~State.stopping,
            # Зайнята + черга порожня + ще не зупиняємось → кнопка Стоп
            rx.button(
                rx.icon("square", size=16),
                on_click=State.stop_generation,
                size="1", radius="full",
                class_name="bg-red-500 text-white ml-1 hover:bg-red-600",
            ),
            # Інакше → submit (send або + в чергу)
            rx.button(
                rx.cond(State.busy, rx.icon("plus", size=16), rx.icon("arrow-up", size=16)),
                type="submit",
                size="1", radius="full", class_name="bg-white text-black ml-1",
            ),
        ),
        class_name="w-full items-center mt-2 gap-2 flex-wrap",
    )


def extra_dir_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("plus", size=12), "тека", type="button", variant="ghost",
                      size="1", class_name="text-gray-400"),
        ),
        rx.dialog.content(
            rx.dialog.title("Додаткова тека проєкту"),
            rx.dialog.description(
                "Агент зможе читати й писати в цій теці (за абсолютним шляхом). Якщо теки ще нема — "
                "її буде створено: наприклад, щоб скопіювати туди проєкт і працювати далі в копії.",
                class_name="text-xs text-gray-400",
            ),
            rx.input(placeholder="Абсолютний шлях до теки",
                     value=State.extra_input, on_change=State.set_extra_input,
                     class_name="w-full mt-2"),
            rx.cond(State.extra_error != "",
                    rx.text(State.extra_error, class_name="text-xs text-red-400 mt-1"), rx.fragment()),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.button("Додати", on_click=State.add_extra_dir),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.extra_open, on_open_change=State.set_extra_open,
    )


def extra_dirs_row() -> rx.Component:
    return rx.hstack(
        rx.icon("folder-plus", size=12, class_name="text-gray-500"),
        rx.text("теки:", class_name="text-xs text-gray-500"),
        rx.foreach(
            State.extra_dirs,
            lambda d: rx.hstack(
                rx.text(d, class_name="text-xs text-gray-300 truncate max-w-48", title=d),
                rx.icon("x", size=11, class_name="text-gray-500 cursor-pointer",
                        on_click=lambda: State.remove_extra_dir(d)),
                class_name="items-center gap-1 px-1.5 py-0.5 rounded bg-white/5",
            ),
        ),
        extra_dir_dialog(),
        class_name="items-center gap-2 mt-1 flex-wrap",
    )


def references_row() -> rx.Component:
    return rx.hstack(
        rx.icon("lock", size=12, class_name="text-gray-500"),
        rx.text("джерела:", class_name="text-xs text-gray-500"),
        rx.foreach(
            State.references,
            lambda r: rx.hstack(
                rx.text(r, class_name="text-xs text-gray-300 truncate max-w-40"),
                rx.icon("x", size=11, class_name="text-gray-500 cursor-pointer",
                        on_click=lambda: State.remove_ref(r)),
                class_name="items-center gap-1 px-1.5 py-0.5 rounded bg-white/5",
            ),
        ),
        ref_dialog(),
        class_name="items-center gap-2 mt-1 flex-wrap",
    )


def token_bar() -> rx.Component:
    """Лічильник токенів поточного запиту + індикатор черги — над полем вводу."""
    left = rx.cond(
        (State.tok_out > 0) | State.busy,
        rx.hstack(
            rx.cond(State.busy, rx.spinner(size="1"), rx.fragment()),
            rx.text(State.tokens_label, class_name="text-xs text-gray-500 font-mono"),
            rx.cond(
                State.queued_text != "",
                rx.hstack(rx.icon("clock", size=12, class_name="text-amber-400"),
                          rx.text("у черзі", class_name="text-xs text-amber-400"),
                          class_name="items-center gap-1"),
                rx.fragment(),
            ),
            class_name="items-center gap-2",
        ),
        rx.fragment(),
    )
    return rx.hstack(left, class_name="items-center w-full px-1 mb-1")


def input_box() -> rx.Component:
    return rx.form(
        rx.vstack(
            token_bar(),
            rx.box(
                rx.text_area(
                    placeholder=rx.cond(
                        State.is_chat,
                        "Напишіть повідомлення…  (Enter — надіслати, Shift+Enter — новий рядок)",
                        "Опишіть задачу...  (Enter — надіслати, Shift+Enter — новий рядок)"),
                    id=INPUT_ID, name="task",       # некероване поле (без value/on_change) — див. State.submit_task
                    enter_key_submit=True,
                    class_name="w-full bg-transparent text-gray-100 placeholder:text-gray-500 "
                               "resize-none outline-none border-none",
                    style={"fontSize": State.chat_font_px},
                    rows="2",
                ),
                controls_bar(),
                rx.cond(State.is_chat, rx.fragment(), rx.fragment(extra_dirs_row(), references_row())),
                attachment_chips(),
                class_name="w-full rounded-2xl p-3 border " + BORDER,
                style={"backgroundColor": INPUT},
            ),
            class_name="w-full gap-0",      # ширину задає батьківський контейнер
        ),
        on_submit=State.submit_task,
        reset_on_submit=False,
        class_name="w-full",
    )


@rx.memo
def message_bubble_memo(role: rx.Var[str], content: rx.Var[str], kind: rx.Var[str], thinking: rx.Var[str],
                        atts: rx.Var[str], thumbs: rx.Var[str], font: rx.Var[str]) -> rx.Component:
    """Бульбашка повідомлення, МЕМОЇЗОВАНА (React.memo): поки повідомлення не змінилось, воно не
    перемальовується. Props — ПРОСТІ РЯДКИ (а не обʼєкт повідомлення): при додаванні нового повідомлення Reflex
    замінює весь масив messages, тож обʼєкти всіх повідомлень стають «новими» й React.memo за обʼєктом не спрацьовує,
    а за рядками — спрацьовує. Без цього кожне оновлення стану (стрім, статус, лічильники) заново розбирало markdown
    УСІХ повідомлень чату — у довгому чаті це блокувало вікно на ~секунду за оновлення (лаги вводу)."""
    mine = role == "user"
    return rx.box(
        rx.cond(
            mine,
            rx.vstack(
                rx.cond(
                    thumbs != "",                          # зображення, прикріплені до повідомлення (мініатюри)
                    rx.hstack(
                        rx.foreach(
                            thumbs.split("\n"),
                            lambda u: rx.image(src=u, class_name="h-24 w-auto max-w-[200px] rounded-lg object-cover"),
                        ),
                        class_name="flex-wrap gap-2 mb-1 justify-end",
                    ),
                    rx.fragment(),
                ),
                rx.text(content, class_name="whitespace-pre-wrap text-gray-100",
                        style={"fontSize": font}),
                rx.cond(
                    atts != "",
                    rx.hstack(
                        rx.foreach(
                            atts.split("\n"),
                            lambda n: rx.hstack(
                                rx.icon("file", size=11, class_name="text-gray-400"),
                                rx.text(n, class_name="text-xs text-gray-300 truncate max-w-40"),
                                class_name="items-center gap-1 px-2 py-0.5 rounded-full bg-black/20"),
                        ),
                        class_name="flex-wrap gap-1 mt-1 justify-end",
                    ),
                    rx.fragment(),
                ),
                class_name="items-end gap-0",
            ),
            rx.box(
                rx.cond(
                    thinking != "",
                    rx.el.details(
                        rx.el.summary(
                            rx.cond(kind == "step", "хід виконання", "Роздуми моделі"),
                            class_name="text-xs text-gray-500 cursor-pointer select-none"),
                        rx.markdown(thinking, class_name="max-w-full overflow-x-auto break-words"),
                        class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 max-w-full "
                                   "overflow-y-auto overflow-x-hidden",
                    ),
                    rx.fragment(),
                ),
                rx.markdown(content, class_name="max-w-full overflow-x-auto break-words"),
                style={"fontSize": font},
                class_name="min-w-0 max-w-full",
            ),
        ),
        # user — компактна бульбашка справа; assistant — на всю ширину (вирівняно з полем вводу).
        class_name=rx.cond(
            mine,
            "self-end bg-white/10 max-w-[85%]",
            "self-start bg-white/[0.03] w-full min-w-0",
        ) + " rounded-2xl px-4 py-2.5",
    )


def message_bubble(m: dict) -> rx.Component:
    return message_bubble_memo(role=m["role"], content=m["content"], kind=m["kind"], thinking=m["thinking"],
                               atts=m["attachments"].to(list[str]).join("\n"),
                               thumbs=m["thumbs"].to(list[str]).join("\n"), font=State.chat_font_px)


def pending_bar() -> rx.Component:
    return rx.hstack(
        rx.button("Виконати план", on_click=State.execute_pending,
                  class_name="bg-white text-black rounded-lg px-3 py-1.5 text-sm"),
        rx.button("Відхилити", on_click=State.discard_pending, variant="soft", size="2"),
        class_name="self-start gap-2 mt-1",
    )


def q_option(opt: dict) -> rx.Component:
    """Клікабельний кандидат/підхід — відповідь на питання планувальника."""
    return rx.box(
        rx.text(opt["label"], class_name="text-sm text-gray-100 font-medium"),
        rx.cond(
            opt["detail"] != "",
            rx.text(opt["detail"], class_name="text-xs text-gray-400"),
            rx.fragment(),
        ),
        on_click=lambda: State.answer_planner(opt["label"]),
        class_name="cursor-pointer rounded-lg px-3 py-2 bg-white/5 hover:bg-white/10 "
                   "border " + BORDER,
    )


def question_bar() -> rx.Component:
    """Питання планувальника: клікабельні кандидати + підказка, що можна й вписати."""
    return rx.vstack(
        rx.hstack(rx.foreach(State.q_options, q_option),
                  class_name="flex-wrap gap-2 items-stretch"),
        rx.hstack(
            rx.text("…або впишіть відповідь у поле нижче",
                    class_name="text-xs text-gray-500"),
            rx.button("Скасувати", on_click=State.cancel_question,
                      variant="ghost", size="1", class_name="text-gray-400"),
            class_name="items-center gap-2",
        ),
        class_name="self-start gap-2 mt-1",
    )


def status_line() -> rx.Component:
    return rx.cond(
        State.busy & ~State.streaming,
        rx.hstack(
            rx.spinner(size="1"),
            rx.text(State.status, class_name="text-sm text-gray-400 italic"),
            class_name="self-start items-center gap-2 px-1",
        ),
        rx.fragment(),
    )


def streaming_bubble() -> rx.Component:
    """Тимчасова бульбашка під час стріму: роздуми під катом + контент.
    Лічильник токенів — над полем вводу (token_bar)."""
    return rx.box(
        rx.cond(
            State.stream_thinking != "",
            rx.el.details(
                rx.el.summary("Роздуми моделі",
                              class_name="text-xs text-gray-500 cursor-pointer select-none"),
                # живі роздуми — ПРОСТИЙ текст (і лише хвіст): markdown розбирається заново на кожне оновлення,
                # а роздуми бувають величезні; готові роздуми потім показуються markdown-ом у повідомленні
                rx.text(State.stream_thinking, class_name="whitespace-pre-wrap break-words text-gray-400"),
                class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 max-w-full "
                           "overflow-y-auto overflow-x-hidden",
            ),
            rx.fragment(),
        ),
        rx.markdown(State.stream_content, class_name="max-w-full overflow-x-auto break-words"),
        class_name="self-start bg-white/[0.03] rounded-2xl px-4 py-2.5 w-full min-w-0",
        style={"fontSize": State.chat_font_px},
    )


def activity_panel() -> rx.Component:
    """Жива стрічка «Що робить Ігорь» під катом: кожен запит до моделі та виклик тула
    (який скрипт/команда і який результат) + годинник «остання подія N с тому» —
    видно, чи є рух, чи агент завис."""
    return rx.cond(
        State.activity.length() > 0,
        rx.el.details(
            rx.el.summary(
                rx.hstack(
                    rx.icon("activity", size=14),
                    rx.text("Що робить Ігорь · подій: ", State.activity.length(), " · остання ",
                            rx.el.span(data_ts=State.activity_last, class_name="igor-ago"),
                            class_name="text-xs"),
                    rx.cond(State.busy, rx.spinner(size="1")),
                    class_name="items-center gap-2 text-gray-500 cursor-pointer",
                ),
            ),
            rx.vstack(rx.foreach(State.activity, activity_row_full), gap="2",
                      class_name="mt-2 max-h-96 overflow-y-auto w-full"),
            class_name="w-full rounded-lg border border-white/10 px-3 py-2",
        ),
        rx.fragment(),
    )


def activity_row_full(r: dict) -> rx.Component:
    """Рядок стрічки: статус, час, тул і його команда; нижче — результат (для тулів)."""
    return rx.vstack(
        rx.hstack(
            rx.cond(r["status"] == "run", rx.spinner(size="1"),
                    rx.cond(r["status"] == "ok", rx.text("✓", class_name="text-xs text-green-400"),
                            rx.text("✗", class_name="text-xs text-red-400"))),
            rx.text(r["t"], class_name="text-xs text-gray-600 font-mono shrink-0"),
            rx.text(r["title"], class_name=rx.cond(r["kind"] == "tool", "text-xs text-sky-300 font-mono shrink-0",
                                                   "text-xs text-gray-400 shrink-0")),
            class_name="items-center gap-2 w-full",
        ),
        rx.text(r["detail"], class_name="text-xs text-gray-400 font-mono pl-5 whitespace-pre-wrap break-all"),
        gap="0", class_name="w-full",
    )


def chat_view() -> rx.Component:
    return rx.vstack(
        rx.cond(
            State.hidden_count > 0,
            rx.button(rx.text("Показати раніші повідомлення (", State.hidden_count, ")"),
                      on_click=State.show_earlier, variant="soft", size="1", class_name="self-center shrink-0"),
            rx.fragment()),
        rx.foreach(State.shown_messages, message_bubble),
        rx.cond(State.streaming, streaming_bubble(), rx.fragment()),
        status_line(),
        activity_panel(),
        rx.cond(State.has_pending, pending_bar(), rx.fragment()),
        rx.cond(State.has_question, question_bar(), rx.fragment()),
        id="chat-scroll",
        on_mount=rx.call_script(SCROLL_SETUP_JS),
        class_name="w-full flex-1 overflow-y-auto overflow-x-hidden min-w-0 px-4 min-[900px]:px-[1cm] py-6 gap-3",
        # scrollbar-gutter: stable — скролбар завжди резервує місце праворуч, тож ширина
        # контенту стабільна; поле вводу компенсує ту саму ширину через --sbw (нижче).
        style={"fontSize": State.chat_font_px, "scrollbarGutter": "stable"},
    )


def scroll_down_button() -> rx.Component:
    """Кнопка-стрілка: промотати чат донизу. По центру, над полем вводу."""
    return rx.button(
        rx.icon("arrow-down", size=18),
        on_click=rx.call_script(SCROLL_BOTTOM_JS),
        radius="full", size="2",
        id="scroll-down-btn",
        # Початково сховано; SCROLL_SETUP_JS показує лише коли чат прокручено вгору.
        style={"display": "none"},
        class_name="absolute left-1/2 -translate-x-1/2 bottom-full mb-3 bg-white/10 "
                   "hover:bg-white/20 text-gray-100 shadow-lg backdrop-blur z-10",
    )


def ctx_row(r: dict) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.box(class_name="w-3 h-3 rounded-sm shrink-0", style={"backgroundColor": r["color"]}),
            rx.text(r["label"], class_name="text-sm text-gray-200 grow"),
            rx.text(r["tokens_label"], class_name="text-sm text-gray-300 font-mono"),
            rx.text(r["pct_label"], class_name="text-xs text-gray-500 w-14 text-right font-mono"),
            class_name="items-center gap-2 w-full",
        ),
        rx.cond(r["note"] != "", rx.text(r["note"], class_name="text-xs text-gray-500 pl-5")),
        rx.cond(
            r["compress"] != "",
            rx.hstack(rx.icon("shrink", size=12, class_name="shrink-0 mt-0.5 text-amber-400"),
                      rx.text(r["compress"], class_name="text-xs text-amber-400/90"),
                      class_name="items-start gap-1 pl-5"),
        ),
        gap="1", class_name="w-full",
    )


def _section(title: str, *children, **kw) -> rx.Component:
    return rx.vstack(
        rx.text(title, class_name="text-xs text-gray-500 uppercase tracking-wide"),
        *children, gap="2", class_name="w-full py-3 border-b border-white/10", **kw)


def _kv(r: dict) -> rx.Component:
    return rx.hstack(
        rx.text(r["label"], class_name="text-sm text-gray-500 w-28 shrink-0"),
        rx.text(r["value"], class_name="text-sm text-gray-200 break-all"),
        class_name="items-start gap-2 w-full")


def _folder(r: dict) -> rx.Component:
    return rx.hstack(
        rx.icon("folder", size=14, class_name="text-gray-500 shrink-0 mt-0.5"),
        rx.vstack(
            rx.text(r["name"], class_name="text-sm text-gray-200 break-all"),
            rx.text(r["path"], class_name="text-xs text-gray-600 break-all"),
            gap="0", class_name="grow min-w-0"),
        rx.text(r["tag"], class_name="text-xs text-gray-500 shrink-0"),
        class_name="items-start gap-2 w-full")


def _change(r: dict) -> rx.Component:
    return rx.hstack(
        rx.text(r["path"], class_name="text-xs text-gray-300 font-mono break-all grow min-w-0"),
        rx.text(r["delta"], class_name="text-xs text-gray-500 font-mono shrink-0"),
        class_name="items-start gap-2 w-full")


def gpu_card() -> rx.Component:
    """Верх правої панелі: чи відповідає сервер і навантаження GPU (вмикається в Налаштуваннях).
    Картка СТАТИЧНА: текст і графік (canvas, 5 хв) оновлює скрипт GPU_JS, який бекенд викликає подією
    (poll_gpu) — без змінних стану, тож React не перемальовує інтерфейс і ввід не підвисає."""
    body = rx.vstack(
        rx.hstack(rx.box(id="gpu-dot", class_name="w-2.5 h-2.5 rounded-full shrink-0",
                         style={"backgroundColor": "#6b7280"}),
                  rx.text("Опитую сервер…", id="gpu-status", class_name="text-sm text-gray-200"),
                  class_name="items-center gap-2 w-full"),
        rx.vstack(
            rx.hstack(rx.text("ГПУ · останні 5 хв", class_name="text-xs text-gray-500"), rx.spacer(),
                      rx.text("", id="gpu-util", class_name="text-lg font-mono"),
                      class_name="w-full items-end"),
            rx.el.canvas(id="gpu-canvas", class_name="w-full rounded",
                         style={"height": "64px", "display": "block", "backgroundColor": "rgba(255,255,255,0.04)"}),
            rx.hstack(rx.text("−5 хв", class_name="text-[10px] text-gray-600"), rx.spacer(),
                      rx.text("50%", class_name="text-[10px] text-gray-600"), rx.spacer(),
                      rx.text("зараз", class_name="text-[10px] text-gray-600"), class_name="w-full"),
            rx.text("", id="gpu-line", class_name="text-xs text-gray-400 font-mono"),
            rx.text("", id="gpu-vram", class_name="text-xs text-gray-400 font-mono"),
            rx.text("", id="gpu-model", class_name="text-xs text-gray-400"),
            rx.text("VRAM ~90% — норма: vLLM резервує пам'ять наперед. Навантаження — це ГПУ та Вт.",
                    class_name="text-xs text-gray-600"),
            id="gpu-graph", gap="1", class_name="w-full"),
        rx.text("", id="gpu-hint", class_name="text-xs text-gray-500"),
        gap="2", class_name="w-full",
        on_mount=rx.call_script("window.__igorGpuRender&&window.__igorGpuRender()"))   # картку могли перемонтувати
    # Початково прихована: JS (GPU_JS) показує картку, коли прийшли дані/стан сервера, і ховає для локальної Ollama
    # та «не налаштовано» — там монітора немає, і порожня картка була б шумом.
    return rx.cond(State.gpu_enabled, _section("Сервер", body, id="gpu-card", style={"display": "none"}),
                   rx.fragment())


def context_panel() -> rx.Component:
    """Постійна права панель (як у Claude Code): середовище, контекст (що займає вікно
    моделі + дані про стиснення), теки/файли чату, зміни в git. Ховається на вузьких вікнах."""
    context = rx.vstack(
        rx.hstack(
            rx.text(State.ctx_pct, "% використано", class_name="text-sm text-gray-200"),
            rx.spacer(),
            rx.text(State.ctx_total, class_name="text-sm text-gray-400 font-mono"),
            class_name="w-full items-center",
        ),
        rx.hstack(
            rx.foreach(State.ctx_segments,
                       lambda s: rx.box(style={"width": s["width"], "backgroundColor": s["color"]},
                                        class_name="h-full")),
            class_name="w-full h-2 rounded-full overflow-hidden gap-px",
            style={"backgroundColor": "#333"},
        ),
        rx.vstack(rx.foreach(State.ctx_rows, ctx_row), gap="3", class_name="w-full mt-1"),
        rx.hstack(
            rx.box(class_name="w-3 h-3 rounded-sm shrink-0", style={"backgroundColor": "#4b4b4b"}),
            rx.text("Вільно", class_name="text-sm text-gray-400 grow"),
            rx.text(State.ctx_free, class_name="text-sm text-gray-400 font-mono"),
            class_name="items-center gap-2 w-full",
        ),
        rx.cond(State.tok_out > 0,
                rx.text("Останній запит: ", State.tokens_label, class_name="text-xs text-gray-500 font-mono")),
        rx.text("Токени — оцінка за символами. Підсумок розмови, дайджести файлів і нотатки тем "
                "стискаються автоматично при перевищенні порогу; про кожне стиснення Ігорь пише "
                "в чаті й у лозі.", class_name="text-xs text-gray-600"),
        gap="3", class_name="w-full")
    changes = rx.cond(
        State.git_is_repo & (State.mode == "code"),
        _section(
            "Зміни",
            rx.hstack(
                rx.text(State.git_summary, class_name="text-sm font-mono text-green-400"),
                rx.text("у ", State.git_branch, class_name="text-xs text-gray-500"),
                class_name="items-center gap-2"),
            rx.foreach(State.git_files, _change),
            rx.cond(State.git_more > 0,
                    rx.text("… ще ", State.git_more, class_name="text-xs text-gray-600")),
        ),
        rx.fragment(),
    )
    return rx.box(
        rx.vstack(
            rx.heading("Ігорь", size="3", class_name="mb-1"),
            gpu_card(),
            _section("Середовище", rx.foreach(State.env_rows, _kv)),
            _section("Контекст", context),
            rx.cond(State.folder_rows.length() > 0,
                    _section("Теки та файли", rx.foreach(State.folder_rows, _folder)),
                    rx.fragment()),
            changes,
            gap="0", class_name="w-full",
        ),
        class_name="hidden lg:block w-[320px] shrink-0 h-full overflow-y-auto p-4 border-l border-white/10",
        style={"backgroundColor": PANEL},
    )


def main_area() -> rx.Component:
    return rx.cond(
        State.messages,
        rx.vstack(
            chat_view(),
            rx.box(
                scroll_down_button(),
                input_box(),
                # Праворуч додаємо ширину скролбара (--sbw), щоб рамка поля вводу
                # збігалася з правим краєм відповіді (чат має скролбар, інпут — ні).
                class_name="w-full min-w-0 pl-4 pr-[calc(1rem+var(--sbw,0px))] "
                           "min-[900px]:pl-[1cm] min-[900px]:pr-[calc(1cm+var(--sbw,0px))] pb-4 relative",
            ),
            class_name="flex-1 h-full w-full min-w-0 relative",
        ),
        rx.center(
            rx.vstack(
                rx.heading(
                    rx.cond(State.has_current, State.title, "Що робимо?"),
                    class_name="text-4xl text-gray-200 mb-2", style=SERIF,
                ),
                input_box(),
                spacing="5",
                class_name="w-full max-w-2xl items-center px-4",
            ),
            class_name="flex-1 h-full min-w-0",
        ),
    )
