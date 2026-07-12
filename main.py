from __future__ import annotations

import os
import json
import random
import logging
import traceback
import time
import hashlib
import requests
from functools import lru_cache
import telebot
from telebot import types
from dotenv import load_dotenv
from flask import Flask, request
import threading
import psycopg2
import psycopg2.extras

load_dotenv()

TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана.")

bot = telebot.TeleBot(TOKEN, threaded=False)

logger = telebot.logger
telebot.logger.setLevel(logging.DEBUG)


# Жёсткий лимит повторов при 429. Без него бот может уйти в бесконечный
# цикл ожидания, если Telegram долго держит лимит (и тогда поток никогда
# не доходит до answer_callback_query, «часики» на кнопке зависают,
# пользователь кликает повторно — получаем спам и ещё больше 429).
MAX_TG_RETRIES = 3

def _tg_call(func, *args, **kwargs):
    """Выполняет вызов Telegram API с защитой от 429 Too Many Requests.

    При получении 429 извлекает из ответа рекомендованное Telegram время
    ожидания (retry_after) и ждёт ровно столько, затем повторяет попытку.
    Число повторов жёстко ограничено MAX_TG_RETRIES, чтобы исключить
    бесконечный цикл при длительном лимите. После исчерпания лимита
    последняя ошибка 429 пробрасывается вызывающему коду — это позволяет
    обработчикам корректно завершиться (в т.ч. вызвать answer_callback_query)
    вместо бесконечного зависания.
    """
    last_exc = None
    for attempt in range(MAX_TG_RETRIES):
        try:
            return func(*args, **kwargs)
        except telebot.apihelper.ApiTelegramException as e:
            if getattr(e, 'error_code', None) == 429:
                # Извлекаем рекомендованное Telegram время ожидания.
                # По умолчанию 2 секунды, если поле отсутствует/некорректно.
                retry_after = 2
                try:
                    result_json = getattr(e, 'result_json', None) or {}
                    retry_after = int(
                        result_json.get('parameters', {}).get('retry_after', 2)
                    )
                except (TypeError, ValueError):
                    retry_after = 2
                last_exc = e
                logger.warning(
                    "Получен 429 от Telegram, ожидаем %s сек (попытка %s/%s)...",
                    retry_after, attempt + 1, MAX_TG_RETRIES,
                )
                time.sleep(retry_after)
                continue
            raise
    # Исчерпали лимит повторов — пробрасываем последнюю ошибку 429 наверх.
    if last_exc is not None:
        raise last_exc


def safe_answer_callback_query(call, text: str = "") -> None:
    """Всегда отвечает на callback-запрос (снимает «часики» на кнопке).

    Никогда не бросает исключение: даже если сам answer_callback_query
    упирается в 429 и _tg_call исчерпал лимит повторов, мы просто
    фиксируем это в логе и завершаемся. Это критично, чтобы кнопка
    пользователя не «зависала» (иначе клиент повторно шлёт клики → спам
    → ещё больше 429). Используется во всех callback-обработчиках.
    """
    try:
        bot.answer_callback_query(call.id, text=text)
    except telebot.apihelper.ApiTelegramException as e:
        logger.error("Не удалось ответить на callback_query %s: %s", call.id, e)
    except Exception as e:
        logger.error("Не удалось ответить на callback_query %s: %s", call.id, e, exc_info=True)


# --- Интеграция с открытым поисковым API ЛитРес ---
# Публичный эндпоинт поиска. Для книг обязателен параметр types
# (допустимые значения: text_book, audiobook, paper_book, ...).
LITRES_SEARCH_API = "https://api.litres.ru/foundation/api/search"
LITRES_BASE = "https://www.litres.ru"      # канонические страницы книг
LITRES_CDN = "https://cdn.litres.ru"        # хост обложек (без редиректа ddos-guard)

# Лимит длины подписи (caption) в Telegram — 1024 символа.
CAPTION_LIMIT = 1024

# Статусы книг на полке пользователя (PostgreSQL)
STATUS_SAVED = "saved"
STATUS_READ = "read"

# --- База данных PostgreSQL для полки пользователей ---
# Хранение вынесено на внешний PostgreSQL (Render Postgres), чтобы данные
# переживали рестарты сервера: эфемерная ФС Render удаляет локальный файл
# sqlite при каждом деплое/перезапуске, из-за чего сбрасывались «Моя полка»
# и «Прочитанное».
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logger.warning("CRITICAL: DATABASE_URL is not set!")

class ShelfDBError(Exception):
    """Ошибка доступа к БД полки пользователя (PostgreSQL).

    Оборачивает psycopg2.Error, чтобы вызывающий код (обработчики бота)
    мог одним блоком перехватить сбой БД и уведомить пользователя,
    не роняя бота и не провоцируя петлю 429 от Telegram. Также
    поднимается, когда DATABASE_URL не задан (безопасный фоллбэк).
    """
    pass


class AllBooksReadError(Exception):
    """Все книги в выбранной категории уже прочитаны/сохранены пользователем.

    Используется для досрочного прерывания поиска (List Exhaustion),
    чтобы не делать лишние запросы к API и не спамить пользователю.
    """
    def __init__(self, chat_id: int):
        self.chat_id = chat_id


def get_db_connection():
    """Возвращает новое подключение к PostgreSQL по DATABASE_URL.

    Каждый вызов открывает независимое соединение — это держит работу
    потокобезопасной: каждый поток бота работает со своим коннектом, и
    параллельные запросы не мешают друг другу (общий коннект в psycopg2
    не является thread-safe). Если DATABASE_URL не задан — поднимает
    ShelfDBError, чтобы вызывающий код корректно обработал отсутствие БД.
    """
    if not DATABASE_URL:
        raise ShelfDBError()
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    """Создаёт таблицу полки пользователей, если она не существует.

    Вызывается один раз при старте приложения (до обработки запросов).
    Подключение открывается локально и закрывается контекстным
    менеджером. В PostgreSQL блокировки строк решаются на уровне СУБД,
    поэтому отдельный WAL/timeout не нужны — конкурентные потоки
    безопасно работают через независимые соединения. Если DATABASE_URL
    не задан — просто выходим, не роняя бота при старте.
    """
    if not DATABASE_URL:
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS shelves (
                        id BIGSERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        title TEXT NOT NULL,
                        author TEXT,
                        link TEXT,
                        status TEXT NOT NULL DEFAULT STATUS_SAVED,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_shelves_chat_status
                    ON shelves (chat_id, status)
                """)
            conn.commit()
    except psycopg2.Error as e:
        logger.error("Ошибка при инициализации БД: %s", e)
        traceback.print_exc()


# --- Маппинг эмоциональных категорий на поисковые запросы ЛитРес ---
# Ключ — наша категория из меню, значение — ключевые слова для поиска
# по каталогу ЛитРес (жанры/тематики на русском).
MOOD_TO_LITRES = {
    "Уютный вечер": "современная проза уютное бестселлеры",
    "Хочу острых ощущений": "остросюжетный триллер боевик бестселлеры",
    "Немного поплакать": "лирическая проза драма бестселлеры",
    # Классика и мировая литература приоритизированы: добавлены ключевые
    # слова, чтобы зарубежная/русская классика и мировые бестселлеры
    # попадали в топ результатов (сортировка sort=popular/relevance).
    "Пища для ума": "научно-популярная литература хиты зарубежная классика русская классика мировые бестселлеры",
    "Лёгкость и смех": "юмористическая проза бестселлеры",
    "Уйти от реальности": "магический реализм эпическая фантастика мастер и маргарита дюна властелин колец",
    "Закрытый клуб": "интеллектуальный детектив классика популярное зарубежная классика русская классика мировые бестселлеры",
    "Проглотить за одну ночь": "остросюжетный детектив триллер бестселлеры",
    "Моральный детокс": "современная проза вдохновляющие бестселлеры",
    # Новые категории меню: детективы/триллеры, любовные романы, фэнтези.
    "Холодный расчёт": "остросюжетный детектив триллер звездная коллекция издательских детективов и мистики",
    "Пьянящая романтика": "современный любовный роман зарубежная сентиментальная проза бестселлеры хиты",
    "Шагнуть в портал": "эпическое фэнтези магия миры хиты продаж",
}

# Сколько топовых результатов рассматриваем при случайном выборе.
LITRES_TOP_N = 20


@lru_cache(maxsize=32)
def get_litres_books_list(category: str) -> tuple:
    """Возвращает КЭШИРУЕМЫЙ список (топ-LITRES_TOP_N) кандидатов по категории.

    ВАЖНО (оптимизация производительности): кэшируется именно СПИСОК книг,
    а не финальная выдача. Случайный выбор (random.choice) происходит ВНЕ этой
    функции (см. get_litres_random_book), поэтому при нажатии «Следующая книга»
    пользователь мгновенно получает новую книгу из уже сохранённого в памяти
    списка — без повторного HTTP-запроса к ЛитРес.

    Жёсткий таймаут 2.5с: при requests.exceptions.Timeout (или любой другой
    ошибке сети/JSON) возвращаем пустой кортеж, и вызывающий код мгновенно
    уходит в fallback на локальный books.json (защита от 429/тормозов).
    """
    query = MOOD_TO_LITRES.get(category)
    if not query:
        return tuple()

    params = {
        "q": query,
        "types": "text_book",
        "limit": 40,            # берём расширенную выдачу, чтобы отобрать топ
        "sort": "popular",      # сортировка по популярности (бестселлеры выше)
    }

    try:
        # ЖЁСТКИЙ ТАЙМАУТ 2.5с: если ЛитРес не ответил — падаем в Timeout,
        # чтобы бот мгновенно выдал книгу из локального books.json.
        resp = requests.get(LITRES_SEARCH_API, params=params, timeout=2.5)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        # ЛитРес не уложился в 2.5с — мгновенный fallback на books.json.
        logger.warning("ЛитРес не ответил за 2.5с (timeout) для категории %r", category)
        return tuple()
    except (requests.RequestException, ValueError):
        # Сеть недоступна / API вернул не-JSON / некорректный статус.
        logger.warning("Ошибка запроса к ЛитРес для категории %r", category)
        return tuple()

    payload = data.get("payload") or {}
    items = payload.get("data") or []
    if not items:
        return tuple()

    # Жёсткий фильтр качества: оставляем только книги с рейтингом >= 3.5.
    # Рейтинг 0, None или ниже 3.5 (включая неоценённые самоиздаты и слабые
    # книги) полностью исключаются из выдачи — качество превыше всего.
    # Фильтруем ДО отбора топа по популярности, чтобы пул кандидатов
    # состоял исключительно из достойных книг.
    def _has_valid_rating(item: dict) -> bool:
        inst = item.get("instance") or {}
        rating_obj = inst.get("rating") or {}
        rating = rating_obj.get("rated_avg")
        if rating is None:
            return False
        try:
            return float(rating) >= 3.5
        except (TypeError, ValueError):
            return False

    items = [item for item in items if _has_valid_rating(item)]
    if not items:
        # Ни одной книги с рейтингом не найдено — возвращаем пусто,
        # чтобы сработал стандартный fallback (выдача из books.json).
        return tuple()

    # Берём только топ-LITRES_TOP_N самых популярных книг из ответа API
    # (выдача уже отсортирована по популярности через sort=popular).
    # Сначала оставляем только те, у которых есть обложка, чтобы не возвращать
    # пустые карточки из-за отсутствия картинки у случайно выбранного элемента.
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
    # Возвращаем КОРТЕЖ (иммутабельно — безопасно для lru_cache),
    # чтобы random.choice происходил уже вне кэшированной функции.
    return tuple(candidates)


def get_litres_random_book(category: str, exclude_hash: str | None = None, chat_id: int | None = None) -> dict | None:
    """Возвращает ОДНУ случайную книгу из кэшированного списка ЛитРес.

    Список кандидатов берётся из get_litres_books_list (кэшируется через
    @lru_cache), а random.choice происходит ЗДЕСЬ, ВНЕ кэшированной функции.
    Поэтому повторные нажатия «Следующая книга» не бьют по API, а мгновенно
    выбирают другую книгу из уже загруженного в память списка.

    exclude_hash — короткий хэш текущей (уже показанной) книги. Если передан,
    эта книга исключается из пула выбора, чтобы «Следующая книга» никогда не
    выдавала ту же книгу дважды подряд. Если после исключения список пуст
    (в категории всего одна книга), откатываемся на полный список.

    chat_id — идентификатор пользователя. Если передан, из выдачи исключаются
    книги, которые пользователь уже сохранил или отметил как прочитанные
    (анти-повтор).

    При пустом кэше (таймаут/ошибка/нет книг) возвращает None — вызывающий
    код уходит в fallback на локальный books.json.

    При пустом списке после фильтрации прочитанных книг поднимает
    AllBooksReadError — вызывающий код должен прервать поиск и сообщить
    пользователю, что в категории больше нет непрочитанных книг.
    """
    books = get_litres_books_list(category)
    original_books = books
    if not books:
        return None
    if exclude_hash:
        filtered = [b for b in books if _book_hash(b) != exclude_hash]
        # Если после исключения пул опустел — не ломаем выдачу,
        # возвращаемся к полному списку (дубль лучше, чем пустота).
        if filtered:
            books = filtered

    # Анти-повтор: исключаем книги, которые пользователь уже сохранил/прочитал.
    if chat_id is not None:
        user_titles = get_user_book_titles(chat_id, ["saved", "read"])
        if user_titles:
            books = [b for b in books if b.get("Название") not in user_titles]

    if not books:
        # Защита от List Exhaustion: все книги категории уже прочитаны/сохранены.
        # Поднимаем исключение, чтобы вызывающий код прервал поиск
        # и не попытался сделать лишний запрос к fallback-источнику.
        if original_books:
            raise AllBooksReadError(chat_id)
        return None
    return random.choice(books)


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
    try:
        rating_val = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating_val = None
    if rating_val is not None and rating_val > 0:
        rating_line = f"Рейтинг: ⭐ {rating_val}"
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
        return _tg_call(bot.send_photo,
            chat_id,
            photo=book["cover_url"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=shelf_kb,
        )
    except telebot.apihelper.ApiTelegramException:
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

# Последняя показанная книга каждого пользователя (для кнопки «Следующая
# книга»). Храним саму книгу в памяти процесса, чтобы НЕ класть ни название,
# ни длинные строки в callback_data (жёсткий лимит Telegram — 64 байта).
# В callback_data кнопки кладём только короткий экшен "next_book", а
# идентификатор текущей книги достаём отсюда при обработке нажатия.
user_last_book: dict[int, dict] = {}


# --- Блокировка пользователя (User Lock) против Race Condition ---
# Webhook-режим (см. Flask-приложение в конце файла) порождает отдельный
# threading.Thread на каждое входящее обновление, поэтому один и тот же
# пользователь может «долбить» inline-кнопку («Следующая книга» и т.п.)
# десятки раз подряд, пока бот «думает» (ход в БД / ЛитРес). Без блокировки
# эти клики честно запускают десятки параллельных потоков, бьющих в Telegram
# API одновременно, — что и порождает шквал 429 Too Many Requests.
# Ниже — атомарный реестр per-user блокировок: пока первый клик
# обрабатывается, остальные клики этого же юзера игнорируются.
_user_locks: dict[int, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def _acquire_user_lock(user_id: int) -> bool:
    """Атомарно пытается захватить блокировку пользователя.

    Возвращает True, если блокировка захвачена (можно обрабатывать клик),
    и False, если пользователь УЖЕ обрабатывается (спам-клик — игнорируем).
    Использует threading.Lock.acquire(blocking=False), что исключает
    TOCTOU-гонку, присутствующую у наивного dict.get()/dict[uid]=True.
    """
    with _user_locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
    return lock.acquire(blocking=False)


def _release_user_lock(user_id: int) -> None:
    """Снимает блокировку пользователя (всегда в блоке finally обработчика)."""
    with _user_locks_guard:
        lock = _user_locks.get(user_id)
    if lock is not None:
        try:
            lock.release()
        except RuntimeError:
            # Блокировка уже свободна — не критично, просто логируем.
            logger.debug("Попытка снять незанятую блокировку user_id=%s", user_id)


def add_to_shelf(chat_id: int, book: dict, status: str = STATUS_SAVED) -> bool:
    """Добавляет книгу на полку пользователя с указанным статусом.
    Возвращает False, если такая книга уже есть у пользователя (дедуп по названию)."""
    try:
        # Локальное подключение внутри функции: каждый поток открывает и
        # закрывает своё соединение (psycopg2-коннект не thread-safe,
        # поэтому общий пул здесь не используем). commit() выполняется
        # явно после успешной записи, коннект закрывается контекстным
        # менеджером. При отсутствии DATABASE_URL get_db_connection
        # поднимает ShelfDBError — бот не падает, а сообщает об ошибке.
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM shelves WHERE chat_id = %s AND title = %s",
                    (chat_id, book.get("Название", "")),
                )
                if cur.fetchone():
                    return False
                cur.execute(
                    "INSERT INTO shelves (chat_id, title, author, link, status) VALUES (%s, %s, %s, %s, %s)",
                    (
                        chat_id,
                        book.get("Название", ""),
                        book.get("Автор", ""),
                        book.get("Ссылка", ""),
                        status,
                    ),
                )
            conn.commit()
            return True
    except (psycopg2.Error, ShelfDBError) as e:
        logger.error("Ошибка БД (add_to_shelf): %s", e)
        traceback.print_exc()
        raise ShelfDBError() from e


def remove_from_shelf(chat_id: int, book_id: int) -> bool:
    """Убирает книгу с полки пользователя по ID записи.
    Возвращает True, если книга была найдена и удалена."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM shelves WHERE id = %s AND chat_id = %s",
                    (book_id, chat_id),
                )
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    except (psycopg2.Error, ShelfDBError) as e:
        logger.error("Ошибка БД (remove_from_shelf): %s", e)
        traceback.print_exc()
        raise ShelfDBError() from e


def toggle_book_status(chat_id: int, book_id: int) -> str | None:
    """Меняет статус книги: saved <-> read. Возвращает новый статус или None."""
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT status FROM shelves WHERE id = %s AND chat_id = %s",
                    (book_id, chat_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                new_status = STATUS_READ if row["status"] == STATUS_SAVED else STATUS_SAVED
                cur.execute(
                    "UPDATE shelves SET status = %s WHERE id = %s",
                    (new_status, book_id),
                )
            conn.commit()
            return new_status
    except (psycopg2.Error, ShelfDBError) as e:
        logger.error("Ошибка БД (toggle_book_status): %s", e)
        traceback.print_exc()
        raise ShelfDBError() from e


def clear_shelf(chat_id: int, status: str | None = None) -> bool:
    """Очищает полку пользователя (все статусы или только указанный).
    Возвращает True, если что-то было удалено."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if status:
                    cur.execute(
                        "DELETE FROM shelves WHERE chat_id = %s AND status = %s",
                        (chat_id, status),
                    )
                else:
                    cur.execute(
                        "DELETE FROM shelves WHERE chat_id = %s",
                        (chat_id,),
                    )
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    except (psycopg2.Error, ShelfDBError) as e:
        logger.error("Ошибка БД (clear_shelf): %s", e)
        traceback.print_exc()
        raise ShelfDBError() from e


def get_user_books(chat_id: int, status: str) -> list[dict]:
    """Возвращает список книг пользователя с указанным статусом."""
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, title, author, link, status FROM shelves WHERE chat_id = %s AND status = %s ORDER BY id DESC",
                    (chat_id, status),
                )
                return [dict(row) for row in cur.fetchall()]
    except (psycopg2.Error, ShelfDBError) as e:
        logger.error("Ошибка БД (get_user_books): %s", e)
        traceback.print_exc()
        raise ShelfDBError() from e


def get_user_book_titles(chat_id: int, statuses: list[str]) -> set[str]:
    """Возвращает множество названий книг пользователя с указанными статусами."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(statuses))
                cur.execute(
                    f"SELECT title FROM shelves WHERE chat_id = %s AND status IN ({placeholders})",
                    (chat_id, *statuses),
                )
                return {row[0] for row in cur.fetchall()}
    except (psycopg2.Error, ShelfDBError) as e:
        logger.error("Ошибка БД (get_user_book_titles): %s", e)
        traceback.print_exc()
        return set()


def show_shelf(chat_id: int, status: str = STATUS_SAVED, edit_message_id: int | None = None) -> None:
    """Отправляет или редактирует список книг пользователя с указанным статусом."""
    try:
        books = get_user_books(chat_id, status)
    except ShelfDBError:
        # Сбой БД не должен ронять бота и провоцировать петлю 429:
        # просто сообщаем пользователю и завершаем.
        # Выводим полный стек в консоль/Render-логи, чтобы видеть
        # точную причину (database is locked, no such table и т.п.).
        traceback.print_exc()
        error_text = "Упс, полка временно недоступна 🛠 Попробуй чуть позже — мы уже чиним погреб."
        if edit_message_id:
            try:
                _tg_call(bot.edit_message_text,
                    error_text,
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    reply_markup=None,
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e):
                    logger.warning("Не удалось отредактировать сообщение: %s", e)
        else:
            _tg_call(bot.send_message,
                chat_id,
                error_text,
                reply_markup=get_main_keyboard(),
            )
        return

    if status == "saved":
        status_label = "📚 <b>Ваша полка:</b>"
    else:
        status_label = "✅ <b>Прочитанное:</b>"

    if not books:
        empty_text = "Ваша полка пуста. Время найти что-то новое!" if status == "saved" else "Список прочитанного пуст."
        if edit_message_id:
            try:
                _tg_call(bot.edit_message_text,
                    empty_text,
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e):
                    logger.warning("Не удалось отредактировать сообщение: %s", e)
        else:
            _tg_call(bot.send_message,
                chat_id,
                empty_text,
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

    # Inline-кнопки только для сохраненных книг; прочитанное — чистый список.
    if status == "saved":
        kb = types.InlineKeyboardMarkup()
        for b in books:
            book_id = b["id"]
            truncated_title = _truncate_title(b.get("title", "Книга"))
            kb.row(
                types.InlineKeyboardButton(f"✅ Прочитал: {truncated_title}", callback_data=f"read_{book_id}")
            )
        kb.row(
            types.InlineKeyboardButton("🗑 Очистить полку", callback_data="clear_shelf")
        )
    else:
        kb = None

    text = f"{status_label}\n\n" + "\n".join(lines)

    if edit_message_id:
        try:
            _tg_call(bot.edit_message_text,
                text,
                chat_id=chat_id,
                message_id=edit_message_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                logger.warning("Не удалось отредактировать сообщение: %s", e)
    else:
        _tg_call(bot.send_message,
            chat_id,
            text,
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
        types.InlineKeyboardButton("🧊 Холодный расчёт", callback_data="mood_Холодный расчёт"),
        types.InlineKeyboardButton("🌹 Пьянящая романтика", callback_data="mood_Пьянящая романтика"),
        types.InlineKeyboardButton("🌀 Шагнуть в портал", callback_data="mood_Шагнуть в портал"),
    )
    return keyboard


def _book_hash(book: dict) -> str:
    """Короткий хэш книги для callback_data (не превышает лимит Telegram)."""
    title = book.get("Название", "")
    author = book.get("Автор", "")
    return hashlib.md5(f"{title}{author}".encode()).hexdigest()[:10]


def _truncate_title(title: str, max_length: int = 25) -> str:
    """Обрезает название книги для кнопки, чтобы не превышать лимит длины."""
    if len(title) > max_length:
        return title[:max_length - 1] + "…"
    return title


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
    try:
        _tg_call(bot.send_message, message.chat.id, welcome_text, reply_markup=get_main_keyboard())
    except telebot.apihelper.ApiTelegramException as e:
        logger.error("Не удалось отправить приветствие: %s", e)


@bot.message_handler(func=lambda message: message.text in [
    "📚 Моя полка",
    "✅ Прочитанное",
    "🎭 Выбрать настроение",
])
def handle_main_menu(message):
    text = message.text.strip()
    try:
        # Просмотр полки отложенных книг
        if text == "📚 Моя полка":
            show_shelf(message.chat.id, status=STATUS_SAVED)
            return

        # Просмотр прочитанных книг
        if text == "✅ Прочитанное":
            show_shelf(message.chat.id, status=STATUS_READ)
            return

        # Открытие меню выбора настроения
        if text == "🎭 Выбрать настроение":
            _tg_call(bot.send_message,
                message.chat.id,
                "🎭 <b>Выбери настроение:</b>",
                parse_mode="HTML",
                reply_markup=get_mood_inline_keyboard(),
            )
            return
    except telebot.apihelper.ApiTelegramException as e:
        logger.error("Ошибка в обработчике handle_main_menu: %s", e)


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


def get_book_from_json(category: str, exclude_hash: str | None = None, chat_id: int | None = None) -> dict | None:
    """Случайная книга из books.json по категории (fallback-источник).

    exclude_hash — хэш текущей книги; если передан, книга с таким хэшем
    исключается из выбора (защита от дублей в «Следующая книга»).

    chat_id — идентификатор пользователя. Если передан, из выдачи исключаются
    книги, которые пользователь уже сохранил или отметил как прочитанные
    (анти-повтор).

    Возвращает словарь в нашей схеме (Название, Автор, Описание, Послевкусие)
    или None, если для категории нет книг в подборке.

    При пустом списке после фильтрации прочитанных книг поднимает
    AllBooksReadError — вызывающий код должен прервать поиск.
    """
    books_db = _load_curated()
    books = books_db.get(category, books_db.get("Лёгкость и смех"))
    original_books = books
    if not books:
        return None
    if exclude_hash:
        filtered = [
            b for b in books
            if _book_hash({
                "Название": b.get("title", "—"),
                "Автор": b.get("author", "—"),
            }) != exclude_hash
        ]
        # Если после исключения не осталось кандидатов — не ломаем
        # выдачу, откатываемся к полному списку.
        if filtered:
            books = filtered

    # Анти-повтор: исключаем книги, которые пользователь уже сохранил/прочитал.
    if chat_id is not None:
        user_titles = get_user_book_titles(chat_id, ["saved", "read"])
        if user_titles:
            books = [b for b in books if b.get("title") not in user_titles]

    if not books:
        # Защита от List Exhaustion: все книги категории уже прочитаны/сохранены.
        if original_books:
            raise AllBooksReadError(chat_id)
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
        # Жёсткий таймаут 2.5с: при превышении — мгновенный отказ от
        # обогащения обложкой, книга отправится текстом (fallback).
        resp = requests.get(LITRES_SEARCH_API, params=params, timeout=2.5)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        # ЛитРес не ответил за 2.5с — не обогащаем, возвращаем None.
        logger.warning("ЛитРес не ответил за 2.5с (timeout) при обогащении %r", title)
        return None
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
            try:
                rating = float(rating) if rating is not None else None
            except (TypeError, ValueError):
                rating = None
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
    try:
        rating_val = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating_val = None
    if rating_val is not None and rating_val > 0:
        rating_line = f"Рейтинг: ⭐ {rating_val}"
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
    return _tg_call(bot.send_message, chat_id, response, parse_mode="HTML", reply_markup=shelf_kb)


def send_recommendation(chat_id: int, category: str, exclude_hash: str | None = None) -> None:
    """Полный цикл поиска и отправки карточки книги по категории.

    Сначала пытаемся найти книгу по категории напрямую через ЛитРес
    (с учётом фильтров рейтинга и популярности). Если API недоступно
    или ничего не найдено — берём книгу из нашей подборки books.json
    (fallback). Используется и при первичном выборе настроения, и при
    нажатии кнопки «Следующая книга».

    exclude_hash — хэш текущей книги; передаётся из callback_data кнопки
    «Следующая книга», чтобы не выдать ту же книгу повторно.

    Жёсткий лимит 3 попыток: если за 3 попытки не удалось найти
    непрочитанную книгу (в т.ч. из-за List Exhaustion), прерываем
    поиск и сообщаем пользователю, чтобы не спамить API и не уходить
    в бесконечный цикл.
    """
    # Лимит попыток поиска: максимум 3 попытки найти непрочитанную книгу.
    for _ in range(3):
        # 1) Пробуем ЛитРес по категории (обложка + аннотация + рейтинг + ссылка).
        try:
            litres_book = get_litres_random_book(category, exclude_hash=exclude_hash, chat_id=chat_id)
        except AllBooksReadError:
            # Все книги категории уже прочитаны — прерываем поиск
            # и отправляем сообщение через _tg_call (защита от 429).
            _tg_call(bot.send_message,
                chat_id,
                "Похоже, вы уже прочитали все лучшие книги в этой категории! 🏆 "
                "Попробуйте выбрать другое настроение в меню.",
                reply_markup=get_main_keyboard(),
            )
            return
        if litres_book:
            shelf_kb = get_shelf_action_kb(litres_book)
            sent = _send_litres_card(chat_id, litres_book, shelf_kb)
            pending_book[sent.message_id] = {"book": litres_book, "hash": _book_hash(litres_book)}
            user_last_book[chat_id] = litres_book
            return

        # 2) Fallback: случайная книга из нашей подборки books.json.
        try:
            book = get_book_from_json(category, exclude_hash=exclude_hash, chat_id=chat_id)
        except AllBooksReadError:
            # Все книги категории уже прочитаны — прерываем поиск
            # и отправляем сообщение через _tg_call (защита от 429).
            _tg_call(bot.send_message,
                chat_id,
                "Похоже, вы уже прочитали все лучшие книги в этой категории! 🏆 "
                "Попробуйте выбрать другое настроение в меню.",
                reply_markup=get_main_keyboard(),
            )
            return
        if book:
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
            user_last_book[chat_id] = book
            return

    # Если за 3 попытки новая книга не найдена — сообщаем пользователю.
    _tg_call(bot.send_message,
        chat_id,
        "Похоже, вы уже прочитали все лучшие книги в этой категории! 🏆 "
        "Попробуйте выбрать другое настроение в меню.",
        reply_markup=get_main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("mood_"))
def on_mood_selected(call):
    """Обработка выбора настроения из inline-клавиатуры.

    Запоминаем выбранную категорию пользователя (state management) и
    запускаем полный цикл поиска/отправки рекомендации.
    """
    user_id = call.from_user.id
    if not _acquire_user_lock(user_id):
        # Пользователь уже нажал кнопку и бот ещё «думает» — игнорируем
        # спам-клики, чтобы не порождать параллельные удары по Telegram API.
        safe_answer_callback_query(call, "Загружаю… подождите секунду ⏳")
        return
    try:
        category = call.data[len("mood_"):]
        # Мгновенная обратная связь: показываем, что бот начал работу, и
        # предотвращаем повторные клики, пока идёт (возможно кэшированный) поиск.
        _tg_call(bot.send_chat_action, call.message.chat.id, 'upload_photo')
        # Сохраняем текущее настроение, чтобы кнопка «Следующая книга»
        # могла заново запустить поиск в той же категории.
        user_current_category[call.message.chat.id] = category
        send_recommendation(call.message.chat.id, category)
    except Exception:
        # Любая непредвиденная ошибка (таймаут ЛитРес, исчерпание лимита
        # 429 в _tg_call и т.д.) не должна ронять обработчик — логируем и
        # спокойно завершаем, обязательно ответив на callback, чтобы
        # «часики» на кнопке не зависли (это провоцирует повторные клики).
        traceback.print_exc()
        logger.error("Ошибка в обработчике on_mood_selected", exc_info=True)
    finally:
        # Снимаем блокировку пользователя (даже при ошибке) — иначе
        # последующие клики этого юзера навсегда «зависнут» в ignored-состоянии.
        _release_user_lock(user_id)
        # Гарантированно снимаем «часики» с кнопки в любом исходе.
        try:
            safe_answer_callback_query(call)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data == "next_book")
def on_next_book(call):
    """Обработка кнопки «Следующая книга» под рекомендацией.

    В callback_data передаётся только короткий экшен "next_book" (строго
    в лимите 64 байт Telegram). Идентификатор текущей (показанной) книги
    берётся из словаря состояний user_last_book[chat_id], чтобы исключить
    её из пула выбора и не выдавать ту же книгу дважды подряд. Категория —
    из user_current_category. Весь цикл поиска (ЛитРес → fallback books.json)
    запускается заново, отправляя новую карточку отдельным сообщением.
    """
    user_id = call.from_user.id
    if not _acquire_user_lock(user_id):
        # Пользователь уже нажал кнопку и бот ещё «думает» — игнорируем
        # спам-клики, чтобы не порождать параллельные удары по Telegram API.
        safe_answer_callback_query(call, "Загружаю… подождите секунду ⏳")
        return
    try:
        chat_id = call.message.chat.id
        # Мгновенная обратная связь ДО начала поиска: показываем, что бот
        # работает, и предотвращаем повторные клики «Следующая книга».
        _tg_call(bot.send_chat_action, chat_id, 'upload_photo')
        # Безопасное извлечение категории: при перезагрузке сервера
        # словарь в памяти пуст, .get() вернёт None вместо KeyError.
        category = user_current_category.get(chat_id)
        if not category:
            # Пользователь ещё не выбирал настроение в этой сессии
            # (или бот перезагрузился) — просим выбрать заново.
            safe_answer_callback_query(call, "Сначала выбери настроение 🎭")
            _tg_call(bot.send_message,
                chat_id,
                "Выберите настроение заново в главном меню 👇",
                reply_markup=get_mood_inline_keyboard(),
            )
            return

        # Берём последнюю показанную книгу из словаря состояний (без
        # длинных строк в callback_data) и исключаем её из выдачи, чтобы
        # не выдать ту же книгу дважды подряд.
        last_book = user_last_book.get(chat_id)
        exclude_hash = _book_hash(last_book) if last_book else None

        send_recommendation(chat_id, category, exclude_hash=exclude_hash)
        safe_answer_callback_query(call)
    except Exception:
        # Любая непредвиденная ошибка (таймаут ЛитРес, 429 и т.д.) не
        # должна ронять обработчик — логируем и спокойно завершаем.
        traceback.print_exc()
        logger.error("Ошибка в обработчике on_next_book", exc_info=True)
        try:
            safe_answer_callback_query(call)
        except Exception:
            pass
    finally:
        # Снимаем блокировку пользователя в любом исходе (включая ранний
        # return при отсутствии выбранной категории), иначе последующие
        # клики этого юзера навсегда «зависнут» в ignored-состоянии.
        _release_user_lock(user_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("shelf_add_"))
def on_shelf_add(call):
    """Обработка кнопок «На полку» и «Уже читал» под рекомендацией."""
    user_id = call.from_user.id
    if not _acquire_user_lock(user_id):
        # Пользователь уже нажал кнопку и бот ещё «думает» — игнорируем
        # спам-клики, чтобы не порождать параллельные удары по Telegram API.
        safe_answer_callback_query(call, "Загружаю… подождите секунду ⏳")
        return
    try:
        parts = call.data.split("_", 3)
        if len(parts) != 4:
            safe_answer_callback_query(call, "Некорректные данные кнопки.")
            return
        status = parts[2]  # saved или read
        expected_hash = parts[3]

        pending = pending_book.get(call.message.message_id)
        if not pending:
            safe_answer_callback_query(call, "Книга уже недоступна для добавления.")
            return
        if pending.get("hash") != expected_hash:
            safe_answer_callback_query(call, "Книга уже недоступна для добавления.")
            return

        book = pending["book"]
        try:
            added = add_to_shelf(call.message.chat.id, book, status=status)
        except ShelfDBError:
            # Полный стек ошибки — в консоль/Render-логи, чтобы видеть причину.
            traceback.print_exc()
            safe_answer_callback_query(call, "Упс, полка временно недоступна")
            return
        if added:
            status_label = "📥 Добавлено на полку!" if status == "saved" else "✅ Отмечено как прочитанное!"
            safe_answer_callback_query(call, status_label)
        else:
            safe_answer_callback_query(call, "Эта книга уже есть у тебя.")
    finally:
        # Снимаем блокировку пользователя в любом исходе (включая ранние
        # return при невалидных данных), иначе клики юзера «зависнут».
        _release_user_lock(user_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("read_"))
def on_read_book(call):
    """Обработка кнопки «Отметить прочитанным» в списке полки."""
    user_id = call.from_user.id
    if not _acquire_user_lock(user_id):
        # Пользователь уже нажал кнопку и бот ещё «думает» — игнорируем
        # спам-клики, чтобы не порождать параллельные удары по Telegram API.
        safe_answer_callback_query(call, "Загружаю… подождите секунду ⏳")
        return
    try:
        try:
            book_id = int(call.data.split("_")[1])
        except (ValueError, IndexError):
            safe_answer_callback_query(call, "Некорректные данные кнопки.")
            return

        try:
            # Обновляем статус книги на read
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shelves SET status = %s WHERE id = %s AND chat_id = %s AND status = %s",
                        (STATUS_READ, book_id, call.message.chat.id, STATUS_SAVED),
                    )
                    # Логируем для отладки, сколько строк реально изменилось
                    print(f"PostgreSQL: Updated {cur.rowcount} rows for book_id {book_id}")
                    if cur.rowcount == 0:
                        safe_answer_callback_query(call, "Книга не найдена или уже прочитана.")
                        return
                conn.commit()
        except (psycopg2.Error, ShelfDBError) as e:
            logger.error("Ошибка БД (on_read_book): %s", e)
            traceback.print_exc()
            safe_answer_callback_query(call, "Упс, полка временно недоступна")
            return

        # Получаем обновленный список книг на полке
        try:
            books = get_user_books(call.message.chat.id, status=STATUS_SAVED)
        except ShelfDBError:
            traceback.print_exc()
            safe_answer_callback_query(call, "Упс, не удалось обновить полку")
            return

        # Формируем новое сообщение и клавиатуру
        if not books:
            new_text = "Ваша полка пуста. Время найти что-то новое!"
            new_kb = None
        else:
            lines = []
            for i, b in enumerate(books, 1):
                title = b.get("title", "—")
                author = b.get("author", "—")
                link = b.get("link")
                if link:
                    lines.append(f"{i}. <a href=\"{link}\">{title}</a> — {author}")
                else:
                    lines.append(f"{i}. {title} — {author}")
            new_text = "📚 <b>Ваша полка:</b>\n\n" + "\n".join(lines)

            new_kb = types.InlineKeyboardMarkup()
            for b in books:
                book_id = b["id"]
                truncated_title = _truncate_title(b.get("title", "Книга"))
                new_kb.row(
                    types.InlineKeyboardButton(f"✅ Прочитал: {truncated_title}", callback_data=f"read_{book_id}")
                )
            # Добавляем кнопку очистки
            new_kb.row(
                types.InlineKeyboardButton("🗑 Очистить полку", callback_data="clear_shelf")
            )

        # Редактируем сообщение
        try:
            _tg_call(bot.edit_message_text,
                new_text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML",
                reply_markup=new_kb,
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                logger.warning("Не удалось отредактировать сообщение: %s", e)
        safe_answer_callback_query(call, "✅ Отмечено как прочитанное!")
    finally:
        # Снимаем блокировку пользователя в любом исходе, иначе клики
        # этого юзера «зависнут» в ignored-состоянии.
        _release_user_lock(user_id)


@bot.callback_query_handler(func=lambda call: call.data == "clear_shelf")
def on_clear_shelf(call):
    """Обработка кнопки «Очистить полку»."""
    user_id = call.from_user.id
    if not _acquire_user_lock(user_id):
        # Пользователь уже нажал кнопку и бот ещё «думает» — игнорируем
        # спам-клики, чтобы не порождать параллельные удары по Telegram API.
        safe_answer_callback_query(call, "Загружаю… подождите секунду ⏳")
        return
    try:
        try:
            cleared = clear_shelf(call.message.chat.id, status=STATUS_SAVED)
        except ShelfDBError:
            traceback.print_exc()
            safe_answer_callback_query(call, "Упс, полка временно недоступна")
            return

        if cleared:
            safe_answer_callback_query(call, "🧹 Полка очищена.")
            try:
                _tg_call(bot.edit_message_text,
                    "Ваша полка пуста. Время найти что-то новое!",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e):
                    logger.warning("Не удалось отредактировать сообщение: %s", e)
        else:
            safe_answer_callback_query(call, "Полка уже пуста.")
            try:
                _tg_call(bot.edit_message_text,
                    "Ваша полка пуста. Время найти что-то новое!",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e):
                    logger.warning("Не удалось отредактировать сообщение: %s", e)
    finally:
        # Снимаем блокировку пользователя в любом исходе, иначе клики
        # этого юзера «зависнут» в ignored-состоянии.
        _release_user_lock(user_id)


@bot.message_handler(content_types=['text'])
def handle_greetings(message):
    """Обработчик приветствий и свободного текста (находится в самом конце,
    чтобы не перехватывать команды /start и тексты кнопок главного меню)."""
    text = message.text.strip()
    lower_text = text.lower()
    try:
        if any(word in lower_text for word in ["привет", "здравствуйте", "ку"]):
            _tg_call(bot.send_message,
                message.chat.id,
                "Привет! Я твой книжный сомелье. Выбирай категорию в меню, и я предложу что-то интересное — от легкой прозы до глубокой классики вроде Достоевского!",
                reply_markup=get_main_keyboard(),
            )
            return
        elif any(word in lower_text for word in ["спасибо", "спс", "благодарю"]):
            _tg_call(bot.send_message,
                message.chat.id,
                "Всегда пожалуйста! Приятного чтения!",
                reply_markup=get_main_keyboard(),
            )
            return
        elif any(word in lower_text for word in ["пока", "до свидания", "спокойной ночи"]):
            _tg_call(bot.send_message,
                message.chat.id,
                "До встречи! Жду тебя за новой порцией книг.",
                reply_markup=get_main_keyboard(),
            )
            return
        else:
            # Если пользователь ввёл что-то другое — подсказываем меню
            _tg_call(bot.send_message,
                message.chat.id,
                "Я пока понимаю только нажатия на кнопки меню. Выбери настроение внизу, и я найду для тебя книгу!",
                reply_markup=get_main_keyboard(),
            )
            return
    except telebot.apihelper.ApiTelegramException as e:
        logger.error("Ошибка в обработчике handle_greetings: %s", e)


# --- Flask-приложение для Webhooks ---

APP_URL = os.environ.get('APP_URL', 'https://your-app.onrender.com')
PORT = int(os.environ.get('PORT', 5000))

app = Flask(__name__)


@app.route('/', methods=['POST'])
def webhook():
    try:
        # ВСЁ чтение запросов, проверка JSON и запуск потоков
        # должно быть строго внутри этого try!
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            thread = threading.Thread(target=safe_process_update, args=(update,))
            thread.start()
        return "OK", 200
    except BaseException as e: # Ловим вообще ВСЁ, включая системные выходы
        import traceback
        print("CRITICAL WEBHOOK ERROR:")
        traceback.print_exc()
        return "OK", 200 # Сервер не имеет права отвечать 500

def safe_process_update(update):
    """Фоновая функция для безопасной обработки обновлений"""
    try:
        bot.process_new_updates([update])
    except BaseException:
        logger.error("Ошибка при обработке обновления", exc_info=True)


@app.route('/')
def index():
    """Устанавливаем webhook при старте/деплое."""
    bot.remove_webhook()
    success = bot.set_webhook(url=APP_URL)
    if success:
        return f'Webhook установлен: {APP_URL}', 200
    else:
        return 'Не удалось установить webhook', 500


# Гарантируем создание таблиц при импорте модуля. Под gunicorn
# (Procfile: `gunicorn main:app`) блок `if __name__ == "__main__"` НЕ
# выполняется, поэтому вызов здесь — единственный надёжный способ
# инициализировать БД ДО того, как пользователи начнут жать кнопки полки.
init_db()


if __name__ == "__main__":
    init_db()
    print(welcome_text)
    print("Бот «Книжный сомелье» запущен в режиме Webhooks...")
    app.run(host='0.0.0.0', port=PORT)
