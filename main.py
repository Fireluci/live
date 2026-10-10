import os
import re
import copy
import bisect
import asyncio
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SlowModeWaitError, FileReferenceExpiredError
from motor.motor_asyncio import AsyncIOMotorClient
from telethon.sessions import StringSession

# ==============================================================================
# --- CONFIGURATION (ENV VARIABLES OR DIRECT VALUES) ---
# ==============================================================================
API_ID = int(os.environ.get("API_ID", 20354559))
API_HASH = os.environ.get("API_HASH", "bbdf772b35141fa8b661740dddb840bf")
SESSION_STRING = os.environ.get("SESSION_STRING", "1BVtsOH4Bu455F3vNXuvPbawa5nAnaQg3wEaFzRoY0xzSOqlkCMSnj91qUj1LJfLG6bMlPYsa485cV-VG1iCMcZHNaNmypjWo0r3kV6vN35pWyJof2tqggUXjc2pk_s2rsBYZUzREcjHWuFe2r75JvBToEUNvcjpcqGtQNf5vZgysfdEwqIbGTG-c6KYR_oAYry1ZfWzhibHpuTkpSA4WKPVuyvKHonuhIZhQE5KEuSBZDAkg254q3IzR1_1ADKkjerKBviksR5ZZVsecTx8ov2E8B-RHLfvT0FRoaH2_fv05INR1AMlVkdmM8gcsBD3tXD12sZEDv91bi-iMJLDgCfmKU2mTDgg=")

DESTINATION_CHANNEL = int(os.environ.get("DESTINATION_CHANNEL", -1004388839544))
PORT = int(os.environ.get("PORT", 8080))

# Fixed gap between consecutive sends, seconds. Serializing + throttling sends
# is what actually keeps you under Telegram's flood limits in the first place,
# instead of just reacting after you've already been flagged.
SEND_DELAY_SECONDS = float(os.environ.get("SEND_DELAY_SECONDS", 3))
MAX_FLOODWAIT_RETRIES = int(os.environ.get("MAX_FLOODWAIT_RETRIES", 10))

MONGODB_URL = os.environ.get("MONGODB_URL", "mongodb+srv://test:test@test.i5mjcij.mongodb.net/?appName=test")
mongo_client = AsyncIOMotorClient(MONGODB_URL)
duplicates_col = mongo_client["telegram_bot_db"]["global_seen_v2"]
# Durable record of "claimed but not yet successfully sent" items, so a
# process crash/restart mid-backlog can resume instead of losing them —
# the queue itself is in-memory and wouldn't survive a restart on its own.
pending_col = mongo_client["telegram_bot_db"]["pending_sends"]

# Initialize Telethon Client
client = TelegramClient(
    StringSession(SESSION_STRING), API_ID, API_HASH,
    sequential_updates=True,
)

# Single serialized queue: every send goes through one worker, one at a time.
send_queue = asyncio.Queue()

# ==============================================================================
# --- CAPTION BUILDING: filename, blank line, original caption — with real
# --- bold/link/mention formatting preserved, emoji stripped, symbols kept ---
# ==============================================================================
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # flag emoji (regional indicators)
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # geometric shapes extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows-C
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols & pictographs extended-A
    "\U0001F000-\U0001F0FF"  # mahjong / playing cards / dominoes
    "\U00002600-\U000026FF"  # misc symbols (e.g. warning sign)
    "\U00002700-\U000027FF"  # dingbats + supplemental arrows-A (e.g. ➜ ➤ ➠)
    "\U00002900-\U0000297F"  # supplemental arrows-B
    "\U00002B00-\U00002BFF"  # misc symbols & arrows (e.g. stars, ➡)
    "\U0000FE0F"             # variation selector-16 (emoji presentation)
    "\U0000200D"             # zero-width joiner (glues multi-part emoji)
    "]+",
    flags=re.UNICODE,
)

def strip_emoji(text, entities):
    """Remove emoji from text while shifting formatting entities (bold,
    links, mentions) to match. Entities are counted in UTF-16 units, so
    removal has to be mapped through that, not python character counts."""
    if not text:
        return text, list(entities)

    lengths = [2 if ord(ch) > 0xFFFF else 1 for ch in text]
    old_u16 = [0]
    for l in lengths:
        old_u16.append(old_u16[-1] + l)

    remove = [False] * len(text)
    for m in EMOJI_PATTERN.finditer(text):
        for i in range(m.start(), m.end()):
            remove[i] = True

    new_chars = []
    old_to_new = {0: 0}
    new_u16 = 0
    for i, ch in enumerate(text):
        if not remove[i]:
            new_chars.append(ch)
            new_u16 += lengths[i]
        old_to_new[old_u16[i + 1]] = new_u16

    def _map(offset):
        if offset in old_to_new:
            return old_to_new[offset]
        idx = bisect.bisect_left(old_u16, offset)
        return old_to_new[old_u16[min(idx, len(old_u16) - 1)]]

    new_entities = []
    for e in entities:
        start, end = _map(e.offset), _map(e.offset + e.length)
        if end > start:
            e = copy.copy(e)
            e.offset, e.length = start, end - start
            new_entities.append(e)

    return "".join(new_chars), new_entities

def build_caption(fname, message):
    """(filename) blank line (original caption), bold/links/mentions kept as
    real entities (not markdown text), emoji removed, other symbols kept."""
    raw = message.raw_text or ""
    entities = [copy.copy(e) for e in (message.entities or [])]

    if fname:
        prefix = f"{fname}\n\n"
        shift = len(prefix.encode("utf-16-le")) // 2  # Telegram counts UTF-16 units
        for e in entities:
            e.offset += shift
        caption = prefix + raw
    else:
        caption = raw

    caption, entities = strip_emoji(caption, entities)

    if len(caption) > 1024:  # Telegram's caption hard cap
        caption = caption[:1024]
        entities = [e for e in entities if e.offset + e.length <= 1024]

    return caption, entities

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
# --- SEND WORKER: processes the queue one item at a time, handles flood waits ---
# ==============================================================================
async def give_up(file_uid, reason):
    print(f"❌ [GIVING UP] {file_uid}: {reason}", flush=True)
    await duplicates_col.delete_one({"_id": file_uid})
    await pending_col.delete_one({"_id": file_uid})

async def send_worker():
    while True:
        file_uid, chat_id, message_id, media, caption, entities = await send_queue.get()
        attempt = 0
        while True:
            try:
                await client.send_file(
                    DESTINATION_CHANNEL, media,
                    caption=caption,
                    formatting_entities=entities,
                )
                print(f"🚀 [MIRRORED SUCCESS] Sent unique file to destination!", flush=True)
                await pending_col.delete_one({"_id": file_uid})
                break
            except (FloodWaitError, SlowModeWaitError) as e:
                attempt += 1
                wait_for = e.seconds + 2  # small buffer on top of what Telegram asks for
                if attempt > MAX_FLOODWAIT_RETRIES:
                    await give_up(file_uid, f"hit flood wait {attempt} times")
                    break
                print(f"⏳ [FLOOD WAIT] Sleeping {wait_for}s (attempt {attempt}/{MAX_FLOODWAIT_RETRIES}) before retrying {file_uid}...", flush=True)
                await asyncio.sleep(wait_for)
                # loop again and retry the same item — do not drop it
            except FileReferenceExpiredError:
                # The captured reference went stale (long backlog). Only recoverable
                # if the source message still exists — re-fetch it for a fresh one.
                print(f"🔁 [STALE REFERENCE] {file_uid} — re-fetching from source chat {chat_id}...", flush=True)
                try:
                    fresh = await client.get_messages(chat_id, ids=message_id)
                except Exception as e:
                    fresh = None
                    print(f"⚠️ [REFETCH FAILED] {file_uid}: {e}", flush=True)
                if fresh and fresh.media:
                    media = fresh.media
                    print(f"✅ [REFRESHED] {file_uid} — retrying with fresh reference.", flush=True)
                    # loop again with the refreshed media
                else:
                    await give_up(file_uid, "source message deleted/unavailable — cannot refresh expired reference, no bytes ever downloaded so nothing to fall back on")
                    break
            except Exception as e:
                await give_up(file_uid, str(e))
                break

        send_queue.task_done()
        # Proactive throttle: always pause between sends, success or not,
        # so a burst of source posts doesn't hammer Telegram back-to-back.
        await asyncio.sleep(SEND_DELAY_SECONDS)

# ==============================================================================
# --- REPOSTER EVENT HANDLER ---
# ==============================================================================
@client.on(events.NewMessage())
async def handler(event):
    # Mirror from every chat type (channels, groups, private chats, bots),
    # except the destination itself.
    if event.chat_id == DESTINATION_CHANNEL:
        return

    message = event.message
    print(f"🔥 NEW POST: Chat ID: {message.chat_id} | Msg ID: {message.id} | Media: {bool(message.media)}", flush=True)

    if not message.video and not message.document:
        print("⏩ [SKIPPED] Not a video or document.", flush=True)
        return
    # Skip ALL stickers (static, animated, and video)
    if message.sticker:
        print("⏩ [SKIPPED] Sticker.", flush=True)
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

    # File signature based on the stable, content-based document id only.
    file_uid = f"{media_obj.id}"

    try:
        # Atomic DB insertion check to claim this file instantly — prevents
        # two near-simultaneous posts of the same file both getting queued.
        await duplicates_col.insert_one({"_id": file_uid, "exists": True})
    except Exception:
        print(f"🔄 [DUPLICATE BLOCKED] File already processed across channels.", flush=True)
        return

    caption, entities = build_caption(fname, message)

    # Durable record so a crash/restart can resume this item (best-effort —
    # only works if the source message still exists when we come back).
    # Just fname is stored; caption+entities are rebuilt fresh on recovery.
    await pending_col.insert_one({
        "_id": file_uid,
        "chat_id": event.chat_id,
        "message_id": message.id,
        "fname": fname,
    })

    # Hand off to the serialized send queue instead of sending directly —
    # the worker paces every send and handles flood waits with retries.
    await send_queue.put((file_uid, event.chat_id, message.id, message.media, caption, entities))
    print(f"📥 [QUEUED] {file_uid} (queue size: {send_queue.qsize()})", flush=True)

# ==============================================================================
# --- MAIN APPLICATION ENTRYPOINT ---
# ==============================================================================
async def main():
    # Start aiohttp health-check server first
    await start_web_server()

    # Start the serialized send worker in the background
    asyncio.create_task(send_worker())

    print("🟢 [TELETHON USERBOT] Connecting...", flush=True)
    await client.start()
    dialogs = await client.get_dialogs()
    joined = sum(1 for d in dialogs if d.is_channel or d.is_group)
    print(f"🟢 [ONLINE] Listening to {joined} joined channels/groups...", flush=True)

    # Recover anything left pending from a previous crash/restart.
    recovered = 0
    async for doc in pending_col.find({}):
        file_uid = doc["_id"]
        try:
            fresh = await client.get_messages(doc["chat_id"], ids=doc["message_id"])
        except Exception as e:
            fresh = None
            print(f"⚠️ [RECOVERY REFETCH FAILED] {file_uid}: {e}", flush=True)
        if fresh and fresh.media:
            caption, entities = build_caption(doc.get("fname", ""), fresh)
            await send_queue.put((file_uid, doc["chat_id"], doc["message_id"], fresh.media, caption, entities))
            recovered += 1
        else:
            print(f"❌ [UNRECOVERABLE] {file_uid} — source message gone, dropping leftover claim.", flush=True)
            await duplicates_col.delete_one({"_id": file_uid})
            await pending_col.delete_one({"_id": file_uid})
    if recovered:
        print(f"♻️ [RECOVERED] Requeued {recovered} pending send(s) from before restart.", flush=True)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
