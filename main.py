import asyncio
from aiogram import Bot, Dispatcher

from config import load_config
from state import State
from db import DB
from bot_commands import BotApp


async def main():
    cfg = load_config()
    state = State()
    db = DB("bot.db")
    await db.init()

    bot = Bot(cfg.bot_token)
    dp = Dispatcher()

    app = BotApp(cfg, state, db)
    await app.set_menu(bot)
    app.register(dp, bot)

    task = asyncio.create_task(app.background_loop(bot))

    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
    