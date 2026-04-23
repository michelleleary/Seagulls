import os
import json
import asyncio
import aiohttp
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
UNSPLASH_KEY = os.environ["UNSPLASH_ACCESS_KEY"]

QUEUE_FILE = "/tmp/queue.json"

SEEN_IDS = set()
POST_STORE = {}
POST_COUNTER = 0

# ─── Поисковые запросы Unsplash ───────────────────────────────────────────────

QUERIES = ["seagull", "seagull flying", "seagull beach", "gull bird"]

PUBLISH_HOURS = "9,15,21"

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


async def fetch_unsplash(query: str, page: int = 1):
    """Запрашиваем фото из Unsplash."""
    url = "https://api.unsplash.com/search/photos"
    params = {
        "query": query,
        "per_page": 10,
        "page": page,
        "order_by": "latest",
    }
    headers = {"Authorization": f"Client-ID {UNSPLASH_KEY}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Unsplash вернул {resp.status}: {text[:200]}")
            data = await resp.json()
            return data.get("results", [])


async def check_feeds(app: Application):
    global SEEN_IDS, POST_STORE, POST_COUNTER
    total_new = 0

    for query in QUERIES:
        try:
            photos = await fetch_unsplash(query)
        except Exception as e:
            await app.bot.send_message(ADMIN_ID, f"⚠️ Ошибка Unsplash ({query}):\n{e}")
            continue

        for photo in photos:
            photo_id = photo.get("id", "")
            if photo_id in SEEN_IDS:
                continue
            SEEN_IDS.add(photo_id)

            # Данные фото
            img_url = photo.get("urls", {}).get("regular", "")
            author = photo.get("user", {}).get("name", "unknown")
            author_link = photo.get("user", {}).get("links", {}).get("html", "")
            description = photo.get("description") or photo.get("alt_description") or "seagull"
            photo_link = photo.get("links", {}).get("html", "")

            # Сохраняем в хранилище
            POST_COUNTER += 1
            post_key = str(POST_COUNTER)
            POST_STORE[post_key] = {
                "link": photo_link,
                "media": img_url,
                "media_type": "photo",
                "author": author,
            }

            caption = (
                f"🐦 <b>Unsplash — {query}</b>\n"
                f"📝 {description[:150]}\n"
                f'👤 <a href="{author_link}">{author}</a>\n\n'
                f'<a href="{photo_link}">👉 Оригинал</a>'
            )

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ В очередь", callback_data=f"q|{post_key}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"s|{post_key}"),
            ]])

            try:
                await app.bot.send_photo(
                    ADMIN_ID, img_url,
                    caption=caption, parse_mode="HTML",
                    reply_markup=keyboard
                )
                total_new += 1
            except Exception as e:
                print(f"Ошибка отправки фото: {e}")
                try:
                    await app.bot.send_message(
                        ADMIN_ID, caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=False
                    )
                    total_new += 1
                except Exception as e2:
                    print(f"Ошибка запасной отправки: {e2}")

            # Небольшая пауза чтобы не спамить
            await asyncio.sleep(0.5)

    if total_new == 0:
        await app.bot.send_message(ADMIN_ID, "ℹ️ Новых фото не найдено.")
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
            await query.edit_message_caption("❌ Пропущено")
        except Exception:
            pass

    elif action == "q":
        post = POST_STORE.get(post_key, {})
        ctx.user_data["pending"] = post
        prompt = (
            f"✏️ <b>Напиши описание для публикации в канал</b>\n"
            f"Отправь следующим сообщением.\n\n"
            f'<a href="{post.get("link", "")}">Оригинал на Unsplash</a>'
        )
        try:
            await query.edit_message_caption(prompt, parse_mode="HTML", reply_markup=None)
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
        "media_type": pending.get("media_type", "photo"),
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
        caption += f'\n\n<a href="{post["link"]}">Фото: Unsplash</a>'

    try:
        media = post.get("media", "")
        if media:
            await app.bot.send_photo(CHANNEL_ID, media, caption=caption, parse_mode="HTML")
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
        "Каждые 2 часа я буду присылать фото чаек с Unsplash.\n"
        "Нажимай ✅, пиши описание — фото уйдёт в очередь.\n"
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
    await update.message.reply_text("🔍 Ищу фото чаек…")
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
