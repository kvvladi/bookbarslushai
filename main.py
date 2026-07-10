import os
import json
import random
import logging
import requests
import telebot
from telebot import types
from dotenv import load_dotenv
from flask import Flask, request

load_dotenv()

TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана.")

bot = telebot.TeleBot(TOKEN, threaded=False)

logger = telebot.logger
telebot.logger.setLevel(logging.DEBUG)

# --- Интеграция с открытым поисковым API ЛитРес ---
# Публичный эндпоинт поиска. Для книг обязателен параметр types
# (допустимые значения: text_book, audiobook, paper_book, ...).
LITRES_SEARCH_API = "https://api.litres.ru/foundation/api/search"
LITRES_BASE = "https://www.litres.ru"      # канонические страницы книг
LITRES_CDN = "https://cdn.litres.ru"        # хост обложек (без редиректа ddos-guard)

# Лимит длины подписи (caption) в Telegram — 1024 символа.
CAPTION_LIMIT = 1024


# --- Маппинг эмоциональных категорий на поисковые запросы ЛитРес ---
# Ключ — наша категория из меню, значение — ключевые слова для поиска
# по каталогу ЛитРес (жанры/тематики на русском).
MOOD_TO_LITRES = {
    "Уютный вечер": "современная проза бестселлеры",
    "Хочу острых ощущений": "остросюжетный триллер боевик",
    "Немного поплакать": "лирическая проза драма",
    "Пища для ума": "научно-популярная литература",
    "Лёгкость и смех": "юмористическая проза",
    "Уйти от реальности": "фэнтези фантастика",
    "Закрытый клуб": "интеллектуальный детектив классика",
    "Проглотить за одну ночь": "зарубежный детектив бестселлеры",
    "Моральный детокс": "современная проза бестселлеры",
}

# Сколько топовых результатов рассматриваем при случайном выборе.
LITRES_TOP_N = 20


def get_litres_random_book(category: str) -> dict | None:
    """Ищет случайную книгу по категории напрямую через API ЛитРес.

    Берёт ключевые слова для категории из MOOD_TO_LITRES, делает запрос к
    поисковому API и выбирает случайную книгу из топа результатов.

    Возвращает словарь, нормализованный под нашу схему книги:
        - Название, Автор, Ссылка — для полки и отображения,
        - Описание               — аннотация/подзаголовок с ЛитРес,
        - cover_url, rating, book_url — данные для карточки.
    При ошибке запроса или отсутствии книг — возвращает None (fallback).
    """
    query = MOOD_TO_LITRES.get(category)
    if not query:
        return None

    params = {
        "q": query,
        "types": "text_book",
        "limit": LITRES_TOP_N,
    }

    try:
        resp = requests.get(LITRES_SEARCH_API, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        # Сеть недоступна / API вернул не-JSON / некорректный статус.
        return None

    payload = data.get("payload") or {}
    items = payload.get("data") or []
    if not items:
        return None

    # Случайная книга из топа (до LITRES_TOP_N результатов выдачи).
    pick = random.choice(items[:LITRES_TOP_N])
    instance = pick.get("instance") or {}

    cover = instance.get("cover_url")
    if not cover:
        return None
    if cover.startswith("/"):
        cover = LITRES_CDN + cover

    url = instance.get("url")
    if url and url.startswith("/"):
        url = LITRES_BASE + url

    persons = instance.get("persons") or []
    authors = [
        p.get("full_name")
        for p in persons
        if p.get("role") == "author" and p.get("full_name")
    ]
    author = ", ".join(authors) if authors else "Неизвестный автор"

    rating_obj = instance.get("rating") or {}
    rating = rating_obj.get("rated_avg")

    # Полноценной аннотации в поисковом API нет — берём подзаголовок
    # (часто это описание серии/издания). Пустые значения позже заменяются
    # на короткую заметку при формировании карточки.
    annotation = (instance.get("subtitle") or "").strip()

    return {
        "Название": instance.get("title", "—"),
        "Автор": author,
        "Ссылка": url,
        "Описание": annotation,
        "Послевкусие": "",
        "cover_url": cover,
        "rating": rating,
        "book_url": url,
    }


def _build_litres_caption(book: dict) -> str:
    """Собирает HTML-подпись к карточке ЛитРес и обрезает до лимита (1024)."""
    title = book.get("Название", "—")
    author = book.get("Автор", "—")
    annotation = (book.get("Описание") or "").strip()
    if not annotation:
        annotation = (
            "Краткое описание недоступно на ЛитРес — загляни на страницу "
            "книги по ссылке ниже."
        )
    rating = book.get("rating")
    book_url = book.get("book_url") or book.get("Ссылка") or ""

    if rating is not None and rating > 0:
        full = max(1, min(5, int(round(rating))))
        stars = "⭐" * full
        rating_line = f"⭐ <b>Рейтинг ЛитРес:</b> {stars} {rating}\n"
    else:
        rating_line = "⭐ <b>Рейтинг ЛитРес:</b> пока нет оценок\n"

    link_line = f"🔗 <a href=\"{book_url}\">Читать на ЛитРес</a>\n" if book_url else ""

    # Подводка сомелье: используем кураторское «послевкусие» (если есть,
    # например, из books.json), иначе — случайную подводку.
    intro = (book.get("Послевкусие") or "").strip()
    if not intro:
        intro = random.choice(SOMMELIER_INTROS).replace("🍷 ", "", 1)

    header = (
        f"📖 <b>{title}</b>\n"
        f"✍️ <i>{author}</i>\n\n"
        f"📝 <b>Аннотация:</b>\n"
    )
    footer = (
        f"\n🥂 <b>Послевкусие</b>\n\n{intro}\n\n"
        f"{rating_line}"
        f"{link_line}"
    )

    caption = header + annotation + footer
    if len(caption) > CAPTION_LIMIT:
        # Обрезаем аннотацию, оставляя запас под многоточие.
        allowed = CAPTION_LIMIT - len(header) - len(footer) - 1
        if allowed > 0:
            annotation = annotation[:allowed].rstrip() + "…"
            caption = header + annotation + footer
        else:
            # Заголовок + подвал сами по себе не влезают — режем всё.
            caption = (header + footer)[: CAPTION_LIMIT - 1] + "…"
    return caption


def _send_litres_card(chat_id: int, book: dict, shelf_kb) -> "types.Message":
    """Отправляет карточку книги (обложка + подпись) из данных ЛитРес.
    При неудаче с фото откатывается на текстовое сообщение."""
    caption = _build_litres_caption(book)
    try:
        return bot.send_photo(
            chat_id,
            photo=book["cover_url"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=shelf_kb,
        )
    except telebot.apihelper.ApiException:
        intro = random.choice(SOMMELIER_INTROS).replace("🍷 ", "", 1)
        return _send_book_text(chat_id, book, intro, shelf_kb)


# --- Полки пользователей (отложенные книги) ---
SHELVES_FILE = "shelves.json"
# Полки: chat_id (str) -> [ {Название, Автор, Ссылка}, ... ]
shelves: dict[str, list[dict]] = {}
if os.path.exists(SHELVES_FILE):
    try:
        with open(SHELVES_FILE, "r", encoding="utf-8") as _f:
            shelves = json.load(_f)
    except (json.JSONDecodeError, OSError):
        shelves = {}

# Книга, показанная в конкретном сообщении (по message_id), чтобы кнопка
# «Отложить на полку» добавляла именно её, а не последнюю показанную.
pending_book: dict[int, dict] = {}


def save_shelves() -> None:
    with open(SHELVES_FILE, "w", encoding="utf-8") as _f:
        json.dump(shelves, _f, ensure_ascii=False, indent=2)


def add_to_shelf(chat_id: int, book: dict) -> bool:
    """Добавляет книгу на полку пользователя. Возвращает False, если уже есть
    (дедуп по названию)."""
    cid = str(chat_id)
    shelf = shelves.setdefault(cid, [])
    if any(b.get("Название") == book.get("Название") for b in shelf):
        return False
    shelf.append({
        "Название": book.get("Название"),
        "Автор": book.get("Автор"),
        "Ссылка": book.get("Ссылка", ""),
    })
    save_shelves()
    return True


def remove_from_shelf(chat_id: int, book: dict) -> bool:
    """Убирает книгу с полки пользователя (дедуп по названию).
    Возвращает True, если книга была найдена и удалена."""
    cid = str(chat_id)
    shelf = shelves.get(cid)
    if not shelf:
        return False
    new_shelf = [b for b in shelf if b.get("Название") != book.get("Название")]
    if len(new_shelf) == len(shelf):
        return False
    shelves[cid] = new_shelf
    save_shelves()
    return True


def clear_shelf(chat_id: int) -> bool:
    """Полностью очищает полку пользователя. Возвращает True, если на полке
    что-то было."""
    cid = str(chat_id)
    if not shelves.get(cid):
        return False
    shelves[cid] = []
    save_shelves()
    return True


def show_shelf(chat_id: int) -> None:
    """Отправляет пользователю список отложенных книг с inline-кнопкой очистки."""
    shelf = shelves.get(str(chat_id), [])
    if not shelf:
        bot.send_message(
            chat_id,
            "📚 Твоя полка пока пуста. Откладывай понравившиеся книги кнопкой "
            "«📌 Отложить на полку» под каждой рекомендацией.",
            reply_markup=get_main_keyboard(),
        )
        return
    lines = []
    for i, b in enumerate(shelf, 1):
        title = b.get("Название", "—")
        author = b.get("Автор", "—")
        link = b.get("Ссылка")
        if link:
            lines.append(f"{i}. <a href=\"{link}\">{title}</a> — {author}")
        else:
            lines.append(f"{i}. {title} — {author}")
    shelf_kb = types.InlineKeyboardMarkup()
    shelf_kb.row(
        types.InlineKeyboardButton("🧹 Очистить полку", callback_data="shelf_clear"),
    )
    bot.send_message(
        chat_id,
        "📚 <b>Твоя полка:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=shelf_kb,
    )


# Приветственное сообщение
welcome_text = (
    "🍷 Привет! Располагайся, ты в «Книжном сомелье».\n\n"
    "Тут без душных лекций по литературе: моя задача — достать с полок "
    "именно тот текстовый винтаж, который идеально залетит под твое состояние прямо сейчас.\n\n"
    "Устроим уютный чилл, хорошенько встряхнем мозг или устроим знатный эмоциональный детокс?\n\n"
    "Прислушайся к себе и выбирай настроение на кнопках ниже. Сейчас всё организуем 👇"
)

# Стилизованные подводки сомелье, добавляемые к описанию от API
SOMMELIER_INTROS = [
    "🍷 Сомелье советует: эту историю лучше декантировать — дай ей настояться пару глав, и аромат раскроется.",
    "🍷 Сомелье шепчет: подавай при комнатной температуре и с полным отключением уведомлений.",
    "🍷 Сомелье замечает: букет здесь многослойный, не спеши — первый глоток редко говорит всё.",
    "🍷 Сомелье рекомендует: идеальная пара к этому тексту — мягкое кресло и тишина без претензий.",
    "🍷 Сомелье предупреждает: напиток коварный, затягивает быстрее, чем кажется на вид.",
    "🍷 Сомелье кивает: терпкое, благородное чтиво — оставит приятное послевкусие ещё на пару дней.",
    "🍷 Сомелье настаивает: читай не торопясь, как будто смакуешь редкий винтаж у камина.",
    "🍷 Сомелье улыбается: тут есть тот самый «хмельной» поворот, ради которого стоит дочитать до дна.",
]

# --- Клавиатуры ---

def get_main_keyboard():
    """Главное меню: минимум кнопок."""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_mood = types.KeyboardButton("🎭 Выбрать настроение")
    btn_shelf = types.KeyboardButton("📚 Моя полка")
    keyboard.add(btn_mood, btn_shelf)
    return keyboard


def get_mood_inline_keyboard():
    """Inline-клавиатура с вариантами настроения."""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🍷 Уютный вечер", callback_data="mood_Уютный вечер"),
        types.InlineKeyboardButton("🌪 Хочу острых ощущений", callback_data="mood_Хочу острых ощущений"),
        types.InlineKeyboardButton("💧 Немного поплакать", callback_data="mood_Немного поплакать"),
        types.InlineKeyboardButton("🧠 Пища для ума", callback_data="mood_Пища для ума"),
        types.InlineKeyboardButton("😄 Лёгкость и смех", callback_data="mood_Лёгкость и смех"),
        types.InlineKeyboardButton("✈️ Уйти от реальности", callback_data="mood_Уйти от реальности"),
        types.InlineKeyboardButton("🏛️ Закрытый клуб", callback_data="mood_Закрытый клуб"),
        types.InlineKeyboardButton("☕️ Проглотить за одну ночь", callback_data="mood_Проглотить за одну ночь"),
        types.InlineKeyboardButton("🔋 Моральный детокс", callback_data="mood_Моральный детокс"),
    )
    return keyboard


# --- Обработчики бота ---

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda message: True)
def handle_main_menu(message):
    text = message.text.strip()

    # Просмотр полки отложенных книг
    if text == "📚 Моя полка":
        show_shelf(message.chat.id)
        return

    # Открытие меню выбора настроения
    if text == "🎭 Выбрать настроение":
        bot.send_message(
            message.chat.id,
            "🎭 <b>Выбери настроение:</b>",
            parse_mode="HTML",
            reply_markup=get_mood_inline_keyboard(),
        )
        return

    # Если пользователь нажал что-то другое — подсказываем меню
    bot.send_message(
        message.chat.id,
        "Пожалуйста, выбери действие из меню ниже 👇",
        reply_markup=get_main_keyboard(),
    )


# --- Кураторская подборка books.json (локальный fallback) ---
BOOKS_FILE = "books.json"
_curated_cache: dict | None = None


def _load_curated() -> dict:
    """Загружает кураторскую подборку из books.json (с кэшированием)."""
    global _curated_cache
    if _curated_cache is None:
        try:
            with open(BOOKS_FILE, "r", encoding="utf-8") as f:
                _curated_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _curated_cache = {}
    return _curated_cache


def get_book_from_json(category: str) -> dict | None:
    """Случайная книга из books.json по категории (fallback-источник).

    Возвращает словарь в нашей схеме (Название, Автор, Описание, Послевкусие)
    или None, если для категории нет книг в подборке.
    """
    books = _load_curated().get(category)
    if not books:
        return None
    b = random.choice(books)
    return {
        "Название": b.get("title", "—"),
        "Автор": b.get("author", "—"),
        "Описание": b.get("annotation", ""),
        "Послевкусие": b.get("aftertaste", ""),
        "Ссылка": "",
    }


def search_litres_by_title(title: str, author: str) -> dict | None:
    """Ищет конкретную книгу на ЛитРес по названию+автору, чтобы обогатить
    книгу из books.json обложкой и прямой ссылкой.

    Возвращает {cover_url, book_url, rating} или None при ошибке/отсутствии.
    """
    if not title:
        return None
    query = f"{title} {author}".strip() if author else title
    params = {"q": query, "types": "text_book", "limit": 5}
    try:
        resp = requests.get(LITRES_SEARCH_API, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    items = (data.get("payload") or {}).get("data") or []
    if not items:
        return None

    instance = items[0].get("instance") or {}
    cover = instance.get("cover_url")
    if not cover:
        return None
    if cover.startswith("/"):
        cover = LITRES_CDN + cover

    url = instance.get("url")
    if url and url.startswith("/"):
        url = LITRES_BASE + url

    rating = (instance.get("rating") or {}).get("rated_avg")
    return {"cover_url": cover, "book_url": url, "rating": rating}


def _send_book_text(chat_id: int, book: dict, intro_text: str, shelf_kb) -> "types.Message":
    """Fallback-режим: отправляет книгу обычным текстовым сообщением
    (старое поведение бота, до интеграции с ЛитРес)."""
    link = book.get("Ссылка")
    link_line = f"🔗 <a href=\"{link}\">Читать на ЛитРес</a>\n\n" if link else ""
    response = (
        f"📖 <b>{book['Название']}</b>\n"
        f"✍️ <i>{book['Автор']}</i>\n\n"
        f"📝 <b>Аннотация:</b>\n{book['Описание']}\n\n"
        f"{link_line}"
        f"🥂 <b>Послевкусие</b>\n\n{intro_text}"
    )
    return bot.send_message(chat_id, response, parse_mode="HTML", reply_markup=shelf_kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("mood_"))
def on_mood_selected(call):
    """Обработка выбора настроения из inline-клавиатуры.

    Сначала пытаемся найти книгу по категории напрямую через ЛитРес.
    Если API недоступно или ничего не найдено — берём книгу из нашей
    подборки books.json (fallback).
    """
    category = call.data[len("mood_"):]

    # 1) Пробуем ЛитРес по категории (обложка + аннотация + рейтинг + ссылка).
    litres_book = get_litres_random_book(category)
    if litres_book:
        shelf_kb = types.InlineKeyboardMarkup()
        shelf_kb.row(
            types.InlineKeyboardButton("📌 Отложить на полку", callback_data="shelf_add"),
            types.InlineKeyboardButton("🗑 Убрать с полки", callback_data="shelf_remove"),
        )
        sent = _send_litres_card(call.message.chat.id, litres_book, shelf_kb)
        pending_book[sent.message_id] = litres_book
        bot.answer_callback_query(call.id)
        return

    # 2) Fallback: случайная книга из нашей подборки books.json.
    book = get_book_from_json(category)
    if not book:
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "😔 В этой подборке пока пусто. Загляни в другой погреб — "
            "выбери настроение на кнопках ниже 👇",
            reply_markup=get_mood_inline_keyboard(),
        )
        return

    # Inline-кнопки под каждой рекомендацией: отложить на полку и убрать с полки
    shelf_kb = types.InlineKeyboardMarkup()
    shelf_kb.row(
        types.InlineKeyboardButton("📌 Отложить на полку", callback_data="shelf_add"),
        types.InlineKeyboardButton("🗑 Убрать с полки", callback_data="shelf_remove"),
    )

    # Пробуем обогатить книгу из books.json обложкой и ссылкой с ЛитРес,
    # чтобы показать её в том же «карточном» формате, что и основную выдачу.
    enrich = search_litres_by_title(book["Название"], book["Автор"])
    if enrich and enrich.get("cover_url"):
        book["cover_url"] = enrich["cover_url"]
        book["book_url"] = enrich.get("book_url") or ""
        book["rating"] = enrich.get("rating")
        book["Ссылка"] = book["book_url"]
        sent = _send_litres_card(call.message.chat.id, book, shelf_kb)
    else:
        # ЛитРес недоступен для обогащения — отправляем текстом (наша
        # кураторская аннотация и «послевкусие» сохраняются).
        intro_text = book.get("Послевкусие") or random.choice(SOMMELIER_INTROS).replace("🍷 ", "", 1)
        sent = _send_book_text(call.message.chat.id, book, intro_text, shelf_kb)

    pending_book[sent.message_id] = book
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "shelf_add")
def on_shelf_add(call):
    """Обработка кнопки «Отложить на полку» под рекомендацией."""
    book = pending_book.get(call.message.message_id)
    if not book:
        bot.answer_callback_query(call.id, "Книга уже недоступна для добавления.")
        return
    added = add_to_shelf(call.message.chat.id, book)
    if added:
        bot.answer_callback_query(call.id, "📌 Добавлено на полку!")
    else:
        bot.answer_callback_query(call.id, "Эта книга уже на твоей полке.")


@bot.callback_query_handler(func=lambda call: call.data == "shelf_remove")
def on_shelf_remove(call):
    """Обработка кнопки «Убрать с полки» под рекомендацией."""
    book = pending_book.get(call.message.message_id)
    if not book:
        bot.answer_callback_query(call.id, "Книга уже недоступна для удаления.")
        return
    removed = remove_from_shelf(call.message.chat.id, book)
    if removed:
        bot.answer_callback_query(call.id, "🗑 Убрано с полки.")
    else:
        bot.answer_callback_query(call.id, "Этой книги и так нет на полке.")


@bot.callback_query_handler(func=lambda call: call.data == "shelf_clear")
def on_shelf_clear(call):
    """Обработка inline-кнопки «Очистить полку» из просмотра полки."""
    if not shelves.get(str(call.message.chat.id)):
        bot.answer_callback_query(call.id, "Полка уже пуста.")
        return
    confirm_kb = types.InlineKeyboardMarkup()
    confirm_kb.row(
        types.InlineKeyboardButton("✅ Да, очистить", callback_data="shelf_clear_confirm"),
        types.InlineKeyboardButton("❌ Нет, оставить", callback_data="shelf_clear_cancel"),
    )
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=confirm_kb,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "shelf_clear_confirm")
def on_shelf_clear_confirm(call):
    """Подтверждение очистки полки."""
    cleared = clear_shelf(call.message.chat.id)
    if cleared:
        bot.answer_callback_query(call.id, "🧹 Полка очищена.")
        bot.edit_message_text(
            "🧹 Полка очищена. Откладывай новые книги кнопкой "
            "«📌 Отложить на полку» под рекомендациями.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    else:
        bot.answer_callback_query(call.id, "Полка уже пуста.")
    bot.send_message(
        call.message.chat.id,
        "Чем займёмся дальше? 👇",
        reply_markup=get_main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "shelf_clear_cancel")
def on_shelf_clear_cancel(call):
    """Отмена очистки полки."""
    bot.answer_callback_query(call.id, "Оставили как есть.")
    bot.edit_message_text(
        "Хорошо, оставили полку как есть. 📚",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )


# --- Flask-приложение для Webhooks ---

APP_URL = os.environ.get('APP_URL', 'https://your-app.onrender.com')
PORT = int(os.environ.get('PORT', 5000))

app = Flask(__name__)


@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    """Принимаем обновления от Telegram."""
    if request.headers.get('content-type') != 'application/json':
        return 'Invalid content type', 400

    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return '!', 200


@app.route('/')
def index():
    """Устанавливаем webhook при старте/деплое."""
    bot.remove_webhook()
    success = bot.set_webhook(url=APP_URL + TOKEN)
    if success:
        return f'Webhook установлен: {APP_URL + TOKEN}', 200
    else:
        return 'Не удалось установить webhook', 500


if __name__ == "__main__":
    print(welcome_text)
    print("Бот «Книжный сомелье» запущен в режиме Webhooks...")
    app.run(host='0.0.0.0', port=PORT)
