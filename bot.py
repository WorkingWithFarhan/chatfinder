# bot.py (final combined Chat Finder + Music)
import re
import time
import io
import sqlite3
import asyncio
from datetime import datetime, timezone
import os

# dotenv (optional local testing)
from dotenv import load_dotenv

load_dotenv()

import discord
from discord.ext import commands
from discord import FFmpegPCMAudio

# ---------- Load token ----------
TOKEN = os.getenv("DISCORD_TOKEN")

# ---------- timezone ----------
TIMEZONE_NAME = "Asia/Kolkata"
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    LOCAL_TZ = timezone.utc

# ---------- Try to load Opus (best-effort) ----------
if not discord.opus.is_loaded():
    tried = []
    for libname in ("libopus.so.0", "libopus.so", "libopus.dylib", "opus.dll",
                    "libopus-0.dll"):
        try:
            discord.opus.load_opus(libname)
            print(f"✅ Loaded opus from: {libname}")
            break
        except Exception as e:
            tried.append((libname, str(e)))
    if not discord.opus.is_loaded():
        print("⚠️ opus not loaded. Tried:", tried)
        print(
            "If voice fails, install system lib 'libopus' (apt) or run on host with opus available."
        )

# ---------- bot setup ----------
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Database ----------
conn = sqlite3.connect("messages.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author TEXT,
    content TEXT,
    created_at TEXT,
    channel TEXT,
    attachments TEXT
)
""")
conn.commit()

# ---------- helpers ----------
DATE_REGEX = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")


def parse_date(token: str):
    m = DATE_REGEX.match(token)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return datetime(y, mth, d).date()
    except ValueError:
        return None


def detect_channel(ctx, tokens):
    for tok in tokens[:]:
        if tok.startswith("<#") and tok.endswith(">"):
            try:
                cid = int(tok.strip("<#>"))
                ch = ctx.guild.get_channel(cid)
                if ch:
                    tokens.remove(tok)
                    return ch
            except Exception:
                pass
    return ctx.channel


# ---------- Auto-logging ----------
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return
    try:
        cursor.execute(
            "INSERT INTO messages (author, content, created_at, channel, attachments) VALUES (?, ?, ?, ?, ?)",
            (
                str(message.author),
                message.content,
                message.created_at.isoformat(),
                str(message.channel),
                ",".join([att.url for att in message.attachments])
                if message.attachments else None,
            ),
        )
        conn.commit()
    except Exception as e:
        print("DB error:", e)
    await bot.process_commands(message)


# ---------- Basic ----------
@bot.command()
async def hello(ctx):
    await ctx.send(f"Hello <@{ctx.author.id}>! 👋")


@bot.command()
async def ping(ctx):
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! {latency_ms}ms")


@bot.command()
async def echo(ctx, *, text: str):
    await ctx.send(text)


@bot.command()
async def info(ctx):
    await ctx.send("ℹ️ I am alive and running!")


# ---------- Index ----------
@bot.command()
async def index(ctx, limit: int = 1000):
    count = 0
    async for message in ctx.channel.history(limit=limit):
        if message.author.bot:
            continue
        cursor.execute(
            "INSERT INTO messages (author, content, created_at, channel, attachments) VALUES (?, ?, ?, ?, ?)",
            (
                str(message.author),
                message.content,
                message.created_at.isoformat(),
                str(message.channel),
                ",".join([att.url for att in message.attachments])
                if message.attachments else None,
            ),
        )
        count += 1
    conn.commit()
    await ctx.send(f"✅ Indexed {count} messages into the database.")


# ---------- Stats ----------
@bot.command()
async def stats(ctx):
    cursor.execute("SELECT COUNT(*) FROM messages")
    total_messages = cursor.fetchone()[0]
    cursor.execute(
        "SELECT author, COUNT(*) FROM messages GROUP BY author ORDER BY COUNT(*) DESC LIMIT 5"
    )
    top_authors = cursor.fetchall()
    stats_msg = f"📊 **Chat Stats**\nTotal Messages: {total_messages}\n\n**Top 5 Active Users:**\n"
    for author, count in top_authors:
        stats_msg += f"- {author}: {count}\n"
    await ctx.send(stats_msg)


# ---------- Find ----------
@bot.command()
async def find(ctx, *args):
    if not args:
        await ctx.send(
            "⚠️ Usage: `!find <keyword/@user/#channel/DD-MM-YYYY> [limit]`")
        return
    tokens = list(args)
    limit = 1000
    if tokens and tokens[-1].isdigit():
        limit = int(tokens.pop())
    search_channel = detect_channel(ctx, tokens)
    user_filter = None
    for tok in tokens[:]:
        if tok.startswith("<@") and tok.endswith(">"):
            try:
                uid = int(tok.strip("<@!>"))
                member = ctx.guild.get_member(uid)
                if member:
                    user_filter = member
                    tokens.remove(tok)
                    break
            except Exception:
                pass
    date_filter = None
    for i, tok in enumerate(tokens[:]):
        dt = parse_date(tok)
        if dt:
            date_filter = dt
            tokens.pop(i)
            break
    keyword = " ".join(tokens).strip() if tokens else None
    if keyword == "":
        keyword = None
    start_time = time.time()
    results = []
    async for message in search_channel.history(limit=limit):
        msg_local_dt = message.created_at.astimezone(LOCAL_TZ)
        msg_date = msg_local_dt.date()
        if date_filter and msg_date != date_filter:
            continue
        if user_filter and message.author != user_filter:
            continue
        if keyword and keyword.lower() not in (message.content or "").lower():
            continue
        stamp = msg_local_dt.strftime("%d-%m-%Y %H:%M")
        author_name = getattr(message.author, "display_name",
                              str(message.author))
        preview = f"[{stamp}] {author_name}: {message.content}"
        results.append(preview)
    elapsed = time.time() - start_time
    latency_ms = round(bot.latency * 1000)
    if not results:
        await ctx.send(
            f"❌ No messages found.\n⏱️ {elapsed:.2f}s | 🏓 {latency_ms}ms")
        return
    MAX_INLINE = 25
    if date_filter or len(results) > MAX_INLINE:
        output = "\n".join(results)
        file_buffer = io.BytesIO(output.encode("utf-8"))
        file_buffer.seek(0)
        name_part = date_filter.strftime("%d-%m-%Y") if date_filter else (
            keyword or "results")
        filename = f"chatlog_{name_part}.txt"
        await ctx.send(content=(
            f"✅ Found **{len(results)}** messages in {search_channel.mention}.\n"
            f"⏱️ {elapsed:.2f}s | 🏓 {latency_ms}ms"),
                       file=discord.File(fp=file_buffer, filename=filename))
    else:
        msg_text = "\n".join(results)
        msg_text += f"\n\n⏱️ {elapsed:.2f}s | 🏓 {latency_ms}ms"
        await ctx.send(msg_text)


# ---------- Attachments ----------
async def fetch_attachments(ctx, date_str, args, filetypes, label):
    date_filter = parse_date(date_str)
    if not date_filter:
        await ctx.send("⚠️ Invalid date format. Use DD-MM-YYYY")
        return
    search_channel = detect_channel(ctx, list(args))
    results = []
    async for message in search_channel.history(limit=5000):
        msg_local_dt = message.created_at.astimezone(LOCAL_TZ)
        msg_date = msg_local_dt.date()
        if msg_date != date_filter:
            continue
        for att in message.attachments:
            if not filetypes or att.filename.lower().endswith(filetypes):
                stamp = msg_local_dt.strftime("%d-%m-%Y %H:%M")
                author_name = getattr(message.author, "display_name",
                                      str(message.author))
                preview = f"[{stamp}] {author_name}: {att.url}"
                results.append(preview)
    if not results:
        await ctx.send(
            f"❌ No {label} found on {date_filter} in {search_channel.mention}")
        return
    output = "\n".join(results)
    file_buffer = io.BytesIO(output.encode("utf-8"))
    file_buffer.seek(0)
    filename = f"{label}_{date_filter}.txt"
    await ctx.send(
        content=
        f"✅ Found **{len(results)}** {label} on {date_filter} in {search_channel.mention}.",
        file=discord.File(fp=file_buffer, filename=filename))


@bot.command()
async def files(ctx, date_str: str, *args):
    await fetch_attachments(ctx, date_str, args, None, "files")


@bot.command()
async def videos(ctx, date_str: str, *args):
    await fetch_attachments(ctx, date_str, args,
                            (".mp4", ".mov", ".avi", ".mkv", ".webm"),
                            "videos")


@bot.command()
async def images(ctx, date_str: str, *args):
    await fetch_attachments(ctx, date_str, args,
                            (".png", ".jpg", ".jpeg", ".gif", ".webp"),
                            "images")


# ---------- Summary ----------
@bot.command()
async def summary(ctx, date_str: str, *args):
    date_filter = parse_date(date_str)
    if not date_filter:
        await ctx.send("⚠️ Invalid date format! Use DD-MM-YYYY.")
        return
    channel_filter = None
    if args:
        ch = detect_channel(ctx, list(args))
        if ch:
            channel_filter = str(ch)
    if channel_filter:
        cursor.execute(
            "SELECT content FROM messages WHERE created_at LIKE ? AND channel = ?",
            (f"{date_filter}%", channel_filter))
    else:
        cursor.execute("SELECT content FROM messages WHERE created_at LIKE ?",
                       (f"{date_filter}%", ))
    rows = cursor.fetchall()
    if not rows:
        await ctx.send(
            f"❌ No messages found for {date_filter} {f'in {channel_filter}' if channel_filter else ''}."
        )
        return
    messages = " ".join(row[0] for row in rows if row[0])
    categories = {
        "Games": ["game", "play", "minecraft", "pubg", "fortnite", "gamer"],
        "Music": ["song", "music", "lyrics", "beats"],
        "Movies/Series": ["movie", "series", "netflix", "film"],
        "Memes/Fun": ["meme", "funny", "joke", "haha"],
        "Tech": ["tech", "computer", "app", "website"]
    }
    topic_count = {}
    for topic, keywords in categories.items():
        count = sum(messages.lower().count(word) for word in keywords)
        if count > 0:
            topic_count[topic] = count
    if not topic_count:
        await ctx.send(f"📅 No major topics on {date_filter}.")
        return
    sorted_topics = sorted(topic_count.items(),
                           key=lambda x: x[1],
                           reverse=True)
    summary_text = f"📅 Summary for {date_filter}:\n"
    for topic, count in sorted_topics[:3]:
        summary_text += f"- {topic} ({count} mentions)\n"
    await ctx.send(summary_text)


# ---------- Summary PDF ----------
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet


@bot.command()
async def summarypdf(ctx, date_str: str, *args):
    date_filter = parse_date(date_str)
    if not date_filter:
        await ctx.send("⚠️ Invalid date format! Use DD-MM-YYYY.")
        return
    channel_filter = None
    if args:
        ch = detect_channel(ctx, list(args))
        if ch:
            channel_filter = str(ch)
    if channel_filter:
        cursor.execute(
            "SELECT author, content, created_at FROM messages WHERE created_at LIKE ? AND channel = ?",
            (f"{date_filter}%", channel_filter))
    else:
        cursor.execute(
            "SELECT author, content, created_at FROM messages WHERE created_at LIKE ?",
            (f"{date_filter}%", ))
    rows = cursor.fetchall()
    if not rows:
        await ctx.send(f"❌ No messages found for {date_filter}.")
        return
    file_name = f"chat_summary_{date_filter}.pdf"
    file_buffer = io.BytesIO()
    doc = SimpleDocTemplate(file_buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    Story = []
    Story.append(Paragraph(f"Chat Summary for {date_filter}", styles['Title']))
    Story.append(Spacer(1, 12))
    for author, content, created_at in rows:
        timestamp = created_at.split(
            "T")[1][:5] if "T" in created_at else created_at
        Story.append(
            Paragraph(f"<b>{author}</b> [{timestamp}]:", styles['Heading4']))
        Story.append(
            Paragraph(content if content else "[No content]",
                      styles['BodyText']))
        Story.append(Spacer(1, 12))
    doc.build(Story)
    file_buffer.seek(0)
    await ctx.send(content=f"✅ Chat summary PDF for {date_filter}",
                   file=discord.File(fp=file_buffer, filename=file_name))


# ---------- MUSIC SYSTEM ----------
from discord.utils import get
import yt_dlp

QUEUES = {}
ydl_opts_general = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True
}


async def _play_next_in_guild(guild_id, text_channel):
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    q = QUEUES.get(guild_id)
    if not q or len(q) == 0:
        try:
            await text_channel.send("📭 Queue finished.")
        except Exception:
            pass
        return
    item = q.pop(0)
    info = item['info']
    stream_url = info.get("url")
    title = info.get("title", "Unknown")
    vc = guild.voice_client
    if not vc:
        try:
            await text_channel.send("⚠️ I am not connected to VC.")
        except Exception:
            pass
        return

    def after_play(err):
        if err:
            print("Player error:", err)
        fut = asyncio.run_coroutine_threadsafe(
            _play_next_in_guild(guild_id, text_channel), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print("Error scheduling next:", e)

    try:
        source = FFmpegPCMAudio(stream_url, executable="ffmpeg")
        vc.play(source, after=after_play)
        await text_channel.send(
            f"▶️ Now playing: **{title}** (requested by {item.get('requester','unknown')})"
        )
    except Exception as e:
        await text_channel.send("⚠️ Failed to play song: " + str(e))
        fut = asyncio.run_coroutine_threadsafe(
            _play_next_in_guild(guild_id, text_channel), bot.loop)
        try:
            fut.result()
        except Exception:
            pass


def _extract_info(query):
    with yt_dlp.YoutubeDL(ydl_opts_general) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        stream_url = info.get("url")
        if not stream_url:
            formats = info.get("formats", [])
            if formats:
                stream_url = formats[-1].get("url")
        return {"url": stream_url, "title": info.get("title", "Unknown")}


@bot.command()
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client is None:
            await channel.connect()
        else:
            await ctx.voice_client.move_to(channel)
        await ctx.send(f"✅ Joined {channel.name}")
    else:
        await ctx.send("⚠️ You need to join a voice channel first!")


@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Left the voice channel.")
    else:
        await ctx.send("⚠️ I'm not in a voice channel!")


@bot.command()
async def play(ctx, *, query: str):
    if not query:
        await ctx.send("⚠️ Usage: `!play <query or <url>>`")
        return
    if query.startswith("<") and query.endswith(">"):
        query = query[1:-1].strip()
    if re.match(r"https?://", query):
        ytdl_query = query
    else:
        ytdl_query = f"ytsearch:{query}"
    await ctx.trigger_typing()
    try:
        info = _extract_info(ytdl_query)
        if not info or not info.get("url"):
            await ctx.send("⚠️ Couldn't find audio for that query.")
            return
    except Exception as e:
        await ctx.send("⚠️ Error fetching info: " + str(e))
        return
    guild_id = ctx.guild.id
    if guild_id not in QUEUES:
        QUEUES[guild_id] = []
    QUEUES[guild_id].append({"info": info, "requester": str(ctx.author)})
    if not ctx.voice_client:
        if ctx.author.voice:
            try:
                await ctx.author.voice.channel.connect()
            except Exception as e:
                await ctx.send("⚠️ Failed to connect to VC: " + str(e))
                return
        else:
            await ctx.send("⚠️ Join a voice channel first.")
            return
    vc = ctx.voice_client
    if not vc.is_playing() and not vc.is_paused():
        await _play_next_in_guild(guild_id, ctx.channel)
    else:
        await ctx.send(f"➕ Added to queue: **{info.get('title')}**")


@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")
    else:
        await ctx.send("⚠️ Nothing is playing.")


@bot.command()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused.")
    else:
        await ctx.send("⚠️ Nothing is playing.")


@bot.command()
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("⚠️ Nothing is paused.")


@bot.command(name="queue")
async def command_queue(ctx):
    q = QUEUES.get(ctx.guild.id)
    if not q or len(q) == 0:
        await ctx.send("📭 Queue is empty.")
        return
    msg = "📜 Queue:\n"
    for i, item in enumerate(q, start=1):
        title = item.get("info", {}).get("title", "Unknown")
        requester = item.get("requester", "unknown")
        msg += f"{i}. {title} (requested by {requester})\n"
    if len(msg) > 1900:
        buf = io.BytesIO(msg.encode("utf-8"))
        buf.seek(0)
        await ctx.send(file=discord.File(fp=buf, filename="queue.txt"))
    else:
        await ctx.send(msg)


# ---------- Help ----------
@bot.command()
async def helpme(ctx):
    help_text = """
📜 **Chat Finder**
!index, !find, !stats
!files, !videos, !images
!summary <date> [#channel]
!summarypdf <date> [#channel]

🎵 **Music**
!join, !leave
!play <query or <url>>
!skip, !pause, !resume
!queue
"""
    await ctx.send(help_text)


# ---------- Ready ----------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


# ---------- Run ----------
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN not set in environment.")
    else:
        bot.run("##")