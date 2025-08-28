# main.py
from __future__ import annotations
import os
import sys
import asyncio
from aiohttp import web
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), 'Bot'))

load_dotenv()

# Import bot instance (tuyệt đối, không dùng relative import)
try:
    from Bot.bot import bot
    
except Exception as e:
    print("Import error:", repr(e))
    print("sys.path:", sys.path)
    raise



# Web server handler
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

async def main():
    #await start_web()
    await bot.start(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    asyncio.run(main())