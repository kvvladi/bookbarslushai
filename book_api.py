import os
import re
import json
import time
import random
import requests
from dotenv import load_dotenv

load_dotenv()

# Базовый эндпоинт Google Books API
API_URL = "https://www.googleapis.com/books/v1/volumes"

# Опциональный API-ключ Google Books из .env (GOOGLE_BOOKS_API_KEY).
# С ключом лимиты Google Books API существенно выше, без ключа — жёсткие.
API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY")

# --- Перевод описаний на русский ---
# Используем deep-translator (бесплатный движок Google, ключ не нужен).
# При желании можно переключить на Yandex/Google Cloud — см. translate_to_ru().
try:
    from deep_translator import GoogleTranslator
    _TRANSLATOR_AVAILABLE = True
except ImportError:
    _TRANSLATOR_AVAILABLE = False

# Кэш переводов: исходный текст -> перевод (чтобы не гонять API по нескольку раз)
_translation_cache: dict[str, str] = {}


def _is_russian(text: str) -> bool:
    """Грубая проверка: текст уже преимущественно на кириллице?"""
    cyrillic = len(re.findall(r"[а-яА-ЯёЁ]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    return cyrillic > latin


def translate_to_ru(text: str) -> str:
    """Переводит текст на русский. Если переводчик недоступен или текст уже
    на русском — возвращает текст как есть. При любой ошибке API не падает,
    а отдаёт оригинал."""
    if not text:
        return text
    if text in _translation_cache:
        return _translation_cache[text]
    if not _TRANSLATOR_AVAILABLE:
        return text
    if _is_russian(text):
        _translation_cache[text] = text
        return text
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(text)
        result = translated or text
    except Exception:
        result = text
    _translation_cache[text] = result
    return result


# Кэш книг по жанрам, чтобы не бить Google Books при каждом нажатии кнопки.
_cache: dict[str, list[dict]] = {}
CACHE_TTL = 3600  # время жизни кэша в секундах
_cache_ts: dict[str, float] = {}
TOP_N = 8  # сколько самых популярных книг учитываем при случайном выборе

# Порог «популярности»: выдаём только книги с достаточным числом оценок и
# приемлемым средним рейтингом, чтобы не предлагать малознакомые самоиздаты
# и неизвестные произведения. Пользователь хочет видеть знаменитые книги —
# Дюма, Флобера, Донну Тартт и т.п., а не безымянные.
MIN_RATINGS = 1000       # минимальное число оценок на Google Books
MIN_AVG_RATING = 3.5     # минимальный средний рейтинг

# Маппинг текстовых категорий клавиатуры на список тегов-жанров для API.
# Каждый тег — отдельный subject; при поиске они объединяются через OR,
# то есть дополняют друг друга (книга подходит, если попадает хотя бы в один
# тег), а не исключают. Английские термины дают максимальное покрытие каталога
# (все языки), а описание всё равно переводится на русский (см. translate_to_ru).
MOOD_TO_GENRE = {
    "Уютный вечер": [
        "classic", "historical romance", "literary fiction", "feel-good",
        "cozy", "comfort read", "slice of life", "contemporary romance",
        "bestseller",
    ],
    "Хочу острых ощущений": [
        "thriller", "suspense", "action", "adventure", "psychological thriller",
        "classic", "bestseller",
    ],
    "Немного поплакать": [
        "fiction", "drama", "tearjerker", "family saga", "contemporary fiction",
        "classic", "literary fiction", "bestseller",
    ],
    "Пища для ума": [
        "nonfiction", "psychology", "self-help", "popular science", "philosophy",
        "classic", "bestseller",
    ],
    "Лёгкость и смех": [
        "humor", "comedy", "satire", "funny", "witty",
        "classic", "bestseller",
    ],
    "Уйти от реальности": [
        "fantasy", "science fiction", "magic", "dystopia", "historical fiction",
        "adventure", "classic", "bestseller",
    ],
    "Закрытый клуб": [
        "mystery", "dark academia", "academic", "campus", "detective",
        "boarding school", "literary fiction", "classic", "bestseller",
    ],
    "Проглотить за одну ночь": [
        "mystery", "thriller", "page-turner", "crime", "suspense",
        "literary fiction", "bestseller",
    ],
    "Моральный детокс": [
        "self-help", "inspiration", "personal development", "mindfulness",
        "motivation", "classic", "bestseller",
    ],
}


def _build_query(genres: list[str]) -> str:
    """Собирает поисковый запрос: теги объединяются через OR, чтобы охватить
    максимум книг (книга подходит, если совпадает хотя бы с одним тегом).
    Важно: префикс 'subject:' нельзя ставить перед каждым тегом при OR —
    Google отвечает 503. Поэтому используем free-text OR. Составные теги
    (с пробелом) берём в кавычки, иначе Google читает их как отдельные слова."""
    parts = [f'"{g}"' if " " in g else g for g in genres]
    return " OR ".join(parts)


def _fetch_items(genre: list[str]) -> list[dict] | None:
    """Делает запрос к Google Books API с повторными попытками при 429/5xx.

    Возвращает список «кандидатов» (книг с названием и описанием) или None
    при неустранимой ошибке. Язык не ограничиваем — берём весь каталог.
    """
    params = {
        "q": _build_query(genre),
        "maxResults": 40,
        "printType": "books",
    }
    if API_KEY:
        params["key"] = API_KEY

    # Повторяем до 4 раз с экспоненциальной задержкой (1, 2, 4 сек).
    max_retries = 4
    for attempt in range(max_retries):
        try:
            response = requests.get(API_URL, params=params, timeout=10)
        except requests.RequestException:
            if attempt == max_retries - 1:
                return None
            time.sleep(2 ** attempt)
            continue

        if response.status_code >= 500:
            # 5xx — временная ошибка сервера, повторяем с экспоненциальной задержкой
            if attempt == max_retries - 1:
                return None
            time.sleep(2 ** attempt)
            continue

        if response.status_code == 429:
            # Too Many Requests: квота исчерпана. Не «бомбим» API повторами —
            # делаем максимум одну попытку, уважая заголовок Retry-After, иначе
            # сразу выходим, чтобы не ухудшать и без того жёсткий лимит.
            retry_after = response.headers.get("Retry-After")
            if attempt == 0 and retry_after and retry_after.isdigit():
                time.sleep(int(retry_after))
                continue
            return None

        try:
            response.raise_for_status()
        except requests.RequestException:
            return None

        data = response.json()
        items = data.get("items") or []
        candidates = []
        for item in items:
            info = item.get("volumeInfo", {})
            title = info.get("title")
            description = info.get("description")
            if title and description:
                authors = info.get("authors", ["Неизвестный автор"])
                # Метрики популярности из Google Books
                ratings = info.get("ratingsCount", 0) or 0
                avg = info.get("averageRating", 0) or 0
                # Ссылка на книгу в Google Books (если есть)
                link = info.get("infoLink") or info.get("canonicalVolumeLink") or ""
                candidates.append(
                    {
                        "Название": title,
                        "Автор": ", ".join(authors),
                        "Описание": description,
                        "Ссылка": link,
                        # служебные поля для ранжирования (удаляются перед выдачей)
                        "_ratings": ratings,
                        "_avg": avg,
                    }
                )
        # Фильтруем по популярности: оставляем только книги с большим числом
        # оценок и приемлемым рейтингом, чтобы не выдавать малознакомые
        # самоиздаты и неизвестные произведения. Если после жёсткого фильтра
        # ничего не осталось — ослабляем требования (сначала по числу оценок,
        # затем убираем фильтр вовсе), чтобы не показывать пустую полку при
        # наличии книг в каталоге.
        popular = [
            c for c in candidates
            if c["_ratings"] >= MIN_RATINGS and c["_avg"] >= MIN_AVG_RATING
        ]
        if not popular:
            popular = [c for c in candidates if c["_ratings"] >= MIN_RATINGS // 10]
        if not popular:
            popular = candidates
        candidates = popular

        # Ранжируем по популярности: сначала больше оценок, затем выше средний рейтинг
        candidates.sort(key=lambda c: (c["_ratings"], c["_avg"]), reverse=True)
        return candidates

    return None


def get_random_book(genre: list[str]) -> dict:
    """Возвращает случайную книгу по жанру (весь каталог, любой язык),
    переводя описание на русский. Использует кэш и повторные попытки
    запроса при ошибках Google Books API.

    При отсутствии подходящих книг или неустранимой ошибке запроса
    возвращает словарь с сообщением в поле 'Описание'.
    """
    # Проверяем кэш (актуальный — не старше CACHE_TTL)
    now = time.time()
    cache_key = tuple(genre)
    cached = _cache.get(cache_key)
    if cached is not None and (now - _cache_ts.get(cache_key, 0)) < CACHE_TTL:
        if not cached:
            return {
                "Название": "—",
                "Автор": "—",
                "Описание": "По этому настроению полки оказались пусты. Попробуй другое настроение.",
            }
        top = cached[:TOP_N]
        return _localize(random.choice(top))

    candidates = _fetch_items(genre)

    # Сохраняем в кэш (даже пустой результат, чтобы не бомбить API подряд)
    _cache[cache_key] = candidates or []
    _cache_ts[cache_key] = now

    if candidates is None:
        return {
            "Название": "—",
            "Автор": "—",
            "Описание": (
                "Не удалось достучаться до винного погреба Google — сейчас там "
                "слишком много гостей (превышен лимит запросов). Попробуй ещё раз "
                "чуть позже или выбери другое настроение."
            ),
        }

    if not candidates:
        return {
            "Название": "—",
            "Автор": "—",
            "Описание": "По этому настроению полки оказались пусты. Попробуй другое настроение.",
        }

    # Выбираем из топа по популярности (TOP_N), чтобы выдавать в первую
    # очередь популярные книги, но оставлять небольшое разнообразие.
    top = candidates[:TOP_N] if len(candidates) > TOP_N else candidates
    return _localize(random.choice(top))


def _trim_description(text: str, max_sentences: int = 4, max_lines: int = 8) -> str:
    """Обрезает аннотацию: не более max_sentences предложений и max_lines строк.
    При обрезке ставит многоточие в конце."""
    if not text:
        return text
    # Делим на предложения по точке/восклицательному/вопросительному знаку
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    if len(sentences) > max_sentences:
        text = " ".join(sentences[:max_sentences]).rstrip() + "…"
    else:
        text = " ".join(sentences)
    # Дополнительно ограничиваем количество строк
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]).rstrip() + "…"
    return text


def _localize(book: dict) -> dict:
    """Переводит название, автора и описание выбранной книги на русский,
    обрезает описание до разумного размера и удаляет служебные поля
    ранжирования. Возвращает новый словарь."""
    result = {k: v for k, v in book.items() if not k.startswith("_")}
    result["Название"] = translate_to_ru(book["Название"])
    result["Автор"] = translate_to_ru(book["Автор"])
    result["Описание"] = _trim_description(translate_to_ru(book["Описание"]))
    return result


# Кураторская подборка сомелье (books.json) — гарантированно популярные,
# узнаваемые книги с готовым «послевкусием». Это основной источник рекомендаций.
CURATED_FILE = "books.json"
_curated_cache: dict | None = None


def _load_curated() -> dict:
    """Загружает кураторскую подборку из books.json (с кэшированием)."""
    global _curated_cache
    if _curated_cache is None:
        try:
            with open(CURATED_FILE, "r", encoding="utf-8") as f:
                _curated_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _curated_cache = {}
    return _curated_cache


def get_book_for_mood(mood: str) -> dict:
    """Возвращает книгу по настроению.

    Сначала берём из кураторской подборки books.json — это популярные,
    узнаваемые книги с авторским «послевкусием» от сомелье. Если настроения
    нет в подборке, откатываемся на Google Books (весь каталог, с переводом).
    """
    curated = _load_curated()
    books = curated.get(mood)
    if books:
        b = random.choice(books)
        return {
            "Название": b.get("title", "—"),
            "Автор": b.get("author", "—"),
            "Описание": b.get("annotation", ""),
            # Готовое послевкусие от сомелье (если есть)
            "Послевкусие": b.get("aftertaste", ""),
            "Ссылка": "",
        }

    # Запасной вариант — Google Books
    genre = MOOD_TO_GENRE.get(mood, ["fiction"])
    return get_random_book(genre)
