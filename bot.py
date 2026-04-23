import os
import json
import re
import feedparser
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_CHAT_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]

QUEUE_FILE = "/tmp/queue.json"

# seen хранится только в памяти
SEEN_IDS = set()

# Хранилище постов по короткому ключу (чтобы не пихать URL в кнопки)
POST_STORE = {}
POST_COUNTER = 0

# ─── Фиды ─────────────────────────────────────────────────────────────────────

FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://old.reddit.com/r/seagulls/new.rss",
]

# ─── Время публикации (UTC, +3 = МСК) ────────────────────────────────────────

PUBLISH_HOURS = "9,15,21"

HEADERS = {"User-Agent": "SeagullBot/1.0 (telegram channel aggregator)"}

# ──────────────────────────────────────────────────────────────────────────────

def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения {path}: {e}")


async def check_feeds(app: Application):
    global SEEN_IDS, POST_STORE, POST_COUNTER
    total_new = 0

    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url, request_headers=HEADERS)
            entries = feed.entries
        except Exception as e:
            print(f"Ошибка фида {feed_url}: {e}")
            await app.bot.send_message(ADMIN_ID, f"⚠️ Ошибка фида:\n{feed_url}\n{e}")
            continue

        for entry in entries[:10]:
            entry_id = entry.get("id", entry.get("link", ""))
            if entry_id in SEEN_IDS:
                continue

            SEEN_IDS.add(entry_id)

            title = (entry.get("title") or "")[:120]
            post_link = entry.get("link", "")

            # Определяем источник
            source = "Новости"
            if "/r/" in post_link:
                parts = post_link.split("/r/")
                if len(parts) > 1:
                    source = "r/" + parts[1].split("/")[0]
            elif "bbc" in feed_url:
                source = "BBC News"

            # Сохраняем пост в хранилище, в кнопку кладём только короткий ключ
            POST_COUNTER += 1
            post_key = str(POST_COUNTER)
            POST_STORE[post_key] = {"link": post_link, "media": ""}

            caption = (
                f"📰 <b>{source}</b>\n"
                f"{title}\n\n"
                f'<a href="{post_link}">👉 Открыть оригинал</a>\n\n'
                f"<i>Напиши описание и нажми ✅, или пропусти.</i>"
            )

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ В очередь", callback_data=f"q|{post_key}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"s|{post_key}"),
            ]])

            try:
                await app.bot.send_message(
                    ADMIN_ID, caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
                total_new += 1
            except Exception as e:
                print(f"Ошибка отправки карточки: {e}")
                await app.bot.send_message(ADMIN_ID, f"⚠️ Ошибка карточки: {e}")

    if total_new == 0:
        await app.bot.send_message(ADMIN_ID, "ℹ️ Новых постов не найдено.")
    else:
        await app.bot.send_message(ADMIN_ID, f"✅ Отправлено карточек: {total_new}")


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|", 1)
    action = parts[0]
    post_key = parts[1] if len(parts) > 1 else ""

    if action == "s":
        try:
            await query.edit_message_text("❌ Пропущено")
        except Exception:
            pass

    elif action == "q":
        post = POST_STORE.get(post_key, {})
        ctx.user_data["pending"] = {
            "link": post.get("link", ""),
            "media": post.get("media", "")
        }
        prompt = (
            f"✏️ <b>Напиши описание для этого поста</b> — отправь следующим сообщением.\n"
            f'<a href="{post.get("link", "")}">Оригинал</a>'
        )
        try:
            await query.edit_message_text(prompt, parse_mode="HTML")
        except Exception:
            pass


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    pending = ctx.user_data.get("pending")
    if not pending:
        await update.message.reply_text(
            "ℹ️ Нажми ✅ под карточкой поста, потом пиши описание."
        )
        return

    queue = load_json(QUEUE_FILE)
    queue.append({
        "caption": update.message.text,
        "link": pending["link"],
        "media": pending["media"],
        "added": datetime.now().isoformat(),
    })
    save_json(QUEUE_FILE, queue)
    ctx.user_data["pending"] = None

    await update.message.reply_text(
        f"✅ Добавлено в очередь!\n📋 Постов в очереди: <b>{len(queue)}</b>",
        parse_mode="HTML"
    )


async def publish_next(app: Application):
    queue = load_json(QUEUE_FILE)
    if not queue:
        return

    post = queue.pop(0)
    save_json(QUEUE_FILE, queue)

    caption = post["caption"]
    if post.get("link"):
        caption += f'\n\n<a href="{post["link"]}">Источник</a>'

    try:
        await app.bot.send_message(CHANNEL_ID, caption, parse_mode="HTML")
        await app.bot.send_message(
            ADMIN_ID,
            f"📤 Опубликовано в канал!\n📋 Осталось в очереди: <b>{len(queue)}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await app.bot.send_message(ADMIN_ID, f"⚠️ Ошибка публикации: {e}")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐦 <b>Seagull Bot запущен!</b>\n\n"
        "Я буду присылать тебе посты каждые 2 часа.\n"
        "Нажимай ✅ под понравившимися, пиши описание — пост встанет в очередь.\n\n"
        "/status — посмотреть очередь\n"
        "/check — проверить прямо сейчас\n"
        "/publish — опубликовать следующий пост вручную",
        parse_mode="HTML"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    queue = load_json(QUEUE_FILE)
    if not queue:
        await update.message.reply_text("📋 Очередь пуста.")
        return
    lines = [f"📋 <b>Постов в очереди: {len(queue)}</b>\n"]
    for i, p in enumerate(queue[:5], 1):
        lines.append(f"{i}. {p['caption'][:60]}…")
    if len(queue) > 5:
        lines.append(f"…и ещё {len(queue)-5}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔍 Проверяю фиды…")
    await check_feeds(ctx.application)


async def cmd_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await publish_next(ctx.application)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("publish", cmd_publish))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_feeds, "interval", hours=2, args=[app], start_date="2099-01-01")
    scheduler.add_job(publish_next, "cron", hour=PUBLISH_HOURS, args=[app])
    scheduler.start()

    print("🐦 Seagull Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
