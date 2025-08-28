"""Image processing module for WhoThatOperator bot"""

import os
import random
import tempfile
from pathlib import Path
import boto3
from botocore.config import Config
import discord
from .config import is_r2_enabled, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT_URL
# Import from other modules
from .utils import extract_key_and_variant, canonicalize_key, get_display_names, EN_JSON, CN_JSON

def load_characters_from_r2(access_key_id, secret_access_key, bucket_name, endpoint_url, base_dir: str = None):
    """Load characters from R2 storage"""
    
    s3 = boto3.client('s3',
                      endpoint_url=endpoint_url,
                      aws_access_key_id=access_key_id,
                      aws_secret_access_key=secret_access_key,
                      config=Config(signature_version='s3v4'))
    chars = {}
    
    print("=== R2 DEBUG ===")
    print(f"Bucket: {bucket_name}")
    print(f"Prefixes: ['Char/', 'Skin/']")

    def ensure_ent(key):
        if key not in chars:
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

    # List objects from R2 bucket
    prefixes = ["images/Char/", "images/Skin/"]
    total_objects = 0
    
    for prefix in prefixes:
        print(f"Scanning R2 prefix: {prefix}")
        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

        for page in pages:
            for obj in page.get('Contents', []):
                key = obj.get('Key')
                if not key or key.endswith('/'):
                    continue

                total_objects += 1

                stem = Path(key).stem
                try:
                    base_key, variant_info = extract_key_and_variant(stem)
                except Exception:
                    base_key, variant_info = stem, "unknown"

                canonical_key = canonicalize_key(base_key)

                effective_key = canonical_key
                if not ((EN_JSON and effective_key in EN_JSON) or (CN_JSON and effective_key in CN_JSON)):
                    effective_key = base_key

                is_silhouette = '[alpha]' in key.lower()
                kind = "sil" if is_silhouette else "full"

                pair_id = variant_info or "default"

                ent = ensure_ent(effective_key)
                add_to_pair(ent, pair_id, kind, key)

    print(f"Total objects found: {total_objects}")
    print("=================")

    # Build results
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
        
        display_en, display_cn = get_display_names(k, ent)
        if display_en:
            ent["name"] = display_en
        elif display_cn:
            ent["name"] = display_cn
        
        results.append(ent)

    print(f"Loaded {len(results)} characters from R2")
    return [v for v in results if v.get("all_fulls") or v.get("all_silhouettes")]

def download_r2_object(object_key: str) -> str:
    """Download an object from R2 storage to a temporary file"""
    # Use R2_* constants imported from config at top of file
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
        # ensure cleanup, then re-raise so caller can handle/log
        try:
            os.unlink(path)
        except Exception:
            pass
        raise

async def send_silhouette_image(channel, sil_path, use_seconds, auto_used):
    """Send silhouette image to channel"""
    if is_r2_enabled():
        try:
            file_path = download_r2_object(sil_path)
            msg = await channel.send(
                file=discord.File(file_path, filename="silhouette.png"),
                content=f"🔍 **Who is this?** You have {use_seconds} seconds to guess!{' (auto)' if auto_used else ''} (Gõ tên vào chat)"
            )
            os.unlink(file_path)
            return msg
        except Exception as e:
            await channel.send(f"Lỗi khi tải ảnh từ R2: {e}")
            return None
    else:
        try:
            msg = await channel.send(
                file=discord.File(sil_path, filename="silhouette.png"),
                content=f"🔍 **Who is this?** You have {use_seconds} seconds to guess!{' (auto)' if auto_used else ''} (Gõ tên vào chat)"
            )
            return msg
        except Exception as e:
            await channel.send(f"Lỗi khi gửi ảnh: {e}")
            return None

async def reveal_answer(channel, char):
    """Reveal the answer with full image"""    
    reveal_name = None
    if isinstance(char, dict):
        reveal_name = char.get("_reveal_name") or char.get("_display_name_en") or char.get("_display_name_cn") or char.get("name")
    if not reveal_name:
        reveal_name = "Unknown"
    
    try:
        full_choice = None
        if isinstance(char, dict):
            chosen_pair = char.get('_chosen_pair_id')
            if chosen_pair and char.get('variants'):
                for v in char.get('variants'):
                    if (v.get('pair_id') == chosen_pair or v.get('skin_name') == chosen_pair) and v.get('fulls'):
                        full_choice = random.choice(v.get('fulls'))
                        break
            
            if not full_choice and char.get('_chosen_silhouette_path') and char.get('variants'):
                silp = char.get('_chosen_silhouette_path')
                for v in char.get('variants'):
                    if silp in (v.get('silhouettes') or []):
                        if v.get('fulls'):
                            full_choice = random.choice(v.get('fulls'))
                            break
            
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

def load_characters_from_files(base_dir: str = None):
    """Load characters from files or R2 storage"""
    if is_r2_enabled():
        # call the R2 loader with env vars from config
        try:
            return load_characters_from_r2(
                R2_ACCESS_KEY_ID,
                R2_SECRET_ACCESS_KEY,
                R2_BUCKET_NAME,
                R2_ENDPOINT_URL,
                base_dir
            )
        except Exception as e:
            print("[image_processing] Failed to load from R2:", e)
            return []
    else:
        # TODO: implement local filesystem scanning if you want local images.
        # For now return empty list so caller can handle it.
        return []