import os
import json
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
CHANNEL_ID = os.environ["CHANNEL_ID"]  # например: @seagullschannel

QUEUE_FILE = "queue.json"
SEEN_FILE = "seen.json"

# ─── RSS-фиды Reddit (без API, без ключей) ───────────────────────────────────

FEEDS = [
    "https://www.reddit.com/search.rss?q=new&sort=new&limit=10",
    "https://www.reddit.com/search.rss?q=seagull+photo&sort=new&limit=10",
    "https://www.reddit.com/r/whatsthisbird/search.rss?q=seagull&sort=new&limit=10",
]

# ─── Время публикации (UTC, +3 = МСК) ────────────────────────────────────────
# "9,15,21" = публикации в 12:00, 18:00, 00:00 МСК

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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def check_feeds(app: Application):
    seen = load_json(SEEN_FILE)
    total_new = 0

    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url, request_headers=HEADERS)
            entries = feed.entries
        except Exception as e:
            print(f"Ошибка фида {feed_url}: {e}")
            await app.bot.send_message(ADMIN_ID, f"⚠️ Ошибка фида:\n{e}")
            continue

        print(f"Фид: {feed_url} — записей: {len(entries)}")

        for entry in entries[:5]:
            entry_id = entry.get("id", entry.get("link", ""))
            if entry_id in seen:
                continue

            seen.append(entry_id)
            save_json(SEEN_FILE, seen[-500:])

            title = (entry.get("title") or "")[:120]
            post_link = entry.get("link", "")

            # Пробуем вытащить картинку из содержимого поста
            media_url = None
            content = entry.get("summary", "") or ""
            if content:
                import re
                img_match = re.search(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|gif|webp)', content)
                if img_match:
                    media_url = img_match.group(0)

            # Определяем subreddit из ссылки
            subreddit = "Reddit"
            if "/r/" in post_link:
                parts = post_link.split("/r/")
                if len(parts) > 1:
                    subreddit = "r/" + parts[1].split("/")[0]

            caption = (
                f"🐦 <b>{subreddit}</b>\n"
                f"{title}\n\n"
                f'<a href="{post_link}">👉 Открыть оригинал</a>\n\n'
                f"<i>Напиши описание и нажми ✅, или пропусти.</i>"
            )

            short_id = entry_id[-60:] if len(entry_id) > 60 else entry_id

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ В очередь",
                    callback_data=f"queue|{short_id}|{post_link}|{media_url or ''}"
                ),
                InlineKeyboardButton(
                    "❌ Пропустить",
                    callback_data=f"skip|{short_id}"
                ),
            ]])

            try:
                if media_url:
                    await app.bot.send_photo(
                        ADMIN_ID, media_url,
                        caption=caption, parse_mode="HTML",
                        reply_markup=keyboard
                    )
                else:
                    await app.bot.send_message(
                        ADMIN_ID, caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=False
                    )
                total_new += 1
            except Exception as e:
                print(f"Ошибка отправки карточки: {e}")
                try:
                    await app.bot.send_message(
                        ADMIN_ID, caption,
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                    total_new += 1
                except Exception as e2:
                    print(f"Ошибка запасной отправки: {e2}")

    if total_new == 0:
        await app.bot.send_message(ADMIN_ID, "ℹ️ Новых постов не найдено.")
    else:
        await app.bot.send_message(ADMIN_ID, f"✅ Найдено новых постов: {total_new}")


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|", 3)
    action = parts[0]

    if action == "skip":
        text = "❌ Пропущено"
        try:
            if query.message.photo:
                await query.edit_message_caption(text)
            else:
                await query.edit_message_text(text)
        except Exception:
            pass

    elif action == "queue":
        _, post_id, link, media = parts
        ctx.user_data["pending"] = {"id": post_id, "link": link, "media": media}
        prompt = (
            f"✏️ <b>Напиши описание для этого поста</b> — отправь следующим сообщением.\n"
            f'<a href="{link}">Оригинал</a>'
        )
        try:
            if query.message.photo:
                await query.edit_message_caption(prompt, parse_mode="HTML", reply_markup=None)
            else:
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
        if post.get("media"):
            await app.bot.send_photo(
                CHANNEL_ID, post["media"],
                caption=caption, parse_mode="HTML"
            )
        else:
            await app.bot.send_message(
                CHANNEL_ID, caption, parse_mode="HTML"
            )
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
        "Я буду присылать тебе посты из Reddit каждые 2 часа.\n"
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
    scheduler.add_job(check_feeds, "interval", hours=2, args=[app])
    scheduler.add_job(publish_next, "cron", hour=PUBLISH_HOURS, args=[app])
    scheduler.start()

    print("🐦 Seagull Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
