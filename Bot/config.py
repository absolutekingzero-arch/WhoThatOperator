import json
import os
from pathlib import Path
from dotenv import load_dotenv
import discord
import unicodedata


# --- Config / env ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")

# R2 Configuration
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")

# Nếu config.py nằm trong thư mục Bot/, và Data/ nằm ở root repo:
BASE = Path(__file__).resolve().parent.parent  # repo root
# nếu Data nằm trong Bot/, dùng: BASE = Path(__file__).resolve().parent

DATA_DIR = BASE / "Data"
LOG_DIR = BASE / "logs"
ENV_PATH = BASE / ".env"

# tạo thư mục logs (nếu cần)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# helper để build path an toàn
def data_path(*parts) -> Path:
    return DATA_DIR.joinpath(*parts)

# đọc JSON an toàn với encoding UTF-8 và normalize unicode
def read_json(path):
    if isinstance(path, (str, Path)):
        p = data_path(path) if isinstance(path, str) else path
    else:
        raise TypeError("read_json expects str or Path")

    if not p.exists():
        raise FileNotFoundError(f"Missing data file: {p!s}")

    text = p.read_text(encoding="utf-8")
    return json.loads(text)

# normalize helper cho lookup strings (NFC recommended)
def normalize(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()

# --- file paths (absolute) ---
CHARACTER_EN = data_path("character_tableEN.json")
CHARACTER_CN = data_path("character_tableCN.json")
PROFESSION_MAP = data_path("profession_map.json")
CN_ONLY_MAP = data_path("cn_only_map.json")
AMIYA_PATCH = data_path("char_patch_table.json")

# --- load JSON into variables ---
EN_JSON_PATH = read_json(CHARACTER_EN)
CN_JSON_PATH = read_json(CHARACTER_CN)
PROFESSION_MAP_PATH = read_json(PROFESSION_MAP)
CN_ONLY_MAP_PATH = read_json(CN_ONLY_MAP)
AMIYA_JSON_PATH = read_json(AMIYA_PATCH)

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