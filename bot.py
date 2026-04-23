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
PEXELS_KEY = os.environ["PEXELS_API_KEY"]

QUEUE_FILE = "/tmp/queue.json"
PENDING_FILE = "/tmp/pending.json"  # хранит ожидающий пост между сообщениями

SEEN_IDS = set()
POST_STORE = {}
POST_COUNTER = 0

# Публикация каждый час с 8 до 22 МСК (5-19 UTC)
PUBLISH_HOURS = "5,6,7,8,9,10,11,12,13,14,15,16,17,18,19"

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

def load_pending():
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_pending(data):
    save_json(PENDING_FILE, data)

def clear_pending():
    try:
        os.remove(PENDING_FILE)
    except Exception:
        pass


async def fetch_unsplash():
    results = []
    queries = ["seagull", "seagull flying", "seagull beach"]
    headers = {"Authorization": f"Client-ID {UNSPLASH_KEY}"}
    async with aiohttp.ClientSession() as session:
        for query in queries:
            try:
                async with session.get(
                    "https://api.unsplash.com/search/photos",
                    params={"query": query, "per_page": 5, "order_by": "latest"},
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for photo in data.get("results", []):
                            results.append({
                                "id": "unsplash_" + photo["id"],
                                "source": "Unsplash",
                                "title": photo.get("description") or photo.get("alt_description") or query,
                                "author": photo.get("user", {}).get("name", "unknown"),
                                "author_link": photo.get("user", {}).get("links", {}).get("html", ""),
                                "media": photo.get("urls", {}).get("regular", ""),
                                "media_type": "photo",
                                "link": photo.get("links", {}).get("html", ""),
                            })
            except Exception as e:
                print(f"Ошибка Unsplash ({query}): {e}")
    return results


async def fetch_pexels():
    results = []
    queries = ["seagull", "gull bird"]
    headers = {"Authorization": PEXELS_KEY}
    async with aiohttp.ClientSession() as session:
        for query in queries:
            try:
                async with session.get(
                    "https://api.pexels.com/v1/search",
                    params={"query": query, "per_page": 5},
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for photo in data.get("photos", []):
                            results.append({
                                "id": "pexels_" + str(photo["id"]),
                                "source": "Pexels",
                                "title": photo.get("alt", query),
                                "author": photo.get("photographer", "unknown"),
                                "author_link": photo.get("photographer_url", ""),
                                "media": photo.get("src", {}).get("large", ""),
                                "media_type": "photo",
                                "link": photo.get("url", ""),
                            })
            except Exception as e:
                print(f"Ошибка Pexels ({query}): {e}")
    return results


async def fetch_inaturalist():
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.inaturalist.org/v1/observations",
                params={
                    "taxon_name": "Larus",
                    "has[]": "photos",
                    "per_page": 10,
                    "order": "desc",
                    "order_by": "created_at",
                    "quality_grade": "research",
                }
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for obs in data.get("results", []):
                        photos = obs.get("photos", [])
                        if not photos:
                            continue
                        img_url = photos[0].get("url", "").replace("square", "large")
                        if not img_url:
                            continue
                        taxon = obs.get("taxon", {})
                        species = taxon.get("preferred_common_name") or taxon.get("name") or "Seagull"
                        place = obs.get("place_guess") or "unknown location"
                        user = obs.get("user", {}).get("login", "unknown")
                        obs_id = str(obs.get("id", ""))
                        results.append({
                            "id": "inat_" + obs_id,
                            "source": "iNaturalist",
                            "title": f"{species} — {place}",
                            "author": user,
                            "author_link": f"https://www.inaturalist.org/people/{user}",
                            "media": img_url,
                            "media_type": "photo",
                            "link": f"https://www.inaturalist.org/observations/{obs_id}",
                        })
    except Exception as e:
        print(f"Ошибка iNaturalist: {e}")
    return results


async def check_feeds(app: Application):
    global SEEN_IDS, POST_STORE, POST_COUNTER
    total_new = 0

    all_posts = []
    all_posts += await fetch_unsplash()
    all_posts += await fetch_pexels()
    all_posts += await fetch_inaturalist()

    for post in all_posts:
        post_id = post["id"]
        if post_id in SEEN_IDS:
            continue
        SEEN_IDS.add(post_id)

        POST_COUNTER += 1
        post_key = str(POST_COUNTER)
        POST_STORE[post_key] = post

        caption = (
            f"🐦 <b>{post['source']}</b>\n"
            f"📝 {post['title'][:150]}\n"
            f'👤 <a href="{post["author_link"]}">{post["author"]}</a>\n\n'
            f'<a href="{post["link"]}">👉 Оригинал</a>'
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ В очередь", callback_data=f"q|{post_key}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"s|{post_key}"),
        ]])

        try:
            await app.bot.send_photo(
                ADMIN_ID, post["media"],
                caption=caption, parse_mode="HTML",
                reply_markup=keyboard
            )
            total_new += 1
        except Exception as e:
            try:
                await app.bot.send_message(
                    ADMIN_ID, caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=False
                )
                total_new += 1
            except Exception as e2:
                print(f"Ошибка отправки: {e2}")

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
        # Ищем пост сначала в памяти, потом в хранилище
        post = POST_STORE.get(post_key)

        # Если пост не в памяти (после рестарта) — сохраняем хотя бы ключ
        if not post:
            post = {"link": "", "media": "", "media_type": "photo", "source": ""}

        # Сохраняем pending в файл — переживёт рестарт
        save_pending(post)

        prompt = (
            f"✏️ <b>Напиши описание для публикации в канал</b>\n"
            f"Отправь следующим сообщением."
        )
        if post.get("link"):
            prompt += f'\n\n<a href="{post["link"]}">Оригинал ({post.get("source", "")})</a>'

        try:
            await query.edit_message_caption(prompt, parse_mode="HTML", reply_markup=None)
        except Exception:
            try:
                await query.edit_message_text(prompt, parse_mode="HTML", reply_markup=None)
            except Exception:
                pass


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    # Загружаем pending из файла — работает даже после рестарта
    pending = load_pending()

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
    clear_pending()

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
        caption += f'\n\n<a href="{post["link"]}">Источник фото</a>'

    try:
        if post.get("media"):
            await app.bot.send_photo(CHANNEL_ID, post["media"], caption=caption, parse_mode="HTML")
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
        "Каждые 2 часа я буду присылать фото чаек из Unsplash, Pexels и iNaturalist.\n"
        "Нажимай ✅, пиши описание — фото уйдёт в очередь.\n"
        "Публикации каждый час с 8:00 до 22:00 МСК.\n\n"
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
    scheduler.add_job(publish_next, "cron", hour=PUBLISH_HOURS, minute=0, args=[app])
    scheduler.start()

    print("🐦 Seagull Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
