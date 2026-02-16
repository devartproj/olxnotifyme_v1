import asyncio
import time
import httpx

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)

from config import Config
from state import State, fmt_ts
from db import DB
from olx import fetch_html, parse_list_page, extract_image_from_listing_page


def feed_kb(push_enabled: bool) -> InlineKeyboardMarkup:
    bell = "🔔 Новые: ON" if push_enabled else "🔕 Новые: OFF"
    bell_cb = "push_off" if push_enabled else "push_on"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Следующее", callback_data="feed_next")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="feed_refresh"),
         InlineKeyboardButton(text="📊 Статус", callback_data="feed_status")],
        [InlineKeyboardButton(text=bell, callback_data=bell_cb)]
    ])


def open_kb(url: str, push_enabled: bool) -> InlineKeyboardMarkup:
    # После показа объявления оставляем: открыть + следующее + переключатель push
    bell = "🔔 Новые: ON" if push_enabled else "🔕 Новые: OFF"
    bell_cb = "push_off" if push_enabled else "push_on"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть OLX", url=url)],
        [InlineKeyboardButton(text="➡️ Следующее", callback_data="feed_next")],
        [InlineKeyboardButton(text=bell, callback_data=bell_cb)]
    ])


class BotApp:
    def __init__(self, cfg: Config, state: State, db: DB):
        self.cfg = cfg
        self.state = state
        self.db = db

        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": cfg.user_agent,
                "Accept-Language": "uk,ru;q=0.9,en;q=0.8",
            }
        )

    async def close(self):
        await self.client.aclose()

    async def set_menu(self, bot: Bot):
        await bot.set_my_commands([
            BotCommand(command="start", description="Меню и кнопки"),
            BotCommand(command="status", description="Статус"),
            BotCommand(command="check", description="Обновить базу сейчас"),
            BotCommand(command="debug", description="Debug on/off"),
            BotCommand(command="reset", description="Сбросить круг (для меня)"),
        ])

    # ---------- parsing / queue ----------
    async def update_queue_from_olx(self) -> list[dict]:
        """
        Пополняет общую очередь.
        Возвращает список реально новых объявлений: [{key,title,url,image_url,discovered_at}, ...]
        """
        s = self.state
        now_ts = int(time.time())

        s.last_check_ts = now_ts
        s.total_checks += 1
        s.last_added_in_check = 0
        s.last_found_on_page = 0

        list_html, _final_url = await fetch_html(self.client, self.cfg.olx_url)
        listings = parse_list_page(list_html)
        s.last_found_on_page = len(listings)

        newly_added: list[dict] = []
        added = 0

        for it in listings:
            if await self.db.was_seen(it.key):
                continue

            await self.db.mark_seen(it.key)

            img = None
            try:
                ad_html, _ = await fetch_html(self.client, it.url)
                img = extract_image_from_listing_page(ad_html)
            except Exception:
                img = None

            inserted = await self.db.add_to_queue(it.key, it.title, it.url, img)
            if inserted:
                added += 1
                newly_added.append({
                    "key": it.key,
                    "title": it.title,
                    "url": it.url,
                    "image_url": img,
                    "discovered_at": now_ts,
                })
                s.total_added_to_queue += 1

            await asyncio.sleep(0.7)

        s.last_added_in_check = added
        s.last_ok_ts = int(time.time())
        s.last_error = ""
        return newly_added

    # ---------- авто-рассылка новых ----------
    async def broadcast_new_to_push_users(self, bot: Bot, new_items: list[dict]):
        if not new_items:
            return

        # кому слать
        push_users = await self.db.list_push_subscribers()  # [(chat_id, enabled_at), ...]

        for chat_id, enabled_at in push_users:
            # шлем только то, что "после включения"
            items_for_user = [x for x in new_items if x["discovered_at"] >= enabled_at]
            if not items_for_user:
                continue

            # отправляем по одному, чтобы не получить лимиты
            for it in reversed(items_for_user):  # старее->новее
                # антидубль
                if await self.db.was_pushed(chat_id, it["key"]):
                    continue

                # у пользователя может быть уже выключено, но запись пока не обновилась (редко)
                enabled, _ = await self.db.get_push_settings(chat_id)
                if not enabled:
                    break

                caption = f"🆕 Новое объявление\n{it['title']}\n{it['url']}"
                caption = caption[:1000]

                try:
                    # клавиатура под полученное
                    enabled_now, _ = await self.db.get_push_settings(chat_id)
                    if it["image_url"]:
                        await bot.send_photo(chat_id, photo=it["image_url"], caption=caption, reply_markup=open_kb(it["url"], enabled_now))
                    else:
                        await bot.send_message(chat_id, caption, reply_markup=open_kb(it["url"], enabled_now))

                    await self.db.mark_pushed(chat_id, it["key"])
                    await asyncio.sleep(0.05)

                except Exception:
                    # если бот не может писать пользователю (блок/нет доступа) — просто пропустим
                    continue

    # ---------- "лента по кнопке" ----------
    async def send_next(self, bot: Bot, chat_id: int):
        enabled, _ = await self.db.get_push_settings(chat_id)

        item = await self.db.get_next_unshown_for_chat(chat_id)
        if item is None:
            # по кругу
            await self.db.reset_shown_for_chat(chat_id)
            item = await self.db.get_next_unshown_for_chat(chat_id)
            if item is None:
                await bot.send_message(chat_id, "Очередь пустая 😕\nНажми «🔄 Обновить».", reply_markup=feed_kb(enabled))
                return
            await bot.send_message(chat_id, "🔁 Непоказанные закончились — начинаю по кругу.", reply_markup=feed_kb(enabled))

        await self.db.mark_shown_for_chat(chat_id, item["key"])

        caption = f"🆕 {item['title']}\n{item['url']}"
        caption = caption[:1000]

        if item["image_url"]:
            await bot.send_photo(chat_id, photo=item["image_url"], caption=caption, reply_markup=open_kb(item["url"], enabled))
        else:
            await bot.send_message(chat_id, caption, reply_markup=open_kb(item["url"], enabled))

    # ---------- background loop ----------
    async def background_loop(self, bot: Bot):
        while True:
            try:
                new_items = await self.update_queue_from_olx()
                await self.broadcast_new_to_push_users(bot, new_items)
            except Exception as e:
                self.state.last_error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(self.cfg.poll_seconds)


    # ---------- handlers ----------
    async def cmd_start(self, message: Message):
        chat_id = message.chat.id
        await self.db.add_subscriber(chat_id)

        total, unshown = await self.db.counts_for_chat(chat_id)
        push_enabled, _ = await self.db.get_push_settings(chat_id)

        await message.answer(
            "🤖 OLX NotifyMe Bot\n\n"
            "Режим: лента по кнопке.\n"
            "➡️ «Следующее» — показать 1 объявление.\n"
            "🔔 «Новые: ON» — буду присылать все свежие автоматически.\n\n"
            f"📦 В очереди: {total}, для тебя непоказанных: {unshown}",
            reply_markup=feed_kb(push_enabled)
        )

    async def cmd_status(self, message: Message):
        chat_id = message.chat.id
        total, unshown = await self.db.counts_for_chat(chat_id)
        push_enabled, enabled_at = await self.db.get_push_settings(chat_id)
        s = self.state

        text = (
            "📊 Статус\n"
            f"• Push: {'ON' if push_enabled else 'OFF'}\n"
            f"• Push включен с: {fmt_ts(enabled_at) if enabled_at else '—'}\n"
            f"• Debug: {'ON' if s.debug else 'OFF'}\n"
            f"• Последняя проверка: {fmt_ts(s.last_check_ts)}\n"
            f"• Последний успех: {fmt_ts(s.last_ok_ts)}\n"
            f"• Найдено на странице: {s.last_found_on_page}\n"
            f"• Добавлено в очередь (последний раз): {s.last_added_in_check}\n"
            f"• Всего добавлено в очередь: {s.total_added_to_queue}\n"
            f"• Очередь всего: {total}\n"
            f"• Непоказанных для тебя: {unshown}\n"
            f"• Последняя ошибка: {s.last_error or '—'}"
        )
        await message.answer(text, reply_markup=feed_kb(push_enabled))

    async def cmd_debug(self, message: Message):
        parts = (message.text or "").split()
        if len(parts) >= 2 and parts[1].lower() in ("on", "1", "true", "yes"):
            self.state.debug = True
            await message.answer("🐞 Debug включён.")
        elif len(parts) >= 2 and parts[1].lower() in ("off", "0", "false", "no"):
            self.state.debug = False
            await message.answer("🧹 Debug выключен.")
        else:
            await message.answer("Используй: /debug on или /debug off")

    async def cmd_check(self, message: Message):
        chat_id = message.chat.id
        push_enabled, _ = await self.db.get_push_settings(chat_id)

        await message.answer("🔄 Обновляю базу OLX…")
        try:
            new_items = await self.update_queue_from_olx()
            # Если у пользователя включен push — он и так получит эти новые из broadcast в фоне,
            # но при ручной проверке тоже можно отправить сразу:
            # (оставим без спама — просто покажем счетчик)
            total, unshown = await self.db.counts_for_chat(chat_id)
            await message.answer(
                f"✅ Готово. Новых добавлено: {len(new_items)}\nОчередь: {total}, для тебя непоказанных: {unshown}.",
                reply_markup=feed_kb(push_enabled)
            )
        except Exception as e:
            self.state.last_error = f"{type(e).__name__}: {e}"
            await message.answer(f"⚠️ Ошибка: {self.state.last_error}", reply_markup=feed_kb(push_enabled))

    async def cmd_reset(self, message: Message):
        chat_id = message.chat.id
        await self.db.reset_shown_for_chat(chat_id)
        push_enabled, _ = await self.db.get_push_settings(chat_id)
        await message.answer("🔁 Твой круг сброшен. Теперь снова покажу всё с начала.", reply_markup=feed_kb(push_enabled))

    # ---------- push toggle ----------
    async def set_push(self, bot: Bot, chat_id: int, enabled: bool):
        now_ts = int(time.time())
        if enabled:
            # важное: включаем "сейчас", чтобы не послать весь старый архив
            await self.db.set_push_enabled(chat_id, True, now_ts)
        else:
            await self.db.set_push_enabled(chat_id, False, 0)

        push_enabled, _ = await self.db.get_push_settings(chat_id)
        await bot.send_message(
            chat_id,
            f"{'🔔 Авто-рассылка включена.' if push_enabled else '🔕 Авто-рассылка выключена.'}",
            reply_markup=feed_kb(push_enabled)
        )

    async def on_callback(self, cb: CallbackQuery, bot: Bot):
        data = cb.data or ""
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id

        await cb.answer()

        if data == "feed_next":
            await self.send_next(bot, chat_id)

        elif data == "feed_refresh":
            push_enabled, _ = await self.db.get_push_settings(chat_id)
            await bot.send_message(chat_id, "🔄 Обновляю базу…")
            try:
                new_items = await self.update_queue_from_olx()
                total, unshown = await self.db.counts_for_chat(chat_id)
                await bot.send_message(
                    chat_id,
                    f"✅ Готово. Новых добавлено: {len(new_items)}\nОчередь: {total}, для тебя непоказанных: {unshown}.",
                    reply_markup=feed_kb(push_enabled)
                )
                # При ручном обновлении — если push у кого-то включен, можно сразу разослать:
                await self.broadcast_new_to_push_users(bot, new_items)
            except Exception as e:
                self.state.last_error = f"{type(e).__name__}: {e}"
                await bot.send_message(chat_id, f"⚠️ Ошибка: {self.state.last_error}", reply_markup=feed_kb(push_enabled))

        elif data == "feed_status":
            push_enabled, _ = await self.db.get_push_settings(chat_id)
            total, unshown = await self.db.counts_for_chat(chat_id)
            await bot.send_message(chat_id, f"📦 Очередь: {total}\n👤 Непоказанных для тебя: {unshown}", reply_markup=feed_kb(push_enabled))

        elif data == "push_on":
            await self.set_push(bot, chat_id, True)

        elif data == "push_off":
            await self.set_push(bot, chat_id, False)

    def register(self, dp: Dispatcher, bot: Bot):
        dp.message.register(self.cmd_start, Command("start"))
        dp.message.register(self.cmd_status, Command("status"))
        dp.message.register(self.cmd_check, Command("check"))
        dp.message.register(self.cmd_debug, Command("debug"))
        dp.message.register(self.cmd_reset, Command("reset"))

        async def _cb(cb: CallbackQuery):
            await self.on_callback(cb, bot)

        dp.callback_query.register(_cb)
