import os
import json
import random
import logging
import sqlite3
import traceback
import hashlib
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

# --- База данных SQLite для полки пользователей ---
DB_PATH = "books.db"

def init_db() -> None:
    """Создаёт таблицу полки пользователей, если она не существует."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shelves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT,
                link TEXT,
                status TEXT NOT NULL DEFAULT 'saved',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shelves_chat_status
            ON shelves (chat_id, status)
        """)
        conn.commit()
    finally:
        conn.close()

def get_db() -> sqlite3.Connection:
    """Возвращает подключение к БД с разрешённым доступом из разных потоков."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# --- Маппинг эмоциональных категорий на поисковые запросы ЛитРес ---
# Ключ — наша категория из меню, значение — ключевые слова для поиска
# по каталогу ЛитРес (жанры/тематики на русском).
MOOD_TO_LITRES = {
    "Уютный вечер": "современная проза уютное бестселлеры",
    "Хочу острых ощущений": "остросюжетный триллер боевик бестселлеры",
    "Немного поплакать": "лирическая проза драма бестселлеры",
    "Пища для ума": "научно-популярная литература хиты",
    "Лёгкость и смех": "юмористическая проза бестселлеры",
    "Уйти от реальности": "фантастика фэнтези популярное",
    "Закрытый клуб": "интеллектуальный детектив классика популярное",
    "Проглотить за одну ночь": "остросюжетный детектив триллер бестселлеры",
    "Моральный детокс": "современная проза вдохновляющие бестселлеры",
}

# Сколько топовых результатов рассматриваем при случайном выборе.
LITRES_TOP_N = 20


def get_litres_random_book(category: str) -> dict | None:
    """Ищет случайную книгу по категории напрямую через API ЛитРес.

    Берёт ключевые слова для категории из MOOD_TO_LITRES, делает запрос к
    поисковому API с сортировкой по популярности (sort=popular) и выбирает
    случайную книгу из топа-LITRES_TOP_N самых популярных результатов.

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
        "limit": 40,            # берём расширенную выдачу, чтобы отобрать топ
        "sort": "popular",      # сортировка по популярности (бестселлеры выше)
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

    # Жёсткий фильтр: оставляем только книги с реальным рейтингом
    # (рейтинг строго больше 0 и не равен None), чтобы не выдавать
    # неоценённые самоиздаты. Фильтруем ДО отбора топа по популярности,
    # чтобы пул кандидатов состоял исключительно из оценённых книг.
    def _has_valid_rating(item: dict) -> bool:
        inst = item.get("instance") or {}
        rating_obj = inst.get("rating") or {}
        rating = rating_obj.get("rated_avg")
        return rating is not None and rating > 0

    items = [item for item in items if _has_valid_rating(item)]
    if not items:
        # Ни одной книги с рейтингом не найдено — возвращаем None,
        # чтобы сработал стандартный fallback (выдача из books.json).
        return None

    # Берём только топ-LITRES_TOP_N самых популярных книг из ответа API
    # (выдача уже отсортирована по популярности через sort=popular) и из
    # этого «элитного» списка выбираем одну случайную книгу.
    # Сначала оставляем только те, у которых есть обложка, чтобы не возвращать
    # None из-за отсутствия картинки у случайно выбранного элемента.
    candidates = []
    for item in items[:LITRES_TOP_N]:
        inst = item.get("instance") or {}
        if not inst.get("cover_url"):
            continue

        # Авторы в ЛитРес могут лежать в списке persons как на уровне
        # результата поиска, так и внутри instance — проверяем оба места.
        # Учитываем явную роль «author», а при её отсутствии (нет поля role)
        # считаем person автором, если у него есть имя.
        persons = item.get("persons") or inst.get("persons") or []
        authors = [
            (p.get("full_name") or p.get("name"))
            for p in persons
            if (p.get("role") in (None, "author", "Автор"))
            and (p.get("full_name") or p.get("name"))
        ]
        author = ", ".join(authors) if authors else "Неизвестный автор"

        rating_obj = inst.get("rating") or {}
        rating = rating_obj.get("rated_avg")

        # Полноценной аннотации в поисковом API нет — берём подзаголовок
        # (часто это описание серии/издания). Пустые значения позже заменяются
        # на короткую заметку при формировании карточки.
        annotation = (inst.get("subtitle") or "").strip()
        book_url = inst.get("url") or ""
        if book_url.startswith("/"):
            book_url = LITRES_BASE + book_url

        candidates.append({
            "Название": inst.get("title", "—"),
            "Автор": author,
            "Ссылка": book_url,
            "Описание": annotation,
            "Послевкусие": "",
            "cover_url": inst.get("cover_url"),
            "rating": rating,
            "book_url": book_url,
        })
    if not candidates:
        return None
    return random.choice(candidates)


def _build_litres_caption(book: dict) -> str:
    """Собирает HTML-подпись к карточке ЛитРес и обрезает до лимита (1024).

    Формат карточки:
        Название книги
        Автор: Имя Автора
        Рейтинг: ⭐ 4.5
        Аннотация...
    """
    title = book.get("Название", "—")
    author = book.get("Автор", "—")
    rating = book.get("rating")
    book_url = book.get("book_url") or book.get("Ссылка") or ""

    # Рейтинг в формате «⭐ 4.5» (одна звезда-маркер + числовое значение).
    if rating is not None and rating > 0:
        rating_line = f"Рейтинг: ⭐ {rating}"
    else:
        rating_line = "Рейтинг: пока нет оценок"

    # Аннотация (показываем только если не пустая).
    annotation = (book.get("Описание") or "").strip()
    ann_block = f"\n\n{annotation}" if annotation else ""

    # Подводка сомелье: используем кураторское «послевкусие» (если есть,
    # например, из books.json), иначе — случайную подводку.
    intro = (book.get("Послевкусие") or "").strip()
    if not intro:
        intro = random.choice(SOMMELIER_INTROS).replace("🍷 ", "", 1)

    link_line = f"\n\n🔗 <a href=\"{book_url}\">Читать на ЛитРес</a>" if book_url else ""

    header = (
        f"📖 <b>{title}</b>\n"
        f"Автор: {author}\n"
        f"{rating_line}"
    )
    footer = (
        f"{ann_block}"
        f"\n\n🥂 <b>Послевкусие</b>\n\n{intro}"
        f"{link_line}"
    )

    caption = header + footer
    if len(caption) > CAPTION_LIMIT:
        # Обрезаем аннотацию/подводку, оставляя базовые поля (название,
        # автор, рейтинг) и ссылку, с запасом под многоточие.
        allowed = CAPTION_LIMIT - len(header) - len(link_line) - 1
        if allowed > 0:
            body = (ann_block + f"\n\n🥂 <b>Послевкусие</b>\n\n{intro}").strip()
            body = body[:allowed].rstrip() + "…"
            caption = header + "\n\n" + body + link_line
        else:
            # Заголовок + ссылка сами по себе не влезают — режем всё.
            caption = (header + link_line)[: CAPTION_LIMIT - 1] + "…"
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
# Книга, показанная в конкретном сообщении (по message_id), чтобы кнопка
# «На полку» / «Уже читал» добавляла именно её, а не последнюю показанную.
pending_book: dict[int, dict] = {}

# Текущая выбранная категория (настроение) каждого пользователя.
# Хранится в памяти процесса: при перезапуске бота сбрасывается, что
# приемлемо — пользователь просто заново выбирает настроение в меню.
user_current_category: dict[int, str] = {}


def add_to_shelf(chat_id: int, book: dict, status: str = "saved") -> bool:
    """Добавляет книгу на полку пользователя с указанным статусом.
    Возвращает False, если такая книга уже есть у пользователя (дедуп по названию)."""
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT id FROM shelves WHERE chat_id = ? AND title = ?",
            (str(chat_id), book.get("Название", "")),
        )
        if cur.fetchone():
            return False
        conn.execute(
            "INSERT INTO shelves (chat_id, title, author, link, status) VALUES (?, ?, ?, ?, ?)",
            (
                str(chat_id),
                book.get("Название", ""),
                book.get("Автор", ""),
                book.get("Ссылка", ""),
                status,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_from_shelf(chat_id: int, book_id: int) -> bool:
    """Убирает книгу с полки пользователя по ID записи.
    Возвращает True, если книга была найдена и удалена."""
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM shelves WHERE id = ? AND chat_id = ?",
            (book_id, str(chat_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def toggle_book_status(chat_id: int, book_id: int) -> str | None:
    """Меняет статус книги: saved <-> read. Возвращает новый статус или None."""
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT status FROM shelves WHERE id = ? AND chat_id = ?",
            (book_id, str(chat_id)),
        )
        row = cur.fetchone()
        if not row:
            return None
        new_status = "read" if row["status"] == "saved" else "saved"
        conn.execute(
            "UPDATE shelves SET status = ? WHERE id = ?",
            (new_status, book_id),
        )
        conn.commit()
        return new_status
    finally:
        conn.close()


def clear_shelf(chat_id: int, status: str | None = None) -> bool:
    """Очищает полку пользователя (все статусы или только указанный).
    Возвращает True, если что-то было удалено."""
    conn = get_db()
    try:
        if status:
            cur = conn.execute(
                "DELETE FROM shelves WHERE chat_id = ? AND status = ?",
                (str(chat_id), status),
            )
        else:
            cur = conn.execute(
                "DELETE FROM shelves WHERE chat_id = ?",
                (str(chat_id),),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user_books(chat_id: int, status: str) -> list[dict]:
    """Возвращает список книг пользователя с указанным статусом."""
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT id, title, author, link, status FROM shelves WHERE chat_id = ? AND status = ? ORDER BY created_at DESC",
            (str(chat_id), status),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def show_shelf(chat_id: int, status: str = "saved") -> None:
    """Отправляет пользователю список книг с указанным статусом и кнопками управления."""
    books = get_user_books(chat_id, status)
    if not books:
        status_label = "📚 Моя полка" if status == "saved" else "✅ Прочитанное"
        bot.send_message(
            chat_id,
            f"{status_label} пока пусто. Откладывай понравившиеся книги кнопкой "
            "«📥 На полку» под каждой рекомендацией.",
            reply_markup=get_main_keyboard(),
        )
        return

    lines = []
    for i, b in enumerate(books, 1):
        title = b.get("title", "—")
        author = b.get("author", "—")
        link = b.get("link")
        if link:
            lines.append(f"{i}. <a href=\"{link}\">{title}</a> — {author}")
        else:
            lines.append(f"{i}. {title} — {author}")

    kb = types.InlineKeyboardMarkup(row_width=2)
    for b in books:
        book_id = b["id"]
        current_status = b["status"]
        # Кнопка смены статуса
        toggle_label = "✅ В прочитанное" if current_status == "saved" else "📥 На полку"
        toggle_callback = f"shelf_toggle_{book_id}"
        # Кнопка удаления
        delete_callback = f"shelf_delete_{book_id}"
        kb.row(
            types.InlineKeyboardButton(toggle_label, callback_data=toggle_callback),
            types.InlineKeyboardButton("🗑 Удалить", callback_data=delete_callback),
        )

    status_label = "📚 <b>Моя полка:</b>" if status == "saved" else "✅ <b>Прочитанное:</b>"

    # Кнопка очистки всего списка
    clear_callback = f"shelf_clear_{status}"
    clear_label = "🧹 Очистить полку" if status == "saved" else "🧹 Очистить прочитанное"
    kb.row(types.InlineKeyboardButton(clear_label, callback_data=clear_callback))

    bot.send_message(
        chat_id,
        f"{status_label}\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
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
    btn_read = types.KeyboardButton("✅ Прочитанное")
    keyboard.add(btn_mood, btn_shelf, btn_read)
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


def _book_hash(book: dict) -> str:
    """Короткий хэш книги для callback_data (не превышает лимит Telegram)."""
    title = book.get("Название", "")
    author = book.get("Автор", "")
    return hashlib.md5(f"{title}{author}".encode()).hexdigest()[:10]


def get_shelf_action_kb(book: dict) -> types.InlineKeyboardMarkup:
    """Клавиатура с кнопками «На полку», «Уже читал» и «Следующая книга».

    Первые две кнопки идут в один ряд, а «Следующая книга» — широкой
    кнопкой во втором ряду, чтобы интерфейс оставался аккуратным.
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    book_hash = _book_hash(book)
    kb.row(
        types.InlineKeyboardButton("📥 На полку", callback_data=f"shelf_add_saved_{book_hash}"),
        types.InlineKeyboardButton("✅ Уже читал", callback_data=f"shelf_add_read_{book_hash}"),
    )
    kb.row(
        types.InlineKeyboardButton("🔄 Следующая книга", callback_data="next_book"),
    )
    return kb


# --- Обработчики бота ---

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda message: True)
def handle_main_menu(message):
    text = message.text.strip()

    # Просмотр полки отложенных книг
    if text == "📚 Моя полка":
        show_shelf(message.chat.id, status="saved")
        return

    # Просмотр прочитанных книг
    if text == "✅ Прочитанное":
        show_shelf(message.chat.id, status="read")
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

    # Ищем первый элемент с обложкой среди первых 5 результатов.
    cover = None
    url = None
    rating = None
    for item in items[:5]:
        inst = item.get("instance") or {}
        if inst.get("cover_url"):
            cover = inst["cover_url"]
            url = inst.get("url")
            rating = (inst.get("rating") or {}).get("rated_avg")
            break
    if not cover:
        return None
    if cover.startswith("/"):
        cover = LITRES_CDN + cover
    if url and url.startswith("/"):
        url = LITRES_BASE + url

    return {"cover_url": cover, "book_url": url, "rating": rating}


def _send_book_text(chat_id: int, book: dict, intro_text: str, shelf_kb) -> "types.Message":
    """Fallback-режим: отправляет книгу обычным текстовым сообщением
    (старое поведение бота, до интеграции с ЛитРес)."""
    link = book.get("Ссылка")
    link_line = f"\n\n🔗 <a href=\"{link}\">Читать на ЛитРес</a>" if link else ""
    rating = book.get("rating")
    if rating is not None and rating > 0:
        rating_line = f"Рейтинг: ⭐ {rating}"
    else:
        rating_line = "Рейтинг: пока нет оценок"
    annotation = (book.get("Описание") or "").strip()
    ann_block = f"\n\n{annotation}" if annotation else ""
    response = (
        f"📖 <b>{book['Название']}</b>\n"
        f"Автор: {book['Автор']}\n"
        f"{rating_line}"
        f"{ann_block}"
        f"\n\n🥂 <b>Послевкусие</b>\n\n{intro_text}"
        f"{link_line}"
    )
    return bot.send_message(chat_id, response, parse_mode="HTML", reply_markup=shelf_kb)


def send_recommendation(chat_id: int, category: str) -> None:
    """Полный цикл поиска и отправки карточки книги по категории.

    Сначала пытаемся найти книгу по категории напрямую через ЛитРес
    (с учётом фильтров рейтинга и популярности). Если API недоступно
    или ничего не найдено — берём книгу из нашей подборки books.json
    (fallback). Используется и при первичном выборе настроения, и при
    нажатии кнопки «Следующая книга».
    """
    # 1) Пробуем ЛитРес по категории (обложка + аннотация + рейтинг + ссылка).
    litres_book = get_litres_random_book(category)
    if litres_book:
        shelf_kb = get_shelf_action_kb(litres_book)
        sent = _send_litres_card(chat_id, litres_book, shelf_kb)
        pending_book[sent.message_id] = {"book": litres_book, "hash": _book_hash(litres_book)}
        return

    # 2) Fallback: случайная книга из нашей подборки books.json.
    book = get_book_from_json(category)
    if not book:
        bot.send_message(
            chat_id,
            "😔 В этой подборке пока пусто. Загляни в другой погреб — "
            "выбери настроение на кнопках ниже 👇",
            reply_markup=get_mood_inline_keyboard(),
        )
        return

    shelf_kb = get_shelf_action_kb(book)

    # Пробуем обогатить книгу из books.json обложкой и ссылкой с ЛитРес,
    # чтобы показать её в том же «карточном» формате, что и основную выдачу.
    enrich = search_litres_by_title(book["Название"], book["Автор"])
    if enrich and enrich.get("cover_url"):
        book["cover_url"] = enrich["cover_url"]
        book["book_url"] = enrich.get("book_url") or ""
        book["rating"] = enrich.get("rating")
        book["Ссылка"] = book["book_url"]
        sent = _send_litres_card(chat_id, book, shelf_kb)
    else:
        # ЛитРес недоступен для обогащения — отправляем текстом (наша
        # кураторская аннотация и «послевкусие» сохраняются).
        intro_text = book.get("Послевкусие") or random.choice(SOMMELIER_INTROS).replace("🍷 ", "", 1)
        sent = _send_book_text(chat_id, book, intro_text, shelf_kb)

    pending_book[sent.message_id] = {"book": book, "hash": _book_hash(book)}


@bot.callback_query_handler(func=lambda call: call.data.startswith("mood_"))
def on_mood_selected(call):
    """Обработка выбора настроения из inline-клавиатуры.

    Запоминаем выбранную категорию пользователя (state management) и
    запускаем полный цикл поиска/отправки рекомендации.
    """
    category = call.data[len("mood_"):]
    # Сохраняем текущее настроение, чтобы кнопка «Следующая книга»
    # могла заново запустить поиск в той же категории.
    user_current_category[call.message.chat.id] = category
    send_recommendation(call.message.chat.id, category)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "next_book")
def on_next_book(call):
    """Обработка кнопки «Следующая книга» под рекомендацией.

    Извлекает сохранённую категорию пользователя и заново запускает
    весь цикл поиска (ЛитРес с фильтрами → fallback из books.json),
    отправляя новую карточку книги отдельным сообщением (самый
    стабильный UX для telebot: не зависит от типа медиа предыдущей
    карточки — фото или текст).
    """
    chat_id = call.message.chat.id
    category = user_current_category.get(chat_id)
    if not category:
        # Пользователь ещё не выбирал настроение в этой сессии.
        bot.answer_callback_query(call.id, "Сначала выбери настроение 🎭")
        bot.send_message(
            chat_id,
            "Чтобы получать рекомендации, сначала выбери настроение 👇",
            reply_markup=get_mood_inline_keyboard(),
        )
        return

    send_recommendation(chat_id, category)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("shelf_add_"))
def on_shelf_add(call):
    """Обработка кнопок «На полку» и «Уже читал» под рекомендацией."""
    parts = call.data.split("_", 3)
    if len(parts) != 4:
        bot.answer_callback_query(call.id, "Некорректные данные кнопки.")
        return
    status = parts[2]  # saved или read
    expected_hash = parts[3]

    pending = pending_book.get(call.message.message_id)
    if not pending:
        bot.answer_callback_query(call.id, "Книга уже недоступна для добавления.")
        return
    if pending.get("hash") != expected_hash:
        bot.answer_callback_query(call.id, "Книга уже недоступна для добавления.")
        return

    book = pending["book"]
    added = add_to_shelf(call.message.chat.id, book, status=status)
    if added:
        status_label = "📥 Добавлено на полку!" if status == "saved" else "✅ Отмечено как прочитанное!"
        bot.answer_callback_query(call.id, status_label)
    else:
        bot.answer_callback_query(call.id, "Эта книга уже есть у тебя.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("shelf_clear_"))
def on_shelf_clear(call):
    """Обработка inline-кнопки «Очистить полку» / «Очистить прочитанное»."""
    status = call.data[len("shelf_clear_"):]
    if status not in ("saved", "read"):
        bot.answer_callback_query(call.id, "Некорректный статус.")
        return

    status_label = "полка" if status == "saved" else "список прочитанного"
    confirm_kb = types.InlineKeyboardMarkup()
    confirm_kb.row(
        types.InlineKeyboardButton("✅ Да, очистить", callback_data=f"shelf_clear_confirm_{status}"),
        types.InlineKeyboardButton("❌ Нет, оставить", callback_data=f"shelf_clear_cancel_{status}"),
    )
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=confirm_kb,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("shelf_clear_confirm_"))
def on_shelf_clear_confirm(call):
    """Подтверждение очистки полки или прочитанного."""
    status = call.data[len("shelf_clear_confirm_"):]
    if status not in ("saved", "read"):
        bot.answer_callback_query(call.id, "Некорректный статус.")
        return

    cleared = clear_shelf(call.message.chat.id, status=status)
    if cleared:
        status_text = "🧹 Полка очищена." if status == "saved" else "🧹 Список прочитанного очищен."
        bot.answer_callback_query(call.id, status_text)
        bot.edit_message_text(
            f"{status_text} Откладывай новые книги кнопкой «📥 На полку» под рекомендациями.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    else:
        bot.answer_callback_query(call.id, "Уже пусто.")
    bot.send_message(
        call.message.chat.id,
        "Чем займёмся дальше? 👇",
        reply_markup=get_main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("shelf_clear_cancel_"))
def on_shelf_clear_cancel(call):
    """Отмена очистки полки или прочитанного."""
    bot.answer_callback_query(call.id, "Оставили как есть.")
    bot.edit_message_text(
        "Хорошо, оставили как есть. 📚",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("shelf_toggle_"))
def on_shelf_toggle(call):
    """Смена статуса книги: saved <-> read."""
    try:
        book_id = int(call.data.split("_")[-1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Некорректные данные кнопки.")
        return

    new_status = toggle_book_status(call.message.chat.id, book_id)
    if not new_status:
        bot.answer_callback_query(call.id, "Книга не найдена.")
        return

    # Обновляем сообщение с новыми кнопками
    status = "saved" if new_status == "read" else "read"
    show_shelf(call.message.chat.id, status=status)
    bot.answer_callback_query(call.id, "Статус обновлён.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("shelf_delete_"))
def on_shelf_delete(call):
    """Удаление книги из полки."""
    try:
        book_id = int(call.data.split("_")[-1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Некорректные данные кнопки.")
        return

    # Определяем статус книги перед удалением, чтобы обновить правильный список
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT status FROM shelves WHERE id = ? AND chat_id = ?",
            (book_id, str(call.message.chat.id)),
        )
        row = cur.fetchone()
        status = row["status"] if row else "saved"
    finally:
        conn.close()

    removed = remove_from_shelf(call.message.chat.id, book_id)
    if removed:
        bot.answer_callback_query(call.id, "🗑 Книга удалена.")
        show_shelf(call.message.chat.id, status=status)
    else:
        bot.answer_callback_query(call.id, "Книга не найдена.")


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
    try:
        update = telebot.types.Update.de_json(json_string)
    except Exception:
        traceback.print_exc()
        logger.error("Не удалось распарсить обновление от Telegram", exc_info=True)
        return 'OK', 200

    try:
        bot.process_new_updates([update])
    except Exception:
        traceback.print_exc()
        logger.error("Ошибка при обработке обновления в webhook", exc_info=True)
    return 'OK', 200


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
    init_db()
    print(welcome_text)
    print("Бот «Книжный сомелье» запущен в режиме Webhooks...")
    app.run(host='0.0.0.0', port=PORT)
