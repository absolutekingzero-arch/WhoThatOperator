"""Game logic module for WhoThatOperator bot."""

import os
import random
import asyncio
import discord

from .config import is_r2_enabled
from .image_processing import download_r2_object

# Game state management

async def reveal_answer(channel: discord.TextChannel, char):
    """Reveal the full character image after guessing."""
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
