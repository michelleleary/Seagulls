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

SEEN_IDS = set()
POST_STORE = {}
POST_COUNTER = 0

# ─── Фиды ─────────────────────────────────────────────────────────────────────

FEEDS = [
    "https://www.reddit.com/search/?q=seagull&type=posts&sort=new.rss",
]

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

def extract_media(entry):
    """Вытаскиваем фото или видео из RSS-записи."""
    media_url = None
    media_type = None

    # 1. media_content (стандартный тег)
    media_content = entry.get("media_content", [])
    for m in media_content:
        url = m.get("url", "")
        mtype = m.get("medium", "") or m.get("type", "")
        if "video" in mtype or url.endswith((".mp4", ".gifv", ".gif")):
            return url, "video"
        if "image" in mtype or url.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return url, "photo"

    # 2. Ищем в summary/content
    content = entry.get("summary", "") or ""
    
    # Видео (gifv Reddit конвертируем в mp4)
    video_match = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|gifv)', content)
    if video_match:
        url = video_match.group(0).replace(".gifv", ".mp4")
        return url, "video"

    # Фото
    img_match = re.search(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>]*)?', content)
    if img_match:
        return img_match.group(0), "photo"

    # 3. Ищем i.redd.it напрямую
    reddit_img = re.search(r'https://i\.redd\.it/[^\s"\'<>]+', content)
    if reddit_img:
        return reddit_img.group(0), "photo"

    return None, None


async def check_feeds(app: Application):
    global SEEN_IDS, POST_STORE, POST_COUNTER
    total_new = 0

    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url, request_headers=HEADERS)
            entries = feed.entries
        except Exception as e:
            await app.bot.send_message(ADMIN_ID, f"⚠️ Ошибка фида:\n{feed_url}\n{e}")
            continue

        for entry in entries[:10]:
            entry_id = entry.get("id", entry.get("link", ""))
            if entry_id in SEEN_IDS:
                continue
            SEEN_IDS.add(entry_id)

            title = (entry.get("title") or "без названия")[:200]
            post_link = entry.get("link", "")
            author = entry.get("author", "").replace("/u/", "").replace("u/", "") or "unknown"

            # Источник
            source = "Reddit"
            if "/r/" in post_link:
                parts = post_link.split("/r/")
                if len(parts) > 1:
                    source = "r/" + parts[1].split("/")[0]

            # Медиа
            media_url, media_type = extract_media(entry)

            # Сохраняем в хранилище
            POST_COUNTER += 1
            post_key = str(POST_COUNTER)
            POST_STORE[post_key] = {
                "link": post_link,
                "media": media_url or "",
                "media_type": media_type or "",
                "title": title,
                "author": author,
                "source": source,
            }

            caption = (
                f"🐦 <b>{source}</b>\n"
                f"📝 {title}\n"
                f"👤 u/{author}\n\n"
                f'<a href="{post_link}">👉 Оригинал</a>'
            )

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ В очередь", callback_data=f"q|{post_key}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"s|{post_key}"),
            ]])

            try:
                if media_url and media_type == "photo":
                    await app.bot.send_photo(
                        ADMIN_ID, media_url,
                        caption=caption, parse_mode="HTML",
                        reply_markup=keyboard
                    )
                elif media_url and media_type == "video":
                    await app.bot.send_video(
                        ADMIN_ID, media_url,
                        caption=caption, parse_mode="HTML",
                        reply_markup=keyboard
                    )
                else:
                    await app.bot.send_message(
                        ADMIN_ID, caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True
                    )
                total_new += 1
            except Exception as e:
                # Если медиа не загрузилось — шлём текстом
                try:
                    await app.bot.send_message(
                        ADMIN_ID, caption + "\n\n⚠️ медиа не загрузилось",
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True
                    )
                    total_new += 1
                except Exception as e2:
                    print(f"Ошибка отправки: {e2}")

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
            if query.message.photo or query.message.video:
                await query.edit_message_caption("❌ Пропущено")
            else:
                await query.edit_message_text("❌ Пропущено")
        except Exception:
            pass

    elif action == "q":
        post = POST_STORE.get(post_key, {})
        ctx.user_data["pending"] = post
        prompt = (
            f"✏️ <b>Напиши описание для публикации в канал</b>\n"
            f"Отправь следующим сообщением.\n\n"
            f'<a href="{post.get("link", "")}">Оригинал на Reddit</a>'
        )
        try:
            if query.message.photo or query.message.video:
                await query.edit_message_caption(prompt, parse_mode="HTML", reply_markup=None)
            else:
                await query.edit_message_text(prompt, parse_mode="HTML", reply_markup=None)
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
        "link": pending.get("link", ""),
        "media": pending.get("media", ""),
        "media_type": pending.get("media_type", ""),
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
        await app.bot.send_message(ADMIN_ID, "📋 Очередь пуста — нечего публиковать.")
        return

    post = queue.pop(0)
    save_json(QUEUE_FILE, queue)

    caption = post["caption"]
    if post.get("link"):
        caption += f'\n\n<a href="{post["link"]}">Источник</a>'

    try:
        media = post.get("media", "")
        media_type = post.get("media_type", "")

        if media and media_type == "photo":
            await app.bot.send_photo(CHANNEL_ID, media, caption=caption, parse_mode="HTML")
        elif media and media_type == "video":
            await app.bot.send_video(CHANNEL_ID, media, caption=caption, parse_mode="HTML")
        else:
            await app.bot.send_message(CHANNEL_ID, caption, parse_mode="HTML", disable_web_page_preview=True)

        await app.bot.send_message(
            ADMIN_ID,
            f"📤 Опубликовано!\n📋 Осталось в очереди: <b>{len(queue)}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await app.bot.send_message(ADMIN_ID, f"⚠️ Ошибка публикации: {e}")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐦 <b>Seagull Bot запущен!</b>\n\n"
        "Каждые 2 часа я буду присылать посты из Reddit.\n"
        "Нажимай ✅, пиши описание — пост уйдёт в очередь.\n"
        "В 12:00, 18:00 и 00:00 МСК бот публикует сам.\n\n"
        "/check — проверить сейчас\n"
        "/status — очередь\n"
        "/publish — опубликовать вручную",
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
