import os
import random
import asyncio
import discord
import re as _re_try
from datetime import datetime
from .image_processing import load_characters_from_files
from .config import  GameState, all_scores ,LOOP_DELAY,is_r2_enabled, games,looping_channels, looping_settings,scheduled_tasks
from .utils import EN_JSON, CN_JSON,canonicalize_key,get_display_names,download_r2_object,generate_hint_for_char,display_len, pad_display
# Change import to:
from .game_logic import (
    reveal_answer, schedule_next
)
try:
    characters_list = load_characters_from_files()
    print(f"[INIT] characters_list loaded: {len(characters_list)} entries.")
except Exception as e:
    characters_list = []
    print("[INIT] Failed to load characters_list:", e)

# debug EN/CN json sizes
try:
    print(f"[INIT] EN_JSON entries: {len(EN_JSON)}; CN_JSON entries: {len(CN_JSON)}")
except Exception as e:
    print("[INIT] Failed to inspect EN/CN JSON:", e)

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

async def stop_game(ctx):
    cid = ctx.channel.id
    looping_channels.discard(cid)
    looping_settings.pop(cid, None)

    # Cancel scheduled task if exists
    task = scheduled_tasks.pop(cid, None)
    if task:
        task.cancel()

    state = games.pop(cid, None)
    if state and state.timeout_task:
        state.timeout_task.cancel()

    await ctx.send("⏹️ Đã dừng ván chơi.")

async def start_loop(ctx, loop_delay: int = LOOP_DELAY, seconds: int = 30):
    channel = ctx.channel
    if loop_delay is None:
        loop_delay = LOOP_DELAY
    looping_channels.add(channel.id)
    looping_settings[channel.id] = loop_delay
    await ctx.send(
        f"🔁 Bắt đầu chế độ lặp: {seconds}s mỗi ván, chờ {loop_delay}s giữa các ván."
    )
    await start_game(ctx, seconds)

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

    if channel.id in looping_channels:
        origin = state.origin_ctx or ctx
        task = asyncio.create_task(schedule_next(origin, 0))
        scheduled_tasks[channel.id] = task

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

# Sửa hàm leaderboard
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

async def myscore(ctx):
    guild_id = str(ctx.guild.id)
    uid = str(ctx.author.id)
    score = all_scores.get(guild_id, {}).get(uid, 0)
    await ctx.send(f"**{ctx.author.display_name}**, bạn có **{score}** điểm.")

async def show_help(ctx):
    help_text = """
    🎮 Hướng dẫn sử dụng WhoThatCharacter Bot 🎮

    `!start [giây]` - Bắt đầu ván chơi mới (tùy chọn thời gian)
    `!stop` - Dừng ván chơi hiện tại
    `!startloop [giây]` - Bật chế độ lặp tự động với khoản cách ván
    `!skip` - Bỏ qua ván hiện tại 
    `!hint` - Xem gợi ý cho ván hiện tại
    `!leaderboard` - Xem bảng xếp hạng
    `!myscore` - Xem điểm của bạn
    `!op <key>` - Xem thông tin nhân vật (VD: `!op char_002_amiya`)
    `!commandhelp` - Hiển thị hướng dẫn này
    """
    await ctx.send(help_text)

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
        "===============\n"
        f"English name (preferred): {display_en}\n"
        f"Chinese name: {display_cn}\n"
        "===============\n"
    )
    await ctx.send(f"```\n{msg}\n```")


def setup(bot):
    bot.command(name="start")(start_game)
    bot.command(name="stop")(stop_game)
    bot.command(name="startloop")(start_loop)
    bot.command(name="skip")(skip_round)
    bot.command(name="hint")(provide_hint)
    bot.command(name="leaderboard")(leaderboard)
    bot.command(name="myscore")(myscore)
    bot.command(name="commandhelp")(show_help)
    bot.command(name="op")(op_info)