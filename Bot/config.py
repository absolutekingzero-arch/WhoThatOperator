import json
import os
from pathlib import Path
from dotenv import load_dotenv
import discord

# --- Config / env ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")

# R2 Configuration
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")


class GameState:
    def __init__(self, channel: discord.TextChannel, origin_ctx=None):
        self.channel = channel
        self.current = None
        self.started_at = None
        self.timeout_task = None
        self.guessed = False
        self.origin_ctx = origin_ctx
        self.hint = None
        self.hint_shown = False

# Loop settings
try:
    LOOP_DELAY = int(os.getenv("LOOP_DELAY", "5"))
except Exception:
    LOOP_DELAY = 5

if not TOKEN:
    print("Warning: DISCORD_TOKEN not set in environment. The bot will not be able to login without it.")

# --- Paths ---
SCORES_FILE = Path("scores.json")

# EN/CN JSON paths
EN_JSON_PATH = Path("data/character_tableEN.json")
CN_JSON_PATH = Path("data/character_tableCN.json")

# Map files
PROFESSION_MAP_PATH = Path("data/profession_map.json")
CN_ONLY_MAP_PATH = Path("data/cn_only_map.json")
AMIYA_JSON_PATH = Path("data/char_patch_table.json")

# Intents configuration (chỉ định nghĩa intents, không tạo bot)
def get_intents():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True
    return intents

"""Hệ thống điểm"""
# --- Scores persistence ---
if SCORES_FILE.exists():
    try:
        with SCORES_FILE.open("r", encoding="utf-8") as f:
            all_scores = json.load(f)
    except Exception:
        all_scores = {}
else:
    all_scores = {}

def save_scores():
    try:
        with SCORES_FILE.open("w", encoding="utf-8") as f:
            json.dump(all_scores, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Failed to save scores:", e)

def is_r2_enabled():
    """Check if R2 is configured"""
    return all([
        R2_ACCESS_KEY_ID,
        R2_SECRET_ACCESS_KEY,
        R2_BUCKET_NAME,
        R2_ENDPOINT_URL
    ])

# --- Shared game state variables ---
games = {}
looping_channels = set()
looping_settings = {}
scheduled_tasks = {}

# Import từ matching
# --- Robust fuzzy matching logic (token-aware) ---
EXACT_LEN = 4
FUZZY_MIN_LEN = 5
FUZZY_THRESHOLD = 0.95

# Stopwords
STOPWORDS = {
    "the","a","an","of","and","in","on","at","by","for","to","from","with",
    "is","was","are","were","this","that","it","its","her","his","new","ex"
}