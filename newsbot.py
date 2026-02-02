import asyncio
import os

import aiosqlite
import feedparser
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
import random

# Добавьте эти импорты в начало файла к остальным from aiogram...
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv

# Создаем класс для отслеживания состояния "Ожидание ключевого слова"
class Form(StatesGroup): waiting_for_keyword = State()

# --------- Настройки (подставьте ваш токен) ---------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN: exit("Error: BOT_TOKEN not found in environment variables!")

CHECK_INTERVAL = 10
DB_PATH = "news_bot.db"
# Источники (выберите один)
# 1. Американский (CNN Top Stories)
# Словарь источников: "Название": "Ссылка"
# СУПЕР-БЫСТРЫЕ ИСТОЧНИКИ
RSS_FEEDS = {
    # Reddit (r/news /new) — посты выходят каждые 1-2 минуты
    "Reddit News 🌎": "https://www.reddit.com/r/news/new/.rss",

    # CoinTelegraph — Крипта движется быстро
    "Crypto ₿": "https://cointelegraph.com/rss",

    # Lenta.ru (Все новости)
    "Lenta.ru ⚡": "https://lenta.ru/rss/news"
}
# -----------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- Работа с БД ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER, feed_name TEXT, PRIMARY KEY (user_id, feed_name))")
        await db.execute("CREATE TABLE IF NOT EXISTS keywords (user_id INTEGER, keyword TEXT)")

        # НОВАЯ ТАБЛИЦА: Настройки пользователя
        # filter_mode может быть 'all' (все новости) или 'keywords' (только слова)
        await db.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, filter_mode TEXT)")
        await db.commit()

# --- НОВЫЕ ФУНКЦИИ ДЛЯ НАСТРОЕК ---
async def set_filter_mode(user_id, mode):
    """mode: 'all' или 'keywords'"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO user_settings (user_id, filter_mode) VALUES (?, ?)", (user_id, mode))
        await db.commit()

async def get_filter_mode(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT filter_mode FROM user_settings WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            # По умолчанию возвращаем 'all' (все новости), если настройки нет
            return row[0] if row else 'all'

async def add_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def get_user_subscriptions(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT feed_name FROM subscriptions WHERE user_id = ?", (user_id,)) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def toggle_subscription(user_id, feed_name):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM subscriptions WHERE user_id = ? AND feed_name = ?", (user_id, feed_name)) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await db.execute("DELETE FROM subscriptions WHERE user_id = ? AND feed_name = ?", (user_id, feed_name))
            await db.commit()
            return False
        else:
            await db.execute("INSERT INTO subscriptions (user_id, feed_name) VALUES (?, ?)", (user_id, feed_name))
            await db.commit()
            return True

async def get_users_for_feed(feed_name):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM subscriptions WHERE feed_name = ?", (feed_name,)) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

# --- НОВЫЕ ФУНКЦИИ ДЛЯ СЛОВ ---
async def add_keyword(user_id, keyword):
    clean_word = keyword.lower().strip()  # Убираем пробелы и делаем маленькими буквами
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, есть ли слово
        async with db.execute("SELECT 1 FROM keywords WHERE user_id = ? AND keyword = ?",
                              (user_id, clean_word)) as cursor:
            exists = await cursor.fetchone()

        # Если нет — добавляем
        if not exists:
            await db.execute("INSERT INTO keywords (user_id, keyword) VALUES (?, ?)", (user_id, clean_word))
            await db.commit()
            return True  # Успешно добавлено

        return False  # Такое слово уже есть

async def get_user_keywords(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT keyword FROM keywords WHERE user_id = ?", (user_id,)) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def clear_keywords(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        # Удаляем ВСЕ записи для этого пользователя из таблицы keywords
        await db.execute("DELETE FROM keywords WHERE user_id = ?", (user_id,))
        await db.commit()

# (Старые функции настроек оставляем как были)
async def get_last_link(feed_name):
    key = f"last_link_{feed_name}"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_last_link(feed_name, link):
    key = f"last_link_{feed_name}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, link))
        await db.commit()

async def delete_specific_keyword(user_id, keyword):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM keywords WHERE user_id = ? AND keyword = ?", (user_id, keyword))
        await db.commit()


# --- Логика проверки новостей (С ФИЛЬТРАЦИЕЙ) ---
async def check_news():
    print(f"[{datetime.now().time()}] --- Проверка быстрых источников ---")

    for feed_name, feed_url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries:
                continue

            current_latest_entry = feed.entries[0]
            current_latest_link = current_latest_entry.link
            last_saved_link = await get_last_link(feed_name)
            new_posts = []

            # 1. Собираем новые посты
            if last_saved_link is None:
                print(f"🆕 {feed_name}: Первая запись.")
                new_posts.append(current_latest_entry)
            elif last_saved_link == current_latest_link:
                continue
            else:
                for entry in feed.entries:
                    if entry.link == last_saved_link:
                        break
                    new_posts.append(entry)

            # 2. Рассылка
            if new_posts:
                new_posts.reverse()
                # Получаем всех подписчиков этого канала
                users = await get_users_for_feed(feed_name)

                print(f"🔥 {feed_name}: {len(new_posts)} новых постов.")

                if users:
                    for entry in new_posts:
                        # Заголовок и ссылка новости
                        news_title = entry.title
                        news_link = entry.link
                        # Собираем текст для поиска (заголовок + описание, если есть)
                        search_text = (news_title + " " + getattr(entry, 'summary', '')).lower()

                        msg_text = f"⚡ **{feed_name}**\n{news_title}\n👉 {news_link}"

                        for user_id in users:
                            try:
                                # --- ПРОВЕРКА НАСТРОЕК ЮЗЕРА ---
                                mode = await get_filter_mode(user_id)

                                should_send = False

                                if mode == 'all':
                                    # Если режим "Все", отправляем всегда
                                    should_send = True

                                elif mode == 'keywords':
                                    # Если режим "Слова", проверяем совпадения
                                    user_keywords = await get_user_keywords(user_id)
                                    # Проверяем, есть ли хоть одно ключевое слово в тексте новости
                                    if any(kw in search_text for kw in user_keywords):
                                        should_send = True

                                # Отправляем только если проверка прошла
                                if should_send:
                                    await bot.send_message(chat_id=user_id, text=msg_text)
                                    await asyncio.sleep(0.1)

                            except Exception as e:
                                print(f"Ошибка отправки юзеру {user_id}: {e}")

                await set_last_link(feed_name, current_latest_link)

        except Exception as e:
            print(f"Error {feed_name}: {e}")


# --- Фоновая задача ---
async def monitoring_task():
    while True:
        await check_news()
        # Ждем 60 секунд (или меньше, если хотите еще быстрее)
        await asyncio.sleep(CHECK_INTERVAL)


# --- 1. Обновляем команду /start ---
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    # Сначала обязательно добавляем пользователя в БД, иначе ему не придут новости
    await add_user(message.from_user.id)

    # Создаем кнопки (как в начале)
    btn_subs = InlineKeyboardButton(text="Подписки 🔔", callback_data="subscriptions")
    btn_keywords = InlineKeyboardButton(text="Специальные слова 🔑", callback_data="keywords")
    btn_settings = InlineKeyboardButton(text="Настройки ⚙️", callback_data="settings")

    # Собираем клавиатуру
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[btn_subs, btn_keywords], [btn_settings]])

    await message.answer(
        "Привет! Я ваш новостной бот 🌿.\n"
        "Я уже начал следить за новостями. Выберите действие ниже:",
        reply_markup=keyboard
    )

@dp.callback_query()
async def generic_callback(call: CallbackQuery, state: FSMContext):
    data = call.data or ""
    # ИСПРАВЛЕНИЕ: Берем ID того, кто НАЖАЛ кнопку, а не ID бота
    user_id = call.from_user.id

    # Логика для кнопок
    if data == "subscriptions":
        user_subs = await get_user_subscriptions(user_id)
        # Создаем кнопки: если есть в подписках — крестик, если нет — галочка
        buttons = [[InlineKeyboardButton(text=f"{'❌' if name in user_subs else '✅'} {name}", callback_data=f"sub:{name}")] for name in RSS_FEEDS]
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

        await call.message.edit_text("<b>Управление подписками:</b>",  reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        return

    # --- Обработка клика по подписке ---
    elif data.startswith("sub:"):
        feed_name = data.split(":", 1)[1]
        # 1. Переключаем подписку в БД и получаем новый статус (True/False)
        is_subscribed = await toggle_subscription(user_id, feed_name)
        # 2. Получаем актуальный список подписок (чтобы обновить иконки на кнопках)
        user_subs = await get_user_subscriptions(user_id)
        # 3. Перерисовываем кнопки (теперь у нажатого источника сменится значок)
        buttons = [
            [InlineKeyboardButton(text=f"{'❌' if name in user_subs else '✅'} {name}", callback_data=f"sub:{name}")] for
            name in RSS_FEEDS]
        buttons.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu")])
        # 4. Формируем текст уведомления
        if is_subscribed: status_line = f"<b>Вы подключили {feed_name}! ✅</b>"
        else: status_line = f"<b>Вы отключили {feed_name}. ❌</b>"
        # Собираем полный текст: Статус + Стандартное меню
        full_text = (
            f"{status_line}\n\n"
            "<b>Управление подписками</b> 📋\n"
            "<b>✅ — подписаться</b>\n"
            "<b>❌ — отписаться</b>\n"
        )
        # 5. Обновляем сообщение
        await call.message.edit_text(
            text=full_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await call.answer()

    # --- МЕНЮ СЛОВ ---
    elif data == "keywords" or data.startswith("del:"):
        # Если это удаление конкретного слова
        if data.startswith("del:"):
            word_to_delete = data.split(":", 1)[1]
            await delete_specific_keyword(user_id, word_to_delete)
            # Можно показать всплывашку
            await call.answer(f"Слово «{word_to_delete}» удалено 🗑")

        # Генерация меню
        kws = await get_user_keywords(user_id)

        # 1. Создаем кнопки для каждого слова (❌ Слово)
        # callback_data="del:слово"
        word_buttons = [InlineKeyboardButton(text=f"❌ {w}", callback_data=f"del:{w}") for w in kws]

        # 2. Разбиваем кнопки по 2 в ряд
        keyboard = [word_buttons[i:i + 2] for i in range(0, len(word_buttons), 2)]

        # 3. Добавляем кнопки управления снизу
        keyboard.append([InlineKeyboardButton(text="➕ Добавить слово", callback_data="add_kw"),
                         InlineKeyboardButton(text="🗑 Очистить всё", callback_data="clear_kw")])
        keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

        text = "<b>Ваши фильтры:</b>\nНажмите на слово, чтобы удалить его." if kws else "Список пуст. Вы получаете все новости."

        # Если мы пришли сюда через удаление, лучше использовать edit_text, если через меню - тоже
        # Обрабатываем случай, если текст не изменился (например, удалили последнее слово и список стал пустым)
        try:
            await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                                         parse_mode="HTML")
        except:
            # Иногда Телеграм ругается, если сообщение визуально не изменилось
            pass

        if not data.startswith("del:"):
            await call.answer()

    elif data == "add_kw":
        await state.set_state(Form.waiting_for_keyword)
        await call.message.edit_text("Напишите слово для фильтра (например: <code>Bitcoin</code>):",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="🔙 Отмена", callback_data="keywords")]]),
                                     parse_mode="HTML")
        await call.answer()



    elif data == "clear_kw":
        # 1. Сначала проверяем, есть ли что удалять
        current_words = await get_user_keywords(user_id)
        # Создаем стандартные кнопки
        btns = [[InlineKeyboardButton(text="➕ Добавить", callback_data="add_kw"),
                 InlineKeyboardButton(text="🗑 Очистить", callback_data="clear_kw")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]]
        if not current_words:
            # Если слов НЕТ — просто уведомляем и не трогаем БД
            await call.answer("Список и так пуст! 🤷‍♂️", show_alert=True)
            # Просто обновляем вид (на всякий случай), но текст оставляем "пустым"
            try:
                await call.message.edit_text("Список ключевых слов пуст (вы получаете все новости).",
                                             reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
            except:
                pass  # Если текст и так такой же, телеграм выдаст ошибку, игнорируем
        else:
            # Если слова ЕСТЬ — удаляем
            await clear_keywords(user_id)
            await call.answer("Список слов полностью очищен! 🗑", show_alert=True)
            # Обновляем сообщение на "пустое"
            await call.message.edit_text("Список ключевых слов пуст (вы получаете все новости).",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

        # --- НАСТРОЙКИ ---

    elif data == "settings" or data.startswith("set_mode:"):
        # Если нажали на кнопку смены режима
        if data.startswith("set_mode:"):
            new_mode = data.split(":")[1]
            await set_filter_mode(user_id, new_mode)
            # Можно показать маленькое уведомление
            mode_text = "Все новости" if new_mode == "all" else "Только по словам"
            await call.answer(f"Режим изменен: {mode_text} ✅")
        # Получаем текущий режим
        current_mode = await get_filter_mode(user_id)
        # Рисуем кнопки (Радио-кнопки)
        # Если режим 'all', ставим галочку там, иначе пустой кружок
        btn_all_text = "🟢 Все новости (Поток)" if current_mode == "all" else "⚪️ Все новости (Поток)"
        btn_kw_text = "🟢 Только по словам" if current_mode == "keywords" else "⚪️ Только по словам"
        btns = [
            [InlineKeyboardButton(text=btn_all_text, callback_data="set_mode:all")],
            [InlineKeyboardButton(text=btn_kw_text, callback_data="set_mode:keywords")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
        ]
        text = (
            "<b>⚙️ Настройки фильтрации</b>\n\n"
            "Выберите, какие новости вы хотите получать из ваших подписок:\n\n"
            "📡 <b>Все новости:</b> Присылает всё подряд из источников, на которые вы подписаны.\n"
            "🔑 <b>Только по словам:</b> Бот молчит, пока в новости не появится одно из ваших ключевых слов."
        )
        # Используем edit_text, но ловим ошибку, если пользователь жмет на уже выбранный режим
        try:
            await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
                                         parse_mode="HTML")
        except:
            pass
        if not data.startswith("set_mode:"):
            await call.answer()
    elif data == "main_menu":
        # Создаем кнопки (как в начале)
        btn_subs = InlineKeyboardButton(text="Подписки 🔔", callback_data="subscriptions")
        btn_keywords = InlineKeyboardButton(text="Специальные слова 🔑", callback_data="keywords")
        btn_settings = InlineKeyboardButton(text="Настройки ⚙️", callback_data="settings")
        # Собираем клавиатуру
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[btn_subs, btn_keywords], [btn_settings]])
        await call.message.edit_text("Привет! Я ваш новостной бот 🌿.\nЯ уже начал следить за новостями. Выберите действие ниже:", reply_markup=keyboard)

    # Обязательно подтверждаем нажатие, чтобы убрались "часики" загрузки
    await call.answer()


# --- Обработчик: когда юзер пишет само слово ---
@dp.message(Form.waiting_for_keyword)
async def process_keyword(message: Message, state: FSMContext):
    user_id = message.from_user.id
    word = message.text

    # 1. Добавляем
    is_added = await add_keyword(user_id, word)
    await state.clear()

    # 2. Формируем красивое меню с кнопками
    kws = await get_user_keywords(user_id)

    # Кнопки слов
    word_buttons = [InlineKeyboardButton(text=f"❌ {w}", callback_data=f"del:{w}") for w in kws]
    # Разбиваем по 2 в ряд
    keyboard = [word_buttons[i:i + 2] for i in range(0, len(word_buttons), 2)]

    # Кнопки управления
    keyboard.append([InlineKeyboardButton(text="➕ Добавить слово", callback_data="add_kw"),
                     InlineKeyboardButton(text="🗑 Очистить всё", callback_data="clear_kw")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

    # Заголовок
    if is_added:
        header = f"✅ Слово <b>«{word}»</b> добавлено!"
    else:
        header = f"⚠️ Слово <b>«{word}»</b> уже есть."

    full_text = f"{header}\n\nНажмите на кнопку со словом, чтобы удалить его."

    await message.answer(full_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

async def main():
    await init_db()
    asyncio.create_task(monitoring_task())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())