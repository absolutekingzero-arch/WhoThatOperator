import os
import re
import random
import unicodedata
import json
from pathlib import Path
from difflib import SequenceMatcher
import boto3
from botocore.config import Config
import tempfile
from typing import Dict, Any


# Import từ config
from .config import (
    EN_JSON_PATH, CN_JSON_PATH, PROFESSION_MAP_PATH, CN_ONLY_MAP_PATH,
    R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT_URL,
    AMIYA_JSON_PATH,STOPWORDS
)



# --- JSON loading utilities ---
def safe_load_json(p: Path):
    """Safely load JSON file with error handling"""
    try:
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Failed to load JSON {p}: {e}")
    return {}

def load_map_file(path: Path):
    """Load map files with consistent structure"""
    data = safe_load_json(path)
    if isinstance(data, dict) and "map" in data:
        return data["map"]
    return data if isinstance(data, dict) else {}

# Load JSON data once
EN_JSON = safe_load_json(EN_JSON_PATH)
CN_JSON = safe_load_json(CN_JSON_PATH)
AMIYA_JSON = safe_load_json(AMIYA_JSON_PATH)
PROFESSION_MAP = load_map_file(PROFESSION_MAP_PATH)
CN_ONLY_MAP = load_map_file(CN_ONLY_MAP_PATH)


# --- Text normalization and matching utilities ---
def normalize_for_match(s: str) -> str:
    """Normalize string for fuzzy matching"""
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_cjk(s: str) -> bool:
    """Check if string contains CJK characters"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in (s or ""))

def tokenize_for_match(s: str, min_len: int = 1):
    """Tokenize string for matching, handling CJK differently"""
    n = normalize_for_match(s)
    if not n:
        return []
    if is_cjk(n):
        if min_len <= 1:
            return [ch for ch in n if ch and ch not in STOPWORDS]
        return [n] if len(n) >= min_len else []
    toks = [t for t in n.split() if t and t not in STOPWORDS and len(t) >= min_len]
    return toks

def _levenshtein_at_most_one(a: str, b: str) -> bool:
    """Check if Levenshtein distance is at most 1"""
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
    """Calculate similarity score between two strings"""
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
    """
    Strict fuzzy matching with thresholds
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
    significant = []
    for tt in t_tokens:
        if not tt: continue
        if (not cjk) and tt in STOPWORDS: continue
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

# --- Name and key utilities ---
def get_display_names(key: str, char: dict) -> tuple:
    """
    Find character names with full key (including variant numbers).
    If missing EN name, try to get from CN_ONLY_MAP without changing display_cn.
    Also check AMIYA_JSON for special Amiya variants.
    Returns (display_en, display_cn)
    """
    # Try multiple key variants
    key_variants = [key]
    parts = key.split('_')
    if len(parts) >= 4:
        base_key = "_".join(parts[:3])
        key_variants.append(base_key)

    # 1) First check if this is a special Amiya variant
    if key in AMIYA_JSON.get("patchChars", {}):
        patch_char = AMIYA_JSON["patchChars"][key]
        display_en = patch_char.get("name")
        display_cn = patch_char.get("name")  # Use same name for CN
        return display_en, display_cn

    # 2) Find in EN_JSON
    display_en = None
    for variant in key_variants:
        if EN_JSON and variant in EN_JSON and isinstance(EN_JSON[variant], dict):
            display_en = EN_JSON[variant].get("name") or EN_JSON[variant].get("displayName") or EN_JSON[variant].get("english_name")
            if display_en:
                break

    # 3) Find in CN_JSON
    display_cn = None
    for variant in key_variants:
        if CN_JSON and variant in CN_JSON and isinstance(CN_JSON[variant], dict):
            display_cn = CN_JSON[variant].get("name") or CN_JSON[variant].get("label")
            if display_cn:
                break

    # 4) Fallback from image metadata (don't prioritize overriding CN)
    if not display_en and char.get("name"):
        display_en = char.get("name")
    if not display_cn and char.get("name"):
        display_cn = char.get("name")

    # 5) If still missing display_en -> try lookup in CN_ONLY_MAP
    if not display_en and isinstance(CN_ONLY_MAP, dict):
        found = None
        for variant in key_variants:
            if variant in CN_ONLY_MAP:
                found = CN_ONLY_MAP[variant]
                break

        if not found and display_cn:
            if display_cn in CN_ONLY_MAP:
                found = CN_ONLY_MAP[display_cn]

        if found:
            if isinstance(found, dict):
                display_en = found.get("en") or found.get("english") or found.get("name") or display_en
            elif isinstance(found, str):
                display_en = found

    return display_en, display_cn

def canonicalize_key(key: str):
    """Temporarily don't canonicalize - keep original key"""
    return key

def map_profession_hint(raw_prof: str) -> str:
    """Map profession to display name"""
    if not raw_prof:
        return raw_prof
    s = str(raw_prof).strip().upper()
    return PROFESSION_MAP.get(s, raw_prof)

def _slugify(name: str) -> str:
    if not name:
        return ""
    s = str(name)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s\-]+", "_", s)
    return s.strip("_")
# --- generate_hint_for_char (finalized behavior) ---
def generate_hint_for_char(char: dict, prefer: str = None, troll_chance: float = 0.002) -> str:
    """
    Return a single hint string. Options are:
      - "Class: <mapped profession>"
      - "Sub-class: <subprofession>"
      - "Thuộc : <nation>"  (main)
      - "Quốc gia liên quan : <nation>" (sub)
      - rare troll -> "is an operator"
    Placeholder for image is included and follows:
      - Class -> <<R2:images/Icon/icon_profession_<slug>.png>>
      - Sub-class -> <<R2:images/subicon/sub_<slug>_icon.png>>
      - Nation -> <<R2:images/Logo/Logo_<slug>.png>>
    """
    if not isinstance(char, dict):
        return ""

    key = char.get("key", "")
    entry = {}
    if key:
        entry = EN_JSON.get(key) or CN_JSON.get(key) or {}

    # pre-build fallback placeholder base
    fallback_name = f"{key}.png" if key else "unknown.png"

    # Very rare troll check (roll before everything to surprise)
    try:
        roll = random.random()
    except Exception:
        roll = 1.0
    if 0.0 <= float(troll_chance) and roll < float(troll_chance):
        # Use fallback placeholder (images path) for troll
        ph = f"<<R2:images/Icon/icon_profession_unknown.png>>"
        return f"is an operator {ph}"

    # Amiya patch special-case: still treat profession/subProfession but with new labels
    if key in AMIYA_JSON.get("patchChars", {}):
        patch_char = AMIYA_JSON["patchChars"][key]
        profession = patch_char.get("profession")
        sub_profession = patch_char.get("subProfessionId")
        options = []
        if profession:
            options.append(f"Class: {map_profession_hint(profession)}")
        if sub_profession:
            options.append(f"Sub-class: {sub_profession}")
        placeholder = f"<<R2:images/Icon/icon_profession_{_slugify(profession or 'unknown')}.png>>"
        if options:
            return f"{random.choice(options)} {placeholder}"
        return placeholder

    # collect class & subclass
    profession = entry.get("profession") or char.get("profession")
    subprofession = (entry.get("subProfessionId") or entry.get("subProfession")
                     or char.get("subProfessionId") or char.get("subProfession"))

    # nations: mainPower.nationId, nationId/nation, plus subPower array
    nations = []
    main_power = (entry.get("mainPower") or {}).get("nationId") or entry.get("nationId") \
                 or entry.get("nation") or char.get("nationId") or char.get("nation")
    if main_power:
        nations.append({"value": str(main_power), "role": "main"})

    subp = entry.get("subPower") or char.get("subPower")
    if isinstance(subp, list):
        for sp in subp:
            if isinstance(sp, dict):
                nid = sp.get("nationId") or sp.get("nation")
                if nid and not (main_power and str(nid) == str(main_power)):
                    nations.append({"value": str(nid), "role": "sub"})

    # dedupe nations preserving order
    seen = set(); deduped = []
    for n in nations:
        if n["value"] not in seen:
            seen.add(n["value"]); deduped.append(n)
    nations = deduped

    # Build options: class, sub-class, nations (with VN labels)
    options = []
    if profession:
        options.append(("class", f"Class: {map_profession_hint(str(profession))}", f"images/Icon/icon_profession_{_slugify(profession)}.png"))
    if subprofession:
        options.append(("subclass", f"Sub-class: {str(subprofession)}", f"images/subicon/sub_{_slugify(subprofession)}_icon.png"))

    for n in nations:
        if n.get("role") == "main":
            label = f"Thuộc : {n['value']}"
            filename = f"images/Logo/logo_{_slugify(n['value'])}.png"
            options.append(("nation_main", label, filename))
        else:
            label = f"Quốc gia liên quan : {n['value']}"
            filename = f"images/Logo/logo_{_slugify(n['value'])}.png"
            options.append(("nation_sub", label, filename))

    # If prefer is set, filter options accordingly
    if prefer:
        p = prefer.lower()
        filtered = []
        for kind, label, fname in options:
            if p in ("class", "profession") and kind == "class":
                filtered.append((kind, label, fname))
            elif p in ("subclass", "sub-profession", "sub_profession") and kind == "subclass":
                filtered.append((kind, label, fname))
            elif p == "nation" and kind.startswith("nation"):
                filtered.append((kind, label, fname))
        if filtered:
            options = filtered

    # Deduplicate labels (preserve order)
    seen_labels = set()
    deduped_opts = []
    for kind, label, fname in options:
        if label not in seen_labels:
            seen_labels.add(label)
            deduped_opts.append((kind, label, fname))
    options = deduped_opts

    # Choose random option (if any)
    if options:
        kind, chosen_label, chosen_file = random.choice(options)
        placeholder = f"<<R2:{chosen_file}>>"
        return f"{chosen_label} {placeholder}"

    # nothing to choose: return fallback placeholder
    fallback_ph = f"<<R2:images/Icon/icon_profession_unknown.png>>"
    return fallback_ph

def extract_key_and_variant(filename: str) -> tuple:
    """
    Extract base key and variant from filename
    Keep only real variant information: numbers, + symbol, or skin codes
    """
    stem = Path(filename).stem
    
    # Normalize: convert to lowercase, remove [alpha] and background markers
    normalized = stem.lower()
    normalized = re.sub(r'\[.*?\]', '', normalized)
    normalized = re.sub(r'_blackbg|_whitebg', '', normalized)
    normalized = re.sub(r'[^a-z0-9_+#]', '_', normalized)
    normalized = re.sub(r'_+', '_', normalized)
    normalized = normalized.strip('_')
    
    # Split parts
    parts = normalized.split('_')
    
    # Determine base key (always has format char_number_name)
    if len(parts) >= 3 and parts[0] == 'char' and parts[1].isdigit():
        base_key = f"char_{parts[1]}_{parts[2]}"
        
        # Remaining parts are variant info - only take parts with numbers, + or #
        variant_parts = []
        for part in parts[3:]:
            if (any(c.isdigit() for c in part) or '+' in part or '#' in part):
                variant_parts.append(part)
        
        variant_info = '_'.join(variant_parts) if variant_parts else "default"
        
        return base_key, variant_info
    
    # Fallback for non-standard format
    return normalized, "unknown"

def download_r2_object(object_key: str) -> str:
    """Download object from R2 storage"""
    s3 = boto3.client('s3',
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4')
    )
    
    fd, path = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    
    try:
        s3.download_file(R2_BUCKET_NAME, object_key, path)
        return path
    except Exception as e:
        os.unlink(path)
        raise e

# --- Display formatting utilities ---
def display_len(s: str) -> int:
    """Calculate display length considering Unicode characters"""
    def _w(ch):
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            return 0
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ("F", "W"): return 2
        if unicodedata.category(ch) == "So": return 2
        return 1
    return sum(_w(ch) for ch in s)

def pad_display(s: str, width: int, align="left") -> str:
    """Pad string for display considering Unicode width"""
    w = display_len(s)
    if w >= width:
        return s
    pad = " " * (width - w)
    return s + pad if align=="left" else pad + s

