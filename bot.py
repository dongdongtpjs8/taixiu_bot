import asyncio
import html
import os
import random
import time

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    BotCommand,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramBadRequest,
    TelegramForbiddenError,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
]

if not TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN. Hãy tạo file .env và khai báo BOT_TOKEN.")

DB_PATH = "taixiu.db"

START_BALANCE = 10_000
MIN_BET = 10
MAX_BET = 50_000

CLAIM_COOLDOWN = 5 * 60
CLAIM_MIN = 200
CLAIM_MAX = 300

ROUND_SECONDS = 30
CONTINUE_TIMEOUT = 15          # giây chờ nút "Bắt đầu phiên mới"
QUICK_BETS = (100, 500, 1_000, 5_000)
ROOM_KEY = "shared_room_chat_id"

session = AiohttpSession(timeout=90)

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=session,
)

dp = Dispatcher()


# =========================================================
# DATABASE
# =========================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER NOT NULL DEFAULT 10000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                last_claim INTEGER NOT NULL DEFAULT 0
            )
        """)

        async with db.execute("PRAGMA table_info(users)") as cur:
            cols = [row[1] for row in await cur.fetchall()]

        if "last_claim" not in cols:
            await db.execute(
                "ALTER TABLE users ADD COLUMN last_claim INTEGER DEFAULT 0"
            )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_rounds (
                round_id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'open',
                started_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                dice1 INTEGER,
                dice2 INTEGER,
                dice3 INTEGER,
                total INTEGER,
                result TEXT,
                room_chat_id INTEGER,
                room_message_id INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS room_bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('tai', 'xiu')),
                amount INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(round_id, user_id, side)
            )
        """)

        await db.commit()


async def get_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")

        await db.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, username, balance, last_claim)
            VALUES (?, ?, ?, 0)
            """,
            (user_id, username or "", START_BALANCE),
        )

        if username:
            await db.execute(
                "UPDATE users SET username=? WHERE user_id=?",
                (username, user_id),
            )

        async with db.execute(
            """
            SELECT balance, wins, losses, last_claim
            FROM users WHERE user_id=?
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()

        await db.commit()
        return tuple(row)


async def update_balance(user_id: int, amount: int, win: bool = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")

        if win is True:
            await db.execute(
                """
                UPDATE users
                SET balance=balance+?, wins=wins+1
                WHERE user_id=?
                """,
                (amount, user_id),
            )
        elif win is False:
            await db.execute(
                """
                UPDATE users
                SET balance=balance-?, losses=losses+1
                WHERE user_id=?
                """,
                (amount, user_id),
            )
        else:
            await db.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (amount, user_id),
            )

        await db.commit()


def roll_dice():
    dice = [random.randint(1, 6) for _ in range(3)]
    total = sum(dice)
    result = "tai" if total >= 11 else "xiu"
    return dice, total, result


def pretty_side(side: str):
    return "🔴 TÀI" if side == "tai" else "🔵 XỈU"


# =========================================================
# PHÒNG TÀI XỈU CHUNG
# =========================================================

def room_keyboard():
    """Bố cục nút đẹp trên điện thoại (2 nút mỗi hàng)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 TÀI 100", callback_data="roombet:tai:100"),
            InlineKeyboardButton(text="🔴 TÀI 500", callback_data="roombet:tai:500"),
        ],
        [
            InlineKeyboardButton(text="🔴 TÀI 1.000", callback_data="roombet:tai:1000"),
            InlineKeyboardButton(text="🔴 TÀI 5.000", callback_data="roombet:tai:5000"),
        ],
        [
            InlineKeyboardButton(text="🔵 XỈU 100", callback_data="roombet:xiu:100"),
            InlineKeyboardButton(text="🔵 XỈU 500", callback_data="roombet:xiu:500"),
        ],
        [
            InlineKeyboardButton(text="🔵 XỈU 1.000", callback_data="roombet:xiu:1000"),
            InlineKeyboardButton(text="🔵 XỈU 5.000", callback_data="roombet:xiu:5000"),
        ],
        [
            InlineKeyboardButton(text="💰 Số dư", callback_data="room:balance"),
            InlineKeyboardButton(text="📊 Cược của tôi", callback_data="room:mybets"),
        ],
    ])


def continue_keyboard(round_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔄 Bắt đầu phiên mới",
            callback_data=f"room:continue:{round_id}"
        )]
    ])


async def get_open_round(db):
    async with db.execute(
        """
        SELECT round_id, started_at, ends_at,
               room_chat_id, room_message_id
        FROM game_rounds
        WHERE status='open'
        ORDER BY round_id DESC
        LIMIT 1
        """
    ) as cur:
        return await cur.fetchone()


async def get_room_chat_id(db=None):
    close_db = db is None
    if close_db:
        db = await aiosqlite.connect(DB_PATH)
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key=?",
            (ROOM_KEY,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] != "0" else None
    finally:
        if close_db:
            await db.close()


async def save_room_chat_id(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (ROOM_KEY, str(chat_id)),
        )
        await db.commit()


async def create_round_if_needed(chat_id: int):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")

        row = await get_open_round(db)
        if row:
            await db.commit()
            return row[0]

        cur = await db.execute(
            """
            INSERT INTO game_rounds
            (status, started_at, ends_at, room_chat_id)
            VALUES('open', ?, ?, ?)
            """,
            (now, now + ROUND_SECONDS, chat_id),
        )
        round_id = cur.lastrowid
        await db.commit()
        return round_id


async def build_room_text(round_id: int):
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT started_at, ends_at, status, total, result,
                   dice1, dice2, dice3
            FROM game_rounds
            WHERE round_id=?
            """,
            (round_id,),
        ) as cur:
            round_row = await cur.fetchone()

        if not round_row:
            return "🎲 Phòng Tài Xỉu"

        (started_at, ends_at, status, total, result, d1, d2, d3) = round_row

        async with db.execute(
            """
            SELECT side, COALESCE(SUM(amount), 0), COUNT(*)
            FROM room_bets
            WHERE round_id=?
            GROUP BY side
            """,
            (round_id,),
        ) as cur:
            sums = await cur.fetchall()

        async with db.execute(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM room_bets
            WHERE round_id=?
            """,
            (round_id,),
        ) as cur:
            player_count = (await cur.fetchone())[0]

    totals = {"tai": 0, "xiu": 0}
    bet_count = 0
    for side, amount, count in sums:
        totals[side] = amount
        bet_count += count

    if status == "open":
        remaining = max(0, ends_at - now)
        status_text = f"🟢 <b>ĐANG NHẬN CƯỢC</b> — còn <b>{remaining} giây</b>"
        result_text = ""
    else:
        status_text = "🔒 <b>ĐÃ ĐÓNG CƯỢC</b>"
        result_text = (
            f"\n🎲 Xúc xắc: <b>{d1} + {d2} + {d3} = {total}</b>\n"
            f"🏁 Kết quả: <b>{pretty_side(result)}</b>"
        )

    return (
        f"🎲 <b>PHÒNG TÀI XỈU CHUNG</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧾 Phiên: <b>#{round_id}</b>\n"
        f"{status_text}\n\n"
        f"🔴 Tổng cược TÀI: <b>{totals['tai']:,}</b>\n"
        f"🔵 Tổng cược XỈU: <b>{totals['xiu']:,}</b>\n"
        f"👥 Người chơi: <b>{player_count}</b> | Lượt cược: <b>{bet_count}</b>"
        f"{result_text}"
    )


async def publish_or_update_room(round_id: int, force_new: bool = False):
    chat_id = await get_room_chat_id()
    if not chat_id:
        return

    text = await build_room_text(round_id)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT room_message_id FROM game_rounds WHERE round_id=?",
            (round_id,),
        ) as cur:
            row = await cur.fetchone()
        message_id = row[0] if row else None

    if message_id and not force_new:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=room_keyboard(),
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except TelegramForbiddenError:
            return

    try:
        sent = await bot.send_message(
            chat_id,
            text,
            reply_markup=room_keyboard(),
        )
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        print(f"Không gửi được bảng phòng: {exc}")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE game_rounds
            SET room_chat_id=?, room_message_id=?
            WHERE round_id=?
            """,
            (chat_id, sent.message_id, round_id),
        )
        await db.commit()


async def settle_round(round_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            "SELECT status, ends_at FROM game_rounds WHERE round_id=?",
            (round_id,),
        ) as cur:
            round_row = await cur.fetchone()

        if not round_row or round_row[0] != "open" or round_row[1] > int(time.time()):
            await db.rollback()
            return False

        dice = [random.randint(1, 6) for _ in range(3)]
        total = sum(dice)
        result = "tai" if total >= 11 else "xiu"

        await db.execute(
            """
            UPDATE game_rounds
            SET status='settled',
                dice1=?, dice2=?, dice3=?,
                total=?, result=?
            WHERE round_id=? AND status='open'
            """,
            (*dice, total, result, round_id),
        )

        async with db.execute(
            "SELECT user_id, side, amount FROM room_bets WHERE round_id=?",
            (round_id,),
        ) as cur:
            bets = await cur.fetchall()

        for user_id, side, amount in bets:
            if side == result:
                await db.execute(
                    """
                    UPDATE users
                    SET balance=balance+?, wins=wins+1
                    WHERE user_id=?
                    """,
                    (amount * 2, user_id),
                )
            else:
                await db.execute(
                    "UPDATE users SET losses=losses+1 WHERE user_id=?",
                    (user_id,),
                )

        await db.commit()
    return True


async def send_result_message(round_id: int):
    chat_id = await get_room_chat_id()
    if not chat_id:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT dice1, dice2, dice3, total, result
            FROM game_rounds WHERE round_id=?
            """,
            (round_id,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return

        d1, d2, d3, total, result = row

        async with db.execute(
            """
            SELECT u.username, b.amount
            FROM room_bets b
            JOIN users u ON u.user_id = b.user_id
            WHERE b.round_id=? AND b.side=?
            ORDER BY b.amount DESC
            LIMIT 15
            """,
            (round_id, result),
        ) as cur:
            winners = await cur.fetchall()

    text = (
        f"🏁 <b>KẾT QUẢ PHIÊN #{round_id}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎲 Xúc xắc: <b>{d1} + {d2} + {d3} = {total}</b>\n"
        f"👉 Kết quả: <b>{pretty_side(result)}</b>\n\n"
    )

    if winners:
        text += "🏆 <b>Người thắng:</b>\n"
        for i, (uname, amount) in enumerate(winners, 1):
            name = html.escape(uname or "Ẩn danh")
            text += f"{i}. {name} +{amount * 2:,}\n"
    else:
        text += "Không có ai thắng phiên này.\n"

    text += f"\n⏳ Nhấn nút bên dưới trong <b>{CONTINUE_TIMEOUT} giây</b> để chơi tiếp."

    try:
        await bot.send_message(
            chat_id,
            text,
            reply_markup=continue_keyboard(round_id),
        )
    except Exception as e:
        print(f"Lỗi gửi kết quả: {e}")


async def game_loop():
    while True:
        try:
            chat_id = await get_room_chat_id()
            if not chat_id:
                await asyncio.sleep(2)
                continue

            async with aiosqlite.connect(DB_PATH) as db:
                round_row = await get_open_round(db)

            if not round_row:
                await asyncio.sleep(2)
                continue

            round_id, started_at, ends_at, _, message_id = round_row

            if not message_id:
                await publish_or_update_room(round_id, force_new=True)

            now = int(time.time())

            if now >= ends_at:
                did_settle = await settle_round(round_id)
                if did_settle:
                    await publish_or_update_room(round_id)
                    await send_result_message(round_id)

                    # Chờ 15 giây xem có ai bấm tiếp tục không
                    await asyncio.sleep(CONTINUE_TIMEOUT)

                    async with aiosqlite.connect(DB_PATH) as db:
                        new_open = await get_open_round(db)

                    if not new_open:
                        # Không ai bấm → hủy phòng
                        await save_room_chat_id(0)
                        try:
                            await bot.send_message(
                                chat_id,
                                "⏹ Phòng đã tạm dừng vì không có người chơi tiếp.\n"
                                "Gõ /phong để mở lại phòng."
                            )
                        except Exception:
                            pass
                else:
                    await asyncio.sleep(1)
            else:
                await publish_or_update_room(round_id)
                sleep_time = min(5, max(1, ends_at - now))
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Lỗi game_loop: {e}")
            await asyncio.sleep(3)


# =========================================================
# /START + /HELP
# =========================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    username = message.from_user.username or message.from_user.first_name

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (message.from_user.id,),
        ) as cur:
            existing = await cur.fetchone()

    balance, _, _, _ = await get_user(message.from_user.id, username)

    if existing is None:
        intro = f"Bạn được tặng <b>{START_BALANCE:,}</b> điểm ảo để chơi."
    else:
        intro = f"Số dư hiện tại của bạn: <b>{balance:,}</b> điểm."

    await message.answer(
        # f"🎲 <b>Bot Tài Xỉu Giải Trí</b>\n\n"
        f"Chào {html.escape(message.from_user.first_name)}!\n"
        f"{intro}\n\n"
        f"📌 <b>Lệnh chính:</b>\n"
        f"/phong — Mở phòng Tài Xỉu chung trong nhóm\n"
        f"/tai &lt;số tiền&gt; hoặc /xiu &lt;số tiền&gt; — Chơi nhanh riêng\n"
        f"/claim — Nhận xu mỗi 5 phút\n"
        f"/sodu — Xem số dư\n"
        f"/top — Bảng xếp hạng\n"
        f"/lixi &lt;số tiền&gt; — Lì xì (trả lời tin nhắn)\n"
        f"/help — Hướng dẫn chi tiết"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Hướng dẫn Tài Xỉu</b>\n\n"
        "• 3 xúc xắc: tổng 3–10 = XỈU, 11–18 = TÀI\n"
        "• Phòng chung: dùng /phong trong nhóm\n"
        "• Mỗi phiên 30 giây, hết giờ tự tung xúc xắc\n"
        "• Có thể cược cả 2 cửa, mỗi cửa 1 lần/phiên\n"
        "• Thắng nhận gấp 2 tiền cược\n"
        f"• Min {MIN_BET:,} – Max {MAX_BET:,} điểm\n"
        f"• /claim nhận {CLAIM_MIN}–{CLAIM_MAX} điểm mỗi 5 phút\n"
        "• Điểm chỉ mang tính giải trí"
    )


# =========================================================
# SỐ DƯ + CLAIM
# =========================================================

@dp.message(Command("balance", "sodu", "bal"))
async def cmd_balance(message: Message):
    bal, wins, losses, _ = await get_user(
        message.from_user.id,
        message.from_user.username or message.from_user.first_name,
    )
    await message.answer(
        f"💰 <b>{html.escape(message.from_user.first_name)}</b>\n"
        f"Số dư: <b>{bal:,}</b> điểm\n"
        f"Thắng: {wins} | Thua: {losses}"
    )


@dp.message(Command("claim", "nhan", "nhanxu"))
async def cmd_claim(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    await get_user(user_id, username)

    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            "SELECT last_claim FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()

        last = row[0] if row else 0
        remaining = CLAIM_COOLDOWN - (now - last)

        if remaining > 0:
            await db.rollback()
            await message.reply(
                f"⏳ Bạn cần đợi thêm <b>{remaining // 60} phút {remaining % 60} giây</b> nữa."
            )
            return

        amount = random.randint(CLAIM_MIN, CLAIM_MAX)
        await db.execute(
            "UPDATE users SET balance=balance+?, last_claim=? WHERE user_id=?",
            (amount, now, user_id),
        )
        await db.commit()

    new_bal, _, _, _ = await get_user(user_id)
    await message.reply(
        f"🎁 <b>{html.escape(username)}</b> nhận được <b>{amount:,}</b> điểm!\n"
        f"Số dư hiện tại: <b>{new_bal:,}</b> điểm.\n"
        f"Có thể nhận tiếp sau 5 phút."
    )


# =========================================================
# CƯỢC RIÊNG LẺ /tai /xiu
# =========================================================

@dp.message(Command("tai", "xiu"))
async def cmd_bet(message: Message, command: CommandObject):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    choice = command.command.lower()

    if not command.args:
        await message.reply(f"Cú pháp: /{choice} &lt;số tiền&gt;\nVí dụ: /{choice} 100")
        return

    try:
        amount = int(command.args.strip().replace(",", "").replace(".", ""))
    except ValueError:
        await message.reply("Số tiền không hợp lệ!")
        return

    if not MIN_BET <= amount <= MAX_BET:
        await message.reply(f"Cược tối thiểu {MIN_BET:,}, tối đa {MAX_BET:,} điểm.")
        return

    await get_user(user_id, username)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()

        if not row or row[0] < amount:
            await db.rollback()
            await message.reply(f"Bạn không đủ điểm để cược {amount:,}.")
            return

        await db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (amount, user_id),
        )
        await db.commit()

    dice, total, result = roll_dice()
    won = choice == result

    async with aiosqlite.connect(DB_PATH) as db:
        if won:
            await db.execute(
                "UPDATE users SET balance=balance+?, wins=wins+1 WHERE user_id=?",
                (amount * 2, user_id),
            )
        else:
            await db.execute(
                "UPDATE users SET losses=losses+1 WHERE user_id=?",
                (user_id,),
            )
        await db.commit()

    bal, _, _, _ = await get_user(user_id)
    outcome = f"🎉 Thắng {amount:,} điểm!" if won else f"😢 Thua {amount:,} điểm."

    await message.reply(
        f"🎲 {dice[0]} + {dice[1]} + {dice[2]} = <b>{total}</b> → <b>{pretty_side(result)}</b>\n"
        f"{outcome}\n"
        f"Số dư: <b>{bal:,}</b> điểm"
    )


# =========================================================
# /PHONG
# =========================================================

@dp.message(Command("phong", "room"))
async def cmd_room(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Hãy dùng /phong trong nhóm Telegram muốn làm phòng chơi chung.")
        return

    await save_room_chat_id(message.chat.id)

    async with aiosqlite.connect(DB_PATH) as db:
        round_row = await get_open_round(db)

    if round_row:
        round_id = round_row[0]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE game_rounds SET room_chat_id=? WHERE round_id=? AND status='open'",
                (message.chat.id, round_id),
            )
            await db.commit()
        await publish_or_update_room(round_id, force_new=True)
    else:
        round_id = await create_round_if_needed(message.chat.id)
        await publish_or_update_room(round_id, force_new=True)

    await message.reply("✅ Đã thiết lập phòng Tài Xỉu chung tại nhóm này.")


# =========================================================
# CALLBACK NÚT CƯỢC + TIẾP TỤC
# =========================================================

@dp.callback_query(F.data.startswith("roombet:"))
async def callback_room_bet(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer("Hãy đặt cược trong nhóm phòng chơi.", show_alert=True)
        return

    try:
        _, side, amount_text = callback.data.split(":")
        amount = int(amount_text)
    except Exception:
        await callback.answer("Lệnh cược không hợp lệ.", show_alert=True)
        return

    configured_chat_id = await get_room_chat_id()
    if configured_chat_id is None or callback.message.chat.id != configured_chat_id:
        await callback.answer("Nút này không thuộc phòng đang hoạt động.", show_alert=True)
        return

    user = callback.from_user
    if user.is_bot:
        await callback.answer("Bot không thể cược.", show_alert=True)
        return

    if side not in ("tai", "xiu") or not MIN_BET <= amount <= MAX_BET:
        await callback.answer("Cược không hợp lệ.", show_alert=True)
        return

    await get_user(user.id, user.username or user.first_name)
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            """
            SELECT round_id, ends_at, status
            FROM game_rounds
            WHERE status='open'
            ORDER BY round_id DESC LIMIT 1
            """
        ) as cur:
            round_row = await cur.fetchone()

        if not round_row or round_row[2] != "open" or round_row[1] <= now:
            await db.rollback()
            await callback.answer("Phiên đã đóng cược!", show_alert=True)
            return

        round_id = round_row[0]

        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user.id,),
        ) as cur:
            user_row = await cur.fetchone()

        if not user_row or user_row[0] < amount:
            await db.rollback()
            await callback.answer("Số dư không đủ.", show_alert=True)
            return

        async with db.execute(
            "SELECT amount FROM room_bets WHERE round_id=? AND user_id=? AND side=?",
            (round_id, user.id, side),
        ) as cur:
            old_bet = await cur.fetchone()

        if old_bet:
            await db.rollback()
            await callback.answer(
                f"Bạn đã cược {old_bet[0]:,} điểm cửa {side.upper()} rồi.",
                show_alert=True,
            )
            return

        await db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (amount, user.id),
        )
        await db.execute(
            """
            INSERT INTO room_bets (round_id, user_id, side, amount, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (round_id, user.id, side, amount, now),
        )
        await db.commit()

    await callback.answer(f"Đã cược {amount:,} điểm cửa {side.upper()}!")
    await publish_or_update_room(round_id)


@dp.callback_query(F.data.startswith("room:continue:"))
async def callback_continue(callback: CallbackQuery):
    try:
        round_id = int(callback.data.split(":")[2])
    except Exception:
        await callback.answer("Lỗi dữ liệu", show_alert=True)
        return

    chat_id = callback.message.chat.id
    await save_room_chat_id(chat_id)

    new_round_id = await create_round_if_needed(chat_id)
    await publish_or_update_room(new_round_id, force_new=True)

    await callback.answer("✅ Đã bắt đầu phiên mới!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data == "room:balance")
async def callback_room_balance(callback: CallbackQuery):
    bal, wins, losses, _ = await get_user(
        callback.from_user.id,
        callback.from_user.username or callback.from_user.first_name,
    )
    await callback.answer(
        f"Số dư: {bal:,} điểm | Thắng {wins} - Thua {losses}",
        show_alert=True,
    )


@dp.callback_query(F.data == "room:mybets")
async def callback_room_my_bets(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT b.round_id, b.side, b.amount
            FROM room_bets b
            JOIN game_rounds r ON r.round_id = b.round_id
            WHERE b.user_id=? AND r.status='open'
            ORDER BY b.round_id DESC
            """,
            (callback.from_user.id,),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await callback.answer("Bạn chưa đặt cược ở phiên đang mở.", show_alert=True)
        return

    details = "\n".join(
        f"Phiên #{rid}: {side.upper()} {amount:,}"
        for rid, side, amount in rows
    )
    await callback.answer(details, show_alert=True)


# =========================================================
# TOP + ADMIN + LÌ XÌ
# =========================================================

@dp.message(Command("top"))
async def cmd_top(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await message.answer("Chưa có dữ liệu.")
        return

    text = "🏆 <b>Top 10 giàu nhất</b>\n\n"
    for i, (uname, bal) in enumerate(rows, 1):
        text += f"{i}. {html.escape(uname or 'Ẩn danh')}: <b>{bal:,}</b>\n"
    await message.answer(text)


@dp.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        parts = (command.args or "").split()
        target_id = int(parts[0])
        amount = int(parts[1])
        if amount <= 0:
            raise ValueError
        await get_user(target_id)
        await update_balance(target_id, amount)
        await message.reply(f"Đã cộng {amount:,} điểm cho {target_id}.")
    except Exception:
        await message.reply("Cú pháp: /give &lt;user_id&gt; &lt;số tiền dương&gt;")


@dp.message(Command("reset"))
async def cmd_reset(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        target_id = int((command.args or "").strip())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET balance=? WHERE user_id=?",
                (START_BALANCE, target_id),
            )
            await db.commit()
        await message.reply(f"Đã reset số dư của {target_id}.")
    except Exception:
        await message.reply("Cú pháp: /reset &lt;user_id&gt;")


@dp.message(Command("lixi"))
async def cmd_lixi(message: Message, command: CommandObject):
    if not message.reply_to_message:
        await message.reply("🎁 Hãy trả lời tin nhắn người nhận bằng lệnh:\n/lixi 2000")
        return

    target = message.reply_to_message.from_user
    if not target or target.is_bot:
        await message.reply("❌ Không xác định được người nhận.")
        return

    sender = message.from_user
    if sender.id == target.id:
        await message.reply("❌ Bạn không thể tự lì xì cho mình.")
        return

    try:
        amount = int((command.args or "").strip().replace(",", "").replace(".", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply("❌ Số tiền không hợp lệ. Ví dụ: /lixi 2000")
        return

    await get_user(sender.id, sender.username or sender.first_name)
    await get_user(target.id, target.username or target.first_name)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (sender.id,),
        ) as cur:
            sender_row = await cur.fetchone()

        if not sender_row or sender_row[0] < amount:
            await db.rollback()
            await message.reply("❌ Số dư của bạn không đủ để lì xì.")
            return

        await db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (amount, sender.id),
        )
        await db.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (amount, target.id),
        )
        await db.commit()

    sender_balance, _, _, _ = await get_user(sender.id)
    target_balance, _, _, _ = await get_user(target.id)

    await message.reply(
        f"🧧 <b>LÌ XÌ THÀNH CÔNG!</b>\n\n"
        f"🎁 Người lì xì: <b>{html.escape(sender.first_name)}</b>\n"
        f"💌 Người nhận: <b>{html.escape(target.first_name)}</b>\n"
        f"💰 Số tiền: <b>{amount:,}</b> điểm\n\n"
        f"👛 Số dư của bạn: <b>{sender_balance:,}</b>\n"
        f"🎉 Số dư người nhận: <b>{target_balance:,}</b>"
    )


# =========================================================
# MENU LỆNH
# =========================================================

async def set_commands():
    commands = [
        BotCommand(command="start", description="Bắt đầu / Xem hướng dẫn"),
        BotCommand(command="phong", description="Mở phòng Tài Xỉu chung trong nhóm"),
        BotCommand(command="tai", description="Cược Tài riêng lẻ"),
        BotCommand(command="xiu", description="Cược Xỉu riêng lẻ"),
        BotCommand(command="nhanxu", description="Nhận xu mỗi 5 phút"),
        BotCommand(command="sodu", description="Xem số dư"),
        BotCommand(command="top", description="Bảng xếp hạng giàu nhất"),
        BotCommand(command="lixi", description="Lì xì điểm khi trả lời tin nhắn"),
        BotCommand(command="help", description="Hướng dẫn chơi"),
    ]
    await bot.set_my_commands(commands)


# =========================================================
# MAIN
# =========================================================

async def main():
    await init_db()
    await set_commands()

    worker = asyncio.create_task(game_loop())
    print("Bot Tài Xỉu đang chạy...")

    try:
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                await dp.start_polling(bot)
                break
            except TelegramNetworkError as exc:
                print(f"Lỗi mạng lần {attempt}/{max_retries}: {exc}")
                if attempt < max_retries:
                    await asyncio.sleep(5)
                else:
                    raise
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())