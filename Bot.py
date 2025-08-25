# bot.py - WhoThatOperator (patched)

"""Cấu hình và Khởi tạo"""
from __future__ import annotations
import os
import json
import random
import asyncio
import unicodedata
import re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from difflib import SequenceMatcher
import unicodedata
import discord
from discord.ext import commands
# --- Import thêm các thư viện cần thiết ---
import boto3
from botocore.config import Config
import tempfile
from aiohttp import web
import threading

async def handle(request):
    return web.Response(text="Bot is running!")

def run_web():
    app = web.Application()
    app.router.add_get("/", handle)
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, port=port)

# --- Config / env ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")

if not TOKEN:
    print("Warning: DISCORD_TOKEN not set in environment. The bot will not be able to login without it.")

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# --- Paths ---
SCORES_FILE = Path("scores.json")

# EN/CN JSON paths
EN_JSON_PATH = Path("character_tableEN.json")
CN_JSON_PATH = Path("character_tableCN.json")

# Map files
PROFESSION_MAP_PATH = Path("profession_map.json")
CN_ONLY_MAP_PATH = Path("cn_only_map.json")

# --- Robust fuzzy matching logic (token-aware) ---
EXACT_LEN = 4
FUZZY_MIN_LEN = 5
FUZZY_THRESHOLD = 0.95

"""R2 enabled"""
# --- Hàm kiểm tra R2 enabled ---
def is_r2_enabled():
   return all([
        os.getenv("R2_ACCESS_KEY_ID"),
        os.getenv("R2_SECRET_ACCESS_KEY"),
        os.getenv("R2_BUCKET_NAME"),
        os.getenv("R2_ENDPOINT_URL")
    ])

# --- Hàm tải metadata từ R2 ---

def load_characters_from_r2(access_key_id, secret_access_key, bucket_name, endpoint_url, base_dir: str = None):
    s3 = boto3.client('s3',
                      endpoint_url=endpoint_url,
                      aws_access_key_id=access_key_id,
                      aws_secret_access_key=secret_access_key,
                      config=Config(signature_version='s3v4'))
    chars = {}
    
    print("=== R2 DEBUG ===")
    print(f"Bucket: {bucket_name}")
    print(f"Prefixes: ['Char/', 'Skin/']")

    # Sử dụng cùng helper functions với local version
    def ensure_ent(key):
        if key not in chars:
            # Lấy tên từ JSON nếu có, nếu không thì từ key
            display_en, display_cn = get_display_names(key, {})
            display_name = display_en or display_cn or key.replace('char_', '').replace('_', ' ').title()
            
            chars[key] = {
                "key": key,
                "name": display_name,
                "pair_map": {},
                "variants": [],
                "all_fulls": [],
                "all_silhouettes": []
            }
        return chars[key]

    def add_to_pair(ent, pair_id, kind, object_key):
        if pair_id not in ent["pair_map"]:
            ent["pair_map"][pair_id] = {"pair_id": pair_id, "fulls": [], "silhouettes": []}
        
        bucket = ent["pair_map"][pair_id]
        if kind == "sil" and object_key not in bucket["silhouettes"]:
            bucket["silhouettes"].append(object_key)
        elif kind == "full" and object_key not in bucket["fulls"]:
            bucket["fulls"].append(object_key)

    # List objects từ R2 bucket
    prefixes = ["images/Char/", "images/Skin/"]
    total_objects = 0
    
    for prefix in prefixes:
        print(f"Scanning R2 prefix: {prefix}")
        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

        # iterate pages correctly
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj.get('Key')
                if not key or key.endswith('/'):
                    continue

                total_objects += 1

                stem = Path(key).stem
                # call the top-level helper (defined elsewhere in file)
                try:
                    base_key, variant_info = extract_key_and_variant(stem)
                except Exception:
                    # fallback: use stem as-is
                    base_key, variant_info = stem, "unknown"

                # Canonicalize key để đảm bảo khớp với JSON
                canonical_key = canonicalize_key(base_key)

                # Sử dụng effective key
                effective_key = canonical_key
                if not ((EN_JSON and effective_key in EN_JSON) or (CN_JSON and effective_key in CN_JSON)):
                    effective_key = base_key

                # Xác định loại ảnh
                is_silhouette = '[alpha]' in key.lower()
                kind = "sil" if is_silhouette else "full"

                # Sử dụng variant_info làm pair_id (đã được làm sạch)
                pair_id = variant_info or "default"

                # Thêm vào character entry
                ent = ensure_ent(effective_key)
                add_to_pair(ent, pair_id, kind, key)

    print(f"Total objects found: {total_objects}")
    print("=================")

    # Xây dựng kết quả (giữ nguyên)
    results = []
    for k, ent in chars.items():
        variants = []
        all_fulls = []
        all_sils = []
        for pid, bucket in ent.get("pair_map", {}).items():
            if bucket.get("fulls") or bucket.get("silhouettes"):
                variants.append({
                    "pair_id": pid,
                    "skin_name": pid,
                    "fulls": list(bucket.get("fulls", [])),
                    "silhouettes": list(bucket.get("silhouettes", []))
                })
            for f in bucket.get("fulls", []):
                if f not in all_fulls:
                    all_fulls.append(f)
            for s in bucket.get("silhouettes", []):
                if s not in all_sils:
                    all_sils.append(s)
        ent["variants"] = variants
        ent["all_fulls"] = all_fulls
        ent["all_silhouettes"] = all_sils
        
        # Cập nhật display name từ JSON
        display_en, display_cn = get_display_names(k, ent)
        if display_en:
            ent["name"] = display_en
        elif display_cn:
            ent["name"] = display_cn
        
        results.append(ent)

    print(f"Loaded {len(results)} characters from R2")
    return [v for v in results if v.get("all_fulls") or v.get("all_silhouettes")]

# --- Hàm tải ảnh từ R2 ---
def download_r2_object(object_key: str) -> str:
    s3 = boto3.client('s3',
        endpoint_url=os.getenv("R2_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        config=Config(signature_version='s3v4')
    )
    
    fd, path = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    
    try:
        s3.download_file(os.getenv("R2_BUCKET_NAME"), object_key, path)
        return path
    except Exception as e:
        os.unlink(path)
        raise e
    
"""Tải dữ liệu JSON"""
# --- Load JSON data ---
def safe_load_json(p: Path):
    try:
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Failed to load JSON {p}: {e}")
    return {}

EN_JSON = safe_load_json(EN_JSON_PATH)
CN_JSON = safe_load_json(CN_JSON_PATH)

# Load map files
def load_map_file(path: Path):
    data = safe_load_json(path)
    if isinstance(data, dict) and "map" in data:
        return data["map"]
    return data if isinstance(data, dict) else {}

PROFESSION_MAP = load_map_file(PROFESSION_MAP_PATH)
CN_ONLY_MAP = load_map_file(CN_ONLY_MAP_PATH)

print(f"Loaded: EN_JSON={len(EN_JSON)} entries, CN_JSON={len(CN_JSON)} entries")
print(f"Loaded: PROFESSION_MAP={len(PROFESSION_MAP)} entries, CN_ONLY_MAP={len(CN_ONLY_MAP)} entries")

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

"""Điều khiển vòng lặp """
# --- Looping control ---
looping_channels = set()  # channel.id set that should auto-start new rounds
looping_settings = {}   # channel.id -> loop delay (seconds)
# lưu task schedule để có thể hủy khi stop
scheduled_tasks = {}  # channel.id -> asyncio.Task
# --- Loop delay (seconds) ---
try:
    LOOP_DELAY = int(os.getenv("LOOP_DELAY", "5"))
except Exception:
    LOOP_DELAY = 5

async def schedule_next(origin_ctx, seconds=0):
    """Sleep channel-specific LOOP_DELAY then start next round if still active."""
    cid = origin_ctx.channel.id
    try:
        delay = looping_settings.get(cid, LOOP_DELAY)
        if delay > 0:
            await asyncio.sleep(delay)
        # chỉ chạy nếu kênh vẫn đang bật loop
        if cid in looping_channels:
            await start_game(origin_ctx, seconds)
    except asyncio.CancelledError:
        # task bị hủy bởi stop()
        pass
    except Exception as e:
        print("Failed to schedule next round:", e)
    finally:
        scheduled_tasks.pop(cid, None)  # dọn khi xong

"""Xử lý ngôn ngữ"""
# --- Matching helpers ---
STOPWORDS = {
    "the","a","an","of","and","in","on","at","by","for","to","from","with",
    "is","was","are","were","this","that","it","its","her","his","new","ex"
}

def normalize_for_match(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (s or ""))

def tokenize_for_match(s: str, min_len:int=1):
    n = normalize_for_match(s)
    if not n:
        return []
    if is_cjk(n):
        if min_len <= 1:
            return [ch for ch in n if ch and ch not in STOPWORDS]
        return [n] if len(n) >= min_len else []
    toks = [t for t in n.split() if t and t not in STOPWORDS and len(t) >= min_len]
    return toks

# --- Name helpers with map support ---
def get_display_names(key: str, char: dict) -> tuple:
    """
    Tìm tên character với key đầy đủ (bao gồm cả số variant).
    Nếu thiếu tên EN, cố gắng lấy từ CN_ONLY_MAP mà không thay đổi display_cn.
    Trả về (display_en, display_cn)
    """
    # Thử nhiều biến thể key
    key_variants = [key]
    parts = key.split('_')
    if len(parts) >= 4:
        base_key = "_".join(parts[:3])
        key_variants.append(base_key)

    # 1) Tìm trong EN_JSON
    display_en = None
    for variant in key_variants:
        if EN_JSON and variant in EN_JSON and isinstance(EN_JSON[variant], dict):
            display_en = EN_JSON[variant].get("name") or EN_JSON[variant].get("displayName") or EN_JSON[variant].get("english_name")
            if display_en:
                break

    # 2) Tìm trong CN_JSON
    display_cn = None
    for variant in key_variants:
        if CN_JSON and variant in CN_JSON and isinstance(CN_JSON[variant], dict):
            display_cn = CN_JSON[variant].get("name") or CN_JSON[variant].get("label")
            if display_cn:
                break

    # 3) Fallback từ metadata ảnh (không ưu tiên override CN)
    if not display_en and char.get("name"):
        display_en = char.get("name")
    if not display_cn and char.get("name"):
        display_cn = char.get("name")

    # 4) Nếu vẫn thiếu display_en -> thử lookup trong CN_ONLY_MAP
    #    Hỗ trợ nhiều dạng nội dung trong CN_ONLY_MAP: str hoặc dict với key 'en'/'english'/'name'
    if not display_en and isinstance(CN_ONLY_MAP, dict):
        # 4a: thử lookup theo variant key trực tiếp
        found = None
        for variant in key_variants:
            if variant in CN_ONLY_MAP:
                found = CN_ONLY_MAP[variant]
                break

        # 4b: nếu chưa, thử lookup theo display_cn (nếu đã lấy được) — nhiều map dùng tên CN làm khóa
        if not found and display_cn:
            # dùng exact match theo chuỗi display_cn
            if display_cn in CN_ONLY_MAP:
                found = CN_ONLY_MAP[display_cn]

        # 4c: nếu tìm được, rút tên EN ra
        if found:
            if isinstance(found, dict):
                display_en = found.get("en") or found.get("english") or found.get("name") or display_en
            elif isinstance(found, str):
                display_en = found

    return display_en, display_cn


def canonicalize_key(key: str):
    """Tạm thời không canonicalize - giữ nguyên key"""
    return key
"""
def canonicalize_key(key: str):
    Canonicalize a character key WITHOUT removing trailing numeric tokens.
    try:
        if not key or not isinstance(key, str):
            return key
        
        k = key.lower()
        
        # First, check exact match
        if isinstance(EN_JSON, dict) and k in EN_JSON:
            return k
        if isinstance(CN_JSON, dict) and k in CN_JSON:
            return k
        
        # If not found, try alternative patterns but DON'T remove trailing numbers
        # Check if it's a skin variant (char_x_y_z)
        parts = k.split("_")
        if len(parts) >= 3:
            # Try the base key (char_x_y)
            base_key = "_".join(parts[:3])
            if (isinstance(EN_JSON, dict) and base_key in EN_JSON) or (isinstance(CN_JSON, dict) and base_key in CN_JSON):
                return base_key
            
            # Try with skin number (char_x_y_z where z is numeric)
            if len(parts) >= 4 and parts[3].isdigit():
                skin_key = "_".join(parts[:4])
                if (isinstance(EN_JSON, dict) and skin_key in EN_JSON) or (isinstance(CN_JSON, dict) and skin_key in CN_JSON):
                    return skin_key
        
        return k  # Return original if nothing found
    except Exception:
        return key
"""

""" Xử lý gợi ý """
# --- Hint ---
def map_profession_hint(raw_prof: str) -> str:
    if not raw_prof:
        return raw_prof
    s = str(raw_prof).strip().upper()
    return PROFESSION_MAP.get(s, raw_prof)

def generate_hint_for_char(char: dict) -> str:
    if not isinstance(char, dict):
        return ""

    key = char.get("key")
    entry = {}
    if key:
        entry = EN_JSON.get(key) or CN_JSON.get(key) or {}

    candidates = []

    p = entry.get("profession") or char.get("profession")
    if p:
        candidates.append(p)

    sp = entry.get("subProfessionId") or entry.get("subProfession") or char.get("subProfessionId") or char.get("subProfession")
    if sp:
        candidates.append(sp)

    nation = entry.get("nationId") or (entry.get("mainPower") or {}).get("nationId") or entry.get("nation") or char.get("nationId") or char.get("nation")
    if nation:
        candidates.append(nation)

    candidates = [c for i, c in enumerate(candidates) if c and c not in candidates[:i]]

    if not candidates:
        return ""

    hint = str(random.choice(candidates))
    return map_profession_hint(hint)

"""Xử lý ảnh"""
# --- Image helpers ---
def extract_key_and_variant(filename: str) -> tuple:
    """
    Trích xuất base key và variant từ filename
    Chỉ giữ lại thông tin biến thể thực sự: số, ký hiệu +, hoặc mã skin
    """
    stem = Path(filename).stem
    
    # Chuẩn hóa: chuyển về lowercase, loại bỏ [alpha] và background markers
    normalized = stem.lower()
    normalized = re.sub(r'\[.*?\]', '', normalized)  # Loại bỏ [alpha], [alpha][alpha]
    normalized = re.sub(r'_blackbg|_whitebg', '', normalized)  # Loại bỏ background markers
    normalized = re.sub(r'[^a-z0-9_+#]', '_', normalized)  # Giữ lại ký tự cho variant (+, #)
    normalized = re.sub(r'_+', '_', normalized)  # Chuẩn hóa nhiều _ thành một
    normalized = normalized.strip('_')
    
    # Tách các phần
    parts = normalized.split('_')
    
    # Xác định base key (luôn có format char_number_name)
    if len(parts) >= 3 and parts[0] == 'char' and parts[1].isdigit():
        base_key = f"char_{parts[1]}_{parts[2]}"
        
        # Phần còn lại là variant info - chỉ lấy các phần có chứa số, + hoặc #
        variant_parts = []
        for part in parts[3:]:
            if (any(c.isdigit() for c in part) or '+' in part or '#' in part):
                variant_parts.append(part)
        
        variant_info = '_'.join(variant_parts) if variant_parts else "default"
        
        return base_key, variant_info
    
    # Fallback cho trường hợp không theo format chuẩn
    return normalized, "unknown"

def load_characters_from_files(base_dir: str = None):
    """
    Robust loader for character images.
    """

    if is_r2_enabled():
        # R2 loading code remains the same
        access_key_id = os.getenv("R2_ACCESS_KEY_ID")
        secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY")
        bucket_name = os.getenv("R2_BUCKET_NAME")
        endpoint_url = os.getenv("R2_ENDPOINT_URL")
        return load_characters_from_r2(access_key_id, secret_access_key, bucket_name, endpoint_url, base_dir)
    else:
        # Xóa phần local đi để chỉ sử dụng R2
        return []

"""So khớp mờ"""
# stricter similarity & fuzzy match overrides
def _levenshtein_at_most_one(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = 0
    edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            edits += 1
            if edits > 1:
                return False
            if la == lb:
                i += 1; j += 1
            else:
                j += 1
    if j < lb or i < la:
        edits += 1
    return edits <= 1

def similarity_score(a: str, b: str) -> float:
    # conservative: use SequenceMatcher only to avoid partial-ratio surprises
    try:
        a_n = normalize_for_match(a)
    except Exception:
        a_n = (a or "").strip().lower()
    try:
        b_n = normalize_for_match(b)
    except Exception:
        b_n = (b or "").strip().lower()
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    try:
        return float(SequenceMatcher(None, a_n, b_n).ratio())
    except Exception:
        return 0.0

def fuzzy_match_threshold(guess: str, target: str) -> tuple:
    """Strict enforcement:
      - len(guess) < 4 -> accept only on exact equality with a significant token or whole target
      - len(guess) >= 4 -> require similarity >= 0.90 OR Levenshtein distance <= 1
      No substring/prefix auto-accept.
    Returns (ok: bool, score: float)
    """
    if not guess or not target:
        return False, 0.0

    try:
        ga = normalize_for_match(guess)
    except Exception:
        ga = str(guess).strip().lower()
    try:
        ta = normalize_for_match(target)
    except Exception:
        ta = str(target).strip().lower()

    if not ga or not ta:
        return False, 0.0

    # exact full string
    if ga == ta:
        return True, 1.0

    # detect CJK
    try:
        cjk = is_cjk(ta)
    except Exception:
        cjk = any('\\u4e00' <= ch <= '\\u9fff' for ch in ta)

    # tokenize target
    try:
        min_len = 1 if cjk else 2
        t_tokens = tokenize_for_match(target, min_len=min_len)
    except Exception:
        if cjk:
            t_tokens = list(ta)
        else:
            t_tokens = [x for x in re.split(r'\\s+', ta) if x]

    # build significant tokens
    STOP = {"the","a","an","of","and","in","on","new","old"}
    significant = []
    for tt in t_tokens:
        if not tt: continue
        if (not cjk) and tt in STOP: continue
        if (not cjk and len(tt) < 2) and (not cjk): continue
        significant.append(tt)
    if not significant:
        significant = [ta]

    # if guess too short (<4): exact-only
    if len(ga) < 4:
        for tt in significant:
            if ga == tt:
                return True, 1.0
        return False, 0.0

    # else require similarity >= 0.90 or levenshtein <=1
    TH = 0.90
    best = 0.0
    def _sim(a,b):
        try:
            return float(similarity_score(a,b))
        except Exception:
            return float(SequenceMatcher(None, a, b).ratio())

    # check tokens
    for tt in significant:
        s = _sim(ga, tt)
        if s > best: best = s
        if s >= TH:
            return True, float(s)

    # whole target
    whole = _sim(ga, ta)
    if whole > best: best = whole
    if whole >= TH:
        return True, float(whole)

    # fallback levenshtein <=1
    for tt in significant:
        if _levenshtein_at_most_one(ga, tt):
            return True, float(best)
    if _levenshtein_at_most_one(ga, ta):
        return True, float(best)

    return False, float(best)

"""Định dạng hiển thị"""
# leaderboard display 
def display_len(s: str) -> int:
    import unicodedata
    def _w(ch):
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            return 0
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ("F", "W"): return 2
        if unicodedata.category(ch) == "So": return 2
        return 1
    return sum(_w(ch) for ch in s)

def pad_display(s: str, width: int, align="left") -> str:
    w = display_len(s)
    if w >= width:
        return s
    pad = " " * (width - w)
    return s + pad if align=="left" else pad + s

"""Quản lý trò chơi"""
# --- Game state ---
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

games = {}

# --- Tải characters once at startup ---
characters_list = []

async def reveal_answer(channel: discord.TextChannel, char):
    reveal_name = None
    if isinstance(char, dict):
        reveal_name = char.get("_reveal_name") or char.get("_display_name_en") or char.get("_display_name_cn") or char.get("name")
    if not reveal_name:
        reveal_name = "Unknown"
    try:
        full_choice = None
        # prefer fulls from the chosen_pair_id if available
        if isinstance(char, dict):
            chosen_pair = char.get('_chosen_pair_id')
            if chosen_pair and char.get('variants'):
                for v in char.get('variants'):
                    if (v.get('pair_id') == chosen_pair or v.get('skin_name') == chosen_pair) and v.get('fulls'):
                        full_choice = random.choice(v.get('fulls'))
                        break
            # fallback: try to find any variant that contains the chosen silhouette path and use its fulls
            if not full_choice and char.get('_chosen_silhouette_path') and char.get('variants'):
                silp = char.get('_chosen_silhouette_path')
                for v in char.get('variants'):
                    if silp in (v.get('silhouettes') or []):
                        if v.get('fulls'):
                            full_choice = random.choice(v.get('fulls'))
                            break
            # fallback to aggregated fulls
            if not full_choice:
                alls = char.get('all_fulls') or []
                if alls:
                    full_choice = random.choice(alls)
                else:
                    full_choice = char.get('full')
        if full_choice:
            if is_r2_enabled():
                try:
                    file_path = download_r2_object(full_choice)
                    await channel.send(
                        file=discord.File(file_path, filename="full.png"),
                        content=f"Đáp án: **{reveal_name}**"
                    )
                    os.unlink(file_path)
                except Exception as e:
                    print(f"Failed to download from R2: {e}")
                    await channel.send(f"Đáp án: **{reveal_name}** (lỗi tải ảnh)")
            else:
                await channel.send(
                    file=discord.File(full_choice, filename="full.png"),
                    content=f"Đáp án: **{reveal_name}**"
                )
    except Exception as e:
        print("Failed to send reveal answer:", e)

"""Lệnh và xử lý sự kiện"""
# --- Commands ---
@bot.event
async def on_ready():
    global characters_list
    
    # Kiểm tra kết nối R2
    if is_r2_enabled():
        print("✅ R2 storage enabled")
        try:
            # Test R2 connection
            s3 = boto3.client('s3',
                endpoint_url=os.getenv("R2_ENDPOINT_URL"),
                aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
                config=Config(signature_version='s3v4')
            )
            s3.list_objects_v2(Bucket=os.getenv("R2_BUCKET_NAME"), MaxKeys=1)
            print("✅ R2 connection successful")
        except Exception as e:
            print(f"❌ R2 connection failed: {e}")
    else:
        print("ℹ️ Using local storage")
    
    characters_list = load_characters_from_files()
    print(f"Bot ready. Logged in as {bot.user} ({bot.user.id})")
    print(f"Loaded characters from images: {len(characters_list)}")

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
            asyncio.create_task(schedule_next(origin, 0))  # Đây là dòng sửa

@bot.command(name="start")
async def start_game(ctx, seconds: int = 0):
    global characters_list
    channel = ctx.channel
    if channel.id in games and games[channel.id].current and not games[channel.id].guessed:
        await ctx.send("Đang có ván đang chạy trong kênh này. Dùng !stop để dừng.")
        return
    
    if not characters_list:
        await ctx.send("Không tìm thấy ảnh trong thư mục `images/` hoặc R2. Hãy thêm ảnh rồi thử lại.")
        return

    char = random.choice(characters_list)

    key = char.get("key")

     # Sử dụng hàm helper mới
    display_en, display_cn = get_display_names(key, char)
    lookup_key = canonicalize_key(key)  # Giữ lại để dùng cho fallback_name

    # keep fallback_name calculation same as before
    fallback_name = display_en or display_cn or lookup_key.replace("char_", "").replace("_", " ").title()
    chars_no_space = len([c for c in fallback_name if not c.isspace()])
    auto_seconds = min(max(30, chars_no_space * 3), 60)
    if isinstance(seconds, int) and seconds > 0:
        use_seconds = min(seconds, 60)
        auto_used = False
    else:
        use_seconds = auto_seconds
        auto_used = True

    reveal_name = display_en or display_cn or fallback_name

    print("=== START ROUND ===")
    print(f"Channel: {channel} (id={channel.id})")
    print(f"Key: {key}")
    print(f"English name (preferred): {display_en}")
    print(f"Chinese name: {display_cn}")
    print(f"Using reveal_name: {repr(reveal_name)}")


    # pick a variant that has at least one silhouette; prefer base variant if present but still random among variants
    sil_path = None
    chosen_variant = None
    chosen_pair = None
    variants = char.get('variants') or []
    # try variants that have silhouettes
    sil_variants = [v for v in variants if v.get('silhouettes')]
    if sil_variants:
        chosen_variant = random.choice(sil_variants)
        sil_path = random.choice(chosen_variant.get('silhouettes'))
        chosen_pair = chosen_variant.get('pair_id') or chosen_variant.get('skin_name')
        print(f"Variant : {chosen_pair}")
        print(f"Time limit used: {use_seconds} seconds (auto? {auto_used})")
        print("====================")

    else:
        # fallback: aggregated silhouettes
        if char.get('all_silhouettes'):
            sil_path = random.choice(char.get('all_silhouettes'))
            for v in variants:
                if sil_path in (v.get('silhouettes') or []):
                    chosen_variant = v
                    chosen_pair = v.get('pair_id') or v.get('skin_name')
                    print(f"Variant:{chosen_pair}")
                    print(f"Time limit used: {use_seconds} seconds (auto? {auto_used})")
                    print("====================")
                    break
        else:
            sil_path = None

    if not sil_path:
        await ctx.send("Không tìm thấy ảnh silhouette cho nhân vật đã chọn.")
        return
    if is_r2_enabled():
        # R2 handling unchanged
        loop = asyncio.get_event_loop()
        sil_file_path = await loop.run_in_executor(None, download_r2_object, sil_path)
        msg = await channel.send(
            file=discord.File(sil_file_path, filename="silhouette.png"),
            content=f"🔍 **Who is this?** You have {use_seconds} seconds to guess!{' (auto)' if auto_used else ''} (Gõ tên vào chat)"
        )
        os.unlink(sil_file_path)
    else:
        # FIXED: Use absolute path for local files
        try:
            msg = await channel.send(
                file=discord.File(sil_path, filename="silhouette.png"),
                content=f"🔍 **Who is this?** You have {use_seconds} seconds to guess!{' (auto)' if auto_used else ''} (Gõ tên vào chat)"
            )
        except Exception as e:
            await ctx.send(f"Lỗi khi gửi ảnh: {e}")
            return
    state = GameState(channel, origin_ctx=ctx)
    state.current = dict(char)

    # preserve original key, but use canonical key for matching/lookups
    orig_key = state.current.get("key")
    try:
        canonical = canonicalize_key(orig_key)
    except Exception:
        canonical = orig_key
    state.current["_orig_key"] = orig_key
    state.current["key"] = canonical
    # record which pair/variant was used for silhouette (if any)
    state.current["_chosen_pair_id"] = chosen_pair
    state.current["_chosen_silhouette_path"] = sil_path
    state.current["_display_name_en"] = display_en
    state.current["_display_name_cn"] = display_cn
    state.current["_reveal_name"] = reveal_name
    state.current["_time_limit"] = use_seconds
    # --- ensure profession/subProfession/nation present for hint generation (auto-populate) ---
    try:
        k = state.current.get("key")
        try:
            canonical_k = canonicalize_key(k)
        except Exception:
            canonical_k = k
        matched_level = 0
        # attempt to use canonical key for looking up profession info
        entry = {}
        if canonical_k:
            entry = EN_JSON.get(canonical_k) or CN_JSON.get(canonical_k) or {}
        else:
            entry = EN_JSON.get(k) or CN_JSON.get(k) or {}
        prof = entry.get("profession") or entry.get("subProfession") or entry.get("subProfessionId") or entry.get("mainProfession") or entry.get("professionId")
        # if no profession on canonical entry, try base key (strip codename digits) or shorter key for profession only
        if not prof and k:
            parts_try = k.split("_")
            if len(parts_try) >= 3:
                import re as _re_try
                m = _re_try.match(r'^([a-zA-Z]+)(\\d+)$', parts_try[2])
                if m:
                    base_key = f"{parts_try[0]}_{parts_try[1]}_{m.group(1)}"
                    prof = (EN_JSON.get(base_key) or CN_JSON.get(base_key) or {}).get('profession')
                if prof is None and len(parts_try) >= 4 and parts_try[-1].isdigit():
                    shorter = "_".join(parts_try[:-1])
                    prof = (EN_JSON.get(shorter) or CN_JSON.get(shorter) or {}).get('profession')
        if prof and not state.current.get("profession"):
            state.current["profession"] = prof
        # debug
        candidates = []
        if entry.get("profession"): candidates.append(entry.get("profession"))
        if entry.get("subProfession") or entry.get("subProfessionId"): candidates.append(entry.get("subProfession") or entry.get("subProfessionId"))
        if entry.get("nation") or entry.get("nationId"): candidates.append(entry.get("nation") or entry.get("nationId"))
        print(f"[HINT DEBUG] original_key={k} canonical_key={canonical_k} matched_level={matched_level} candidates={candidates} -> inserted_profession={state.current.get('profession')}")
    except Exception as e:
        print("[HINT DEBUG] populate error:", e)

    state.current["_hint"] = generate_hint_for_char(state.current)
    state.hint = state.current.get("_hint")
    state.started_at = datetime.utcnow()
    state.guessed = False
    games[channel.id] = state

    async def timeout_job():
        await asyncio.sleep(use_seconds)
        if channel.id in games and not games[channel.id].guessed:
            await channel.send(f"⏰ Hết giờ! Đáp án là **{state.current.get('_reveal_name') or state.current.get('_display_name_en') or state.current.get('_display_name_cn')}**.")
            await reveal_answer(channel, state.current)
            games.pop(channel.id, None)
            if channel.id in looping_channels:
                origin = state.origin_ctx or ctx
                # Sửa: truyền 0 để tự động tính thời gian
                asyncio.create_task(schedule_next(origin, 0))
    state.timeout_task = asyncio.create_task(timeout_job())

@bot.command(name="stop")
async def stop_game(ctx):
    cid = ctx.channel.id
    looping_channels.discard(cid)
    looping_settings.pop(cid, None)

    # hủy schedule nếu còn
    task = scheduled_tasks.pop(cid, None)
    if task:
        task.cancel()

    state = games.pop(cid, None)
    if state and state.timeout_task:
        state.timeout_task.cancel()

    await ctx.send("⏹️ Đã dừng ván chơi.")

@bot.command(name="startloop")
async def start_loop(ctx, loop_delay: int = LOOP_DELAY, seconds: int = 30):
    channel = ctx.channel
    # nếu không truyền, dùng mặc định toàn cục
    if loop_delay is None:
        loop_delay = LOOP_DELAY
    looping_channels.add(channel.id)
    looping_settings[channel.id] = loop_delay
    await ctx.send(
        f"🔁 Bắt đầu chế độ lặp: {seconds}s mỗi ván, chờ {loop_delay}s giữa các ván."
    )
    await start_game(ctx, seconds)

@bot.command(name="skip")
@commands.has_permissions(manage_messages=True)
async def skip_round(ctx):
    channel = ctx.channel
    if channel.id not in games:
        await ctx.send("Không có ván nào để skip.")
        return

    state = games.pop(channel.id)
    if state.timeout_task:
        state.timeout_task.cancel()

    reveal = state.current.get('_reveal_name') or state.current.get('_display_name_en') or state.current.get('_display_name_cn') or state.current.get('name')
    await ctx.send(f"✳ Ván bị skip. Đáp án: **{reveal}**.")
    await reveal_answer(channel, state.current)

    # chỉ lên lịch ván mới nếu đang ở chế độ loop
    if channel.id in looping_channels:
        origin = state.origin_ctx or ctx
        # lưu task để có thể hủy khi stop
        task = asyncio.create_task(schedule_next(origin, 0))
        scheduled_tasks[channel.id] = task

@bot.command(name="hint")
async def provide_hint(ctx):
    channel = ctx.channel
    if channel.id not in games:
        await ctx.send("Chưa có ván nào.")
        return
    state = games[channel.id]
    if not state.hint:
        await ctx.send("🔎 Gợi ý: (không có gợi ý cho nhân vật này)")
        return
    if getattr(state, 'hint_shown', False):
        await ctx.send("Gợi ý đã được hiện rồi trong ván này.")
        return
    await ctx.send(f"🔎 Gợi ý: {state.hint}")
    state.hint_shown = True

@bot.command(name="leaderboard")
async def leaderboard(ctx):
    guild_id = str(ctx.guild.id)
    guild_scores = all_scores.get(guild_id, {})
    if not guild_scores:
        await ctx.send("Chưa có ai có điểm cả.")
        return

    # chỉ lấy top 9
    sorted_scores = sorted(guild_scores.items(), key=lambda x: x[1], reverse=True)[:9]

    rows = []
    for i, (uid, score) in enumerate(sorted_scores, start=1):
        try:
            member = await ctx.guild.fetch_member(int(uid))
            name = member.display_name
        except Exception:
            name = f"Người chơi {uid}"

        rank = f"#{i}"
        rows.append((rank, f"{score} điểm", name))

    # --- tính độ rộng ---
    rank_w  = max(display_len(rank) for rank, _, _ in rows)
    score_w = max(display_len(score) for _, score, _ in rows)

    lines = ["🏆 Bảng xếp hạng (Top 9) 🏆", "```"]
    for rank, score, name in rows:
        rank_col  = pad_display(rank, rank_w, "left")
        score_col = pad_display(score, score_w, "right")
        lines.append(f"{rank_col} | {score_col} | {name}")
    lines.append("```")

    await ctx.send("\n".join(lines))

@bot.command(name="myscore")
async def myscore(ctx):
    guild_id = str(ctx.guild.id)
    uid = str(ctx.author.id)
    score = all_scores.get(guild_id, {}).get(uid, 0)
    await ctx.send(f"**{ctx.author.display_name}**, bạn có **{score}** điểm.")

@bot.command(name="commandhelp")
async def show_help(ctx):
    """Hiển thị hướng dẫn sử dụng bot"""
    help_text = """
    🎮 **Hướng dẫn sử dụng WhoThatCharacter Bot** 🎮

    `!start [giây]` - Bắt đầu ván chơi mới (tùy chọn thời gian)
    `!stop` - Dừng ván chơi hiện tại
    `!startloop [giây]` - Bật chế độ lặp tự động với khoản cách ván
    `!skip` - Bỏ qua ván hiện tại 
    `!hint` - Xem gợi ý cho ván hiện tại
    `!leaderboard` - Xem bảng xếp hạng
    `!myscore` - Xem điểm của bạn
    `!op <key>` - Xem thông tin nhân vật (VD: `!op char_002_amiya`)
    `!commandhelp` - Hiển thị hướng dẫn này

    ⚙️ **Cách chơi:**
    - Bot sẽ gửi ảnh silhouette nhân vật
    - Bạn có 30-60 giây để đoán tên
    - Đoán đúng được +1-10 điểm
    - Dùng !hint để xem gợi ý nghề nghiệp/quốc gia
    """
    await ctx.send(help_text)

@bot.command(name="op")
async def op_info(ctx, key: str):
    """
    Hiển thị thông tin chi tiết về nhân vật dựa trên key
    Cú pháp: !op <key> (ví dụ: !op char_002_amiya)
    """
    chars = load_characters_from_files()
    canonical_input = canonicalize_key(key)
    found_char = None
    
    # Tìm nhân vật khớp với key đã nhập
    for char in chars:
        char_key = char.get("key")
        canonical_char_key = canonicalize_key(char_key)
        if canonical_char_key == canonical_input:
            found_char = char
            break

    if not found_char:
        await ctx.send(f"❌ Không tìm thấy nhân vật với key `{key}` (canonical: `{canonical_input}`)")
        return

    # Sử dụng hàm helper mới
    display_en, display_cn = get_display_names(key, found_char)

    # Tạo thông điệp định dạng
    msg = (
        "=== START ROUND ===\n"
        f"English name (preferred): {display_en}\n"
        f"Chinese name: {display_cn}\n"
    )
    await ctx.send(f"```\n{msg}\n```")

"""Khởi chạy chính"""
# END 
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
    # khởi động web server
    await start_web()
    # khởi động bot discord
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
""" make by: - Chat GPT
             - Deepseek
             - CarKingMoewOh """
        