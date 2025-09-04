# Bot/bot.py — replace toàn bộ file bằng nội dung này

import asyncio
import difflib
import string
from datetime import datetime
from pprint import pformat
import discord
from discord.ext import commands as discord_commands

# project imports (relative)
from . import commands as cmd_module   # module commands.py (renamed here to cmd_module)
from .config import get_intents, PREFIX, all_scores, save_scores, games, looping_channels,GameState
from .game_logic import reveal_answer 
from .utils import fuzzy_match_threshold, EN_JSON, CN_JSON

# Create bot
intents = get_intents()  # defined in config.py
_bot_prefix = PREFIX if PREFIX is not None else "!"
bot = discord_commands.Bot(command_prefix=_bot_prefix, intents=intents)

# register commands from your commands.py module (it should expose setup(bot))
try:
    if hasattr(cmd_module, "setup"):
        cmd_module.setup(bot)
        print("[bot] commands.setup called")
    else:
        print("[bot] cmd_module has no setup(bot) function — skipping setup")
except Exception as e:
    print("[bot] Exception calling commands.setup:", e)

# per-channel locks to avoid multiple winners in race conditions
_channel_locks = {}

def _normalize(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = s.translate(str.maketrans("", "", string.punctuation))
    s = " ".join(s.split())
    return s

def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()

@bot.event
async def on_ready():
    print(f"[bot] Logged in as {bot.user} (id: {bot.user.id})")
    print("[bot] Ready — waiting for commands!")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    channel = message.channel
    if channel.id not in games:
        return
    state = games[channel.id]
    if state.guessed:
        return
    guess = message.content.strip()
    if not guess:
        return

    candidates = {state.current.get("_reveal_name")}
    matched = False
    best_score = 0.0
    best_variant = None
    for cand in candidates:
        ok, score = fuzzy_match_threshold(guess, cand)
        if score > best_score:
            best_score = score
            best_variant = cand
        if ok:
            matched = True
            break

    if not matched and state.current.get("_reveal_name"):
        ok, score = fuzzy_match_threshold(guess, state.current.get("_reveal_name"))
        if ok:
            matched = True
            best_variant = state.current.get("_reveal_name")
            best_score = score

    if matched:
        state.guessed = True
        if state.timeout_task:
            state.timeout_task.cancel()
        elapsed = (datetime.utcnow() - state.started_at).total_seconds() if state.started_at else 0
        points = max(int(10 - elapsed), 1)
        guild_id = str(message.guild.id)
        uid = str(message.author.id)
        all_scores.setdefault(guild_id, {})
        all_scores[guild_id][uid] = all_scores[guild_id].get(uid, 0) + points
        save_scores()
        await channel.send(f"✅ **{message.author.display_name}** đoán đúng! (+{points} điểm) — Đáp án: **{state.current.get('_reveal_name') or state.current.get('_display_name_en') or state.current.get('_display_name_cn')}**")
        print(f"[ROUND END] Channel {channel.id} - Winner: {message.author} guessed: {guess} -> matched: {best_variant} (score={best_score:.3f})")
        await reveal_answer(channel, state.current)
        games.pop(channel.id, None)
        if channel.id in looping_channels:
            origin = state.origin_ctx or await bot.get_context(message)
            # Sửa: truyền 0 để tự động tính thời gian
            asyncio.create_task(cmd_module.schedule_next(origin, 0))  # Đây là dòng sửa
