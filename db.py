import aiosqlite
from typing import Optional


class DB:
    def __init__(self, path: str = "bot.db"):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            # seen: чтобы не добавлять объявление в очередь повторно
            await db.execute("""
                CREATE TABLE IF NOT EXISTS seen (
                    key TEXT PRIMARY KEY,
                    seen_at INTEGER DEFAULT (strftime('%s','now'))
                )
            """)

            # queue: очередь объявлений (общая)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    image_url TEXT,
                    discovered_at INTEGER DEFAULT (strftime('%s','now'))
                )
            """)

            # subscribers: кто нажал /start
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    added_at INTEGER DEFAULT (strftime('%s','now'))
                )
            """)

            # shown: что показали конкретному чату (для режима "по кнопке")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS shown (
                    chat_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    shown_at INTEGER DEFAULT (strftime('%s','now')),
                    PRIMARY KEY(chat_id, key)
                )
            """)

            # settings: настройки пользователя (включена ли авто-рассылка)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    chat_id INTEGER PRIMARY KEY,
                    push_enabled INTEGER DEFAULT 0,
                    push_enabled_at INTEGER DEFAULT 0
                )
            """)

            # pushed: что уже отправили автоматически конкретному чату (чтобы не дублировать)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pushed (
                    chat_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    pushed_at INTEGER DEFAULT (strftime('%s','now')),
                    PRIMARY KEY(chat_id, key)
                )
            """)

            # индексы
            await db.execute("CREATE INDEX IF NOT EXISTS idx_queue_discovered ON queue(discovered_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_shown_chat ON shown(chat_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pushed_chat ON pushed(chat_id)")

            await db.commit()

    # ---------- subscribers ----------
    async def add_subscriber(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO subscribers(chat_id) VALUES(?)", (chat_id,))
            # создадим дефолтные настройки
            await db.execute("INSERT OR IGNORE INTO user_settings(chat_id) VALUES(?)", (chat_id,))
            await db.commit()

    async def list_subscribers(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT chat_id FROM subscribers") as cur:
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    # ---------- user settings ----------
    async def get_push_settings(self, chat_id: int) -> tuple[bool, int]:
        """(enabled, enabled_at_ts)"""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT push_enabled, push_enabled_at FROM user_settings WHERE chat_id = ?",
                (chat_id,),
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return False, 0
                return bool(row[0]), int(row[1])

    async def set_push_enabled(self, chat_id: int, enabled: bool, enabled_at: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO user_settings(chat_id, push_enabled, push_enabled_at)
                VALUES(?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    push_enabled=excluded.push_enabled,
                    push_enabled_at=excluded.push_enabled_at
                """,
                (chat_id, 1 if enabled else 0, enabled_at),
            )
            await db.commit()

    async def list_push_subscribers(self) -> list[tuple[int, int]]:
        """
        Возвращает список (chat_id, push_enabled_at) для тех, у кого push_enabled=1
        """
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT chat_id, push_enabled_at FROM user_settings WHERE push_enabled = 1"
            ) as cur:
                rows = await cur.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

    # ---------- seen ----------
    async def was_seen(self, key: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT 1 FROM seen WHERE key = ?", (key,)) as cur:
                return (await cur.fetchone()) is not None

    async def mark_seen(self, key: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO seen(key) VALUES(?)", (key,))
            await db.commit()

    # ---------- queue ----------
    async def add_to_queue(self, key: str, title: str, url: str, image_url: Optional[str]) -> bool:
        """
        Возвращает True если реально вставили (новое), False если уже было.
        """
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                INSERT OR IGNORE INTO queue(key, title, url, image_url)
                VALUES(?, ?, ?, ?)
            """, (key, title, url, image_url))
            await db.commit()
            return cur.rowcount == 1

    # ---------- shown per chat (лента по кнопке) ----------
    async def get_next_unshown_for_chat(self, chat_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("""
                SELECT q.key, q.title, q.url, q.image_url
                FROM queue q
                LEFT JOIN shown s
                  ON s.key = q.key AND s.chat_id = ?
                WHERE s.key IS NULL
                ORDER BY q.discovered_at DESC
                LIMIT 1
            """, (chat_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return {"key": row[0], "title": row[1], "url": row[2], "image_url": row[3]}

    async def mark_shown_for_chat(self, chat_id: int, key: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO shown(chat_id, key) VALUES(?, ?)", (chat_id, key))
            await db.commit()

    async def reset_shown_for_chat(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM shown WHERE chat_id = ?", (chat_id,))
            await db.commit()

    async def counts_for_chat(self, chat_id: int) -> tuple[int, int]:
        """(total_in_queue, unshown_for_chat)"""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM queue") as c1:
                total = (await c1.fetchone())[0]
            async with db.execute("""
                SELECT COUNT(*)
                FROM queue q
                LEFT JOIN shown s
                  ON s.key = q.key AND s.chat_id = ?
                WHERE s.key IS NULL
            """, (chat_id,)) as c2:
                unshown = (await c2.fetchone())[0]
        return total, unshown

    # ---------- pushed (авто-рассылка) ----------
    async def was_pushed(self, chat_id: int, key: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT 1 FROM pushed WHERE chat_id = ? AND key = ?",
                (chat_id, key),
            ) as cur:
                return (await cur.fetchone()) is not None

    async def mark_pushed(self, chat_id: int, key: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO pushed(chat_id, key) VALUES(?, ?)",
                (chat_id, key),
            )
            await db.commit()
