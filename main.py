# main.py
import os
import sys
import asyncio
from aiohttp import web

# import bot object của bạn (điều chỉnh đường dẫn)
# from Bot.bot import bot

# ---- placeholder bot for example ----
# replace with: from Bot.bot import bot
# Here we assume `bot` is an instance of discord.Client / commands.Bot
bot = None
try:
    from Bot.bot import bot as _bot
    bot = _bot
except Exception as e:
    print("Import bot failed:", repr(e))

async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Web server running on port {port}")

async def start_bot_with_backoff(bot, token, max_attempts=6):
    delay = 10
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[bot] start attempt {attempt}")
            await bot.start(token)
            return
        except Exception as e:
            lower = str(e).lower()
            if "429" in lower or "too many requests" in lower or "rate limited" in lower or "access denied" in lower:
                print(f"[bot] rate-limited detected attempt {attempt}: {repr(e)}")
                print(f"[bot] backing off for {delay} seconds")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 600)
                continue
            else:
                print("[bot] fatal error:", repr(e))
                raise

async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN not set. Set it in Render Environment variables.")
        sys.exit(1)

    if bot is None:
        print("ERROR: bot object not imported. Check import path.")
        sys.exit(1)

    # start web so Render port scan passes (if you want web keep-alive)
    await start_web()

    try:
        await start_bot_with_backoff(bot, token)
    except Exception as e:
        print("Bot failed to start:", repr(e))
        # exit non-zero so Deploy fails visibly
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
