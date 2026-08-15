import os
import asyncio
from aiohttp import web
from telethon import TelegramClient, events
from motor.motor_asyncio import AsyncIOMotorClient
from telethon.sessions import StringSession

# ==============================================================================
# --- CONFIGURATION (ENV VARIABLES OR DIRECT VALUES) ---
# ==============================================================================
API_ID = int(os.environ.get("API_ID", 20354559))
API_HASH = os.environ.get("API_HASH", "bbdf772b35141fa8b661740dddb840bf")
SESSION_STRING = os.environ.get("SESSION_STRING", "1BVtsOHQBu8L59l0nKRHptIg_t_LZSRHKR9eBX6DR6iBtZ5-AvP0GmdscRtFeKmry6aLGFQiSY-S9mIZjd8GgbSyk5ZWKqGsSKamU-LncBnFpt_gfSn95XL1jHNlgj_4XlJ9kP6znc8o65WkA2Hy9gfFOKk9NeXRVkWJ7HqUQx4naOlFaoiDtztRO2uw-8iI7v8X-wreDzSSfMNlss4xTu47VIjI4Sghu48_-MrEjxIoQBAb2q709woa4bJGzGGGAd20tyOJNmqX7U0uzDwn4xJ5ZYOo7-Th34KYlJdrP5V8cueme3jOB1ejZsrYBTT3jJd6mR9Vtk1wuq9CPXSQ2hGMwh6GFtlI=")

DESTINATION_CHANNEL = int(os.environ.get("DESTINATION_CHANNEL", -1004388839544))
PORT = int(os.environ.get("PORT", 8080))

SOURCE_CHANNELS = [
    -1003387671300,
    -1003468710048,
    -1003391810336,
    -1002504957483,
    -1003492599133,
    -1003243949222,
    -1002635680665
]

MONGODB_URL = os.environ.get("MONGODB_URL", "mongodb+srv://test:test@test.i5mjcij.mongodb.net/?appName=test")
mongo_client = AsyncIOMotorClient(MONGODB_URL)
duplicates_col = mongo_client["telegram_bot_db"]["global_seen_v2"]

# Initialize Telethon Client
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ==============================================================================
# --- HEALTH-CHECK WEB SERVER FOR RENDER ---
# ==============================================================================
async def health_check(request):
    return web.Response(text="Render Live Reposter Bot is running successfully!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 [WEB SERVER] Health-check server running on port {PORT}", flush=True)

# ==============================================================================
# --- REPOSTER EVENT HANDLER ---
# ==============================================================================
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    message = event.message
    print(f"🔥 NEW POST: Chat ID: {message.chat_id} | Msg ID: {message.id} | Media: {bool(message.media)}", flush=True)

    if not message.video and not message.document:
        print("⏩ [SKIPPED] Not a video or document.", flush=True)
        return

    media_obj = message.video or message.document
    
    fname = ""
    attributes = getattr(message.media, "document", getattr(message.media, "video", None))
    if attributes and hasattr(attributes, "attributes"):
        for attr in attributes.attributes:
            if hasattr(attr, "file_name"):
                fname = attr.file_name
                break
            
    if fname and fname.lower().endswith(('.srt', '.txt', '.rar', '.zip')):
        print(f"⏩ [SKIPPED EXTENSION] {fname}", flush=True)
        return

    # Bulletproof universal file signature
    file_uid = f"{media_obj.id}_{getattr(media_obj, 'access_hash', 0)}_{getattr(media_obj, 'size', 0)}"

    try:
        # Atomic DB insertion check to block duplicates instantly
        await duplicates_col.insert_one({"_id": file_uid, "exists": True})
    except Exception:
        print(f"🔄 [DUPLICATE BLOCKED] File already processed across channels.", flush=True)
        return

    caption = f"{fname}\n\n{message.text or ''}" if fname else (message.text or "")

    try:
        await client.send_file(
            DESTINATION_CHANNEL,
            message.media,
            caption=caption
        )
        print(f"🚀 [MIRRORED SUCCESS] Sent unique file to destination!", flush=True)
    except Exception as e:
        print(f"❌ [COPY ERROR] {e}", flush=True)

# ==============================================================================
# --- MAIN APPLICATION ENTRYPOINT ---
# ==============================================================================
async def main():
    # Start aiohttp health-check server first
    await start_web_server()

    print("🟢 [TELETHON USERBOT] Connecting...", flush=True)
    await client.start()
    print(f"🟢 [ONLINE] Listening to {len(SOURCE_CHANNELS)} channels...", flush=True)
    
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
