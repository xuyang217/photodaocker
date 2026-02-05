#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
每日相册渲染脚本：
- 从 photos.db / photo_scores 中选出一张“历史上的今天”照片
- 用 LXGWHeartSerifMN.ttf 把文案 / 日期 / 地点都画到图上
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import json
import datetime as dt
import os
from typing import List, Dict, Any, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont, ImageOps
import config as cfg


TODAY = dt.date.today()

# === 路径配置（来自 config.py） ===
ROOT_DIR = Path(__file__).resolve().parent

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "output/inktime") or "output/inktime")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_PATH = Path(str(getattr(cfg, "FONT_PATH", "") or "")).expanduser()
if str(FONT_PATH) and not FONT_PATH.is_absolute():
    FONT_PATH = (ROOT_DIR / FONT_PATH).resolve()


def find_system_chinese_font() -> Optional[Path]:
    """尝试在系统字体目录中寻找常见的中文字体（Windows / macOS / Linux）。
    返回找到的字体路径或 None。
    """
    candidates = []
    # 优先使用 Windows 的楷体（SimKai）
    candidates += [
        r"C:\Windows\Fonts\simkai.ttf",  # 楷体
        r"C:\Windows\Fonts\kaiu.ttf",
        r"C:\Windows\Fonts\KAIU.TTF",
    ]
    # Windows 常见字体
    candidates += [
        r"C:\Windows\Fonts\msyh.ttc",  # 微软雅黑
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf",  # 黑体
        r"C:\Windows\Fonts\simsun.ttc",  # 宋体集合
        r"C:\Windows\Fonts\simsun.ttf",
    ]
    # macOS / 常见 Noto
    candidates += [
        "/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]

    for p in candidates:
        try:
            pp = Path(p)
            if pp.exists():
                return pp
        except Exception:
            continue
    return None

MEMORY_THRESHOLD = float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)
DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)

# 画布尺寸（按照片方向选择）
# 横屏画布（宽 x 高）
LANDSCAPE_CANVAS = (2048, 1536)
# 竖屏画布（宽 x 高）
PORTRAIT_CANVAS = (1536, 2048)

# 底部文字区域高度（像素）
TEXT_AREA_HEIGHT = 180


# ========== DB 与 EXIF 处理 ==========

def extract_date_from_exif(exif_json: Optional[str]) -> str:
    """
    从 EXIF JSON 中提取拍摄日期，返回 YYYY-MM-DD 格式，失败则返回空字符串。
    逻辑与 review_web.py 中保持一致。
    """
    if not exif_json:
        return ""
    try:
        data = json.loads(exif_json)
    except Exception:
        return ""
    dt_str = data.get("datetime")
    if not dt_str:
        return ""
    try:
        date_part = str(dt_str).split()[0]
        parts = date_part.replace(":", "-").split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""
    return ""


def load_sim_rows() -> List[Dict[str, Any]]:
    """
    加载 InkTime 用的核心字段：
    - path: 照片路径
    - exif_json: 用于解析日期 / GPS
    - side_caption: 文案
    - memory_score: 回忆度
    - exif_gps_lat / exif_gps_lon / exif_city: 地点信息（纯本地，不上网）
    """
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    rows = c.execute(
        """
        SELECT path,
               exif_json,
               side_caption,
               memory_score,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        WHERE exif_json IS NOT NULL
        """
    ).fetchall()
    conn.close()

    items: List[Dict[str, Any]] = []
    for path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city in rows:
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        # 再次兜底过滤 Screenshot 等
        if "screenshot" in str(path).lower():
            continue

        try:
            y, m, d = map(int, date_str.split("-"))
        except Exception:
            continue
        md = f"{m:02d}-{d:02d}"

        item = {
            "path": str(path),
            "date": date_str,  # YYYY-MM-DD
            "md": md,          # MM-DD
            "side": side_caption or "",
            "memory": float(memory_score) if memory_score is not None else -1.0,
            "lat": gps_lat,
            "lon": gps_lon,
            "city": exif_city or "",
        }
        items.append(item)

    return items


# ========== “历史上的今天”选片 ==========

def md_to_day_of_year(md: str) -> Optional[int]:
    """把 'MM-DD' 转成非闰年的第几天（1~365）。"""
    try:
        m, d = map(int, md.split("-"))
        days_before = [0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        if m < 1 or m > 12:
            return None
        return days_before[m] + d
    except Exception:
        return None


def day_of_year_to_md(day: int) -> str:
    # 选一个非闰年（2001/2005 随便），只依赖 day-of-year。
    base = dt.date(2001, 1, 1) + dt.timedelta(days=day - 1)
    return f"{base.month:02d}-{base.day:02d}"




def choose_photos_for_today(items: List[Dict[str, Any]], today: dt.date, count: int = 5) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    选片规则（多张版，按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，尽量随机选 count 张
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选回忆度最高的若干张作为兜底
    """
    if not items:
        raise RuntimeError("没有任何可用照片")

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD]
        if not candidates:
            continue

        # 随机选不重复的多张
        if len(candidates) >= count:
            chosen_list = random.sample(candidates, count)
        else:
            # 候选不足 count 张，用该日剩余的高分照片补齐
            chosen_list = list(candidates)
            for extra in arr:
                if extra in chosen_list:
                    continue
                chosen_list.append(extra)
                if len(chosen_list) >= count:
                    break

        info = {
            "target_md": target_md,
            "used_md": md,
            "day_offset": -offset,
            "candidate_count": len(candidates),
            "total_count_md": len(arr),
            "threshold": MEMORY_THRESHOLD,
            "fallback_global_max": False,
        }
        return chosen_list, info

    # 兜底：全局回忆度最高的若干张
    sorted_all = sorted(items, key=lambda x: x.get("memory", -1.0), reverse=True)
    chosen_list = sorted_all[:count]
    info = {
        "target_md": target_md,
        "used_md": chosen_list[0]["md"] if chosen_list else "",
        "day_offset": None,
        "candidate_count": len(chosen_list),
        "total_count_md": len(items),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
    }
    return chosen_list, info


def choose_photos_by_orientation(items: List[Dict[str, Any]], today: dt.date,
                                 landscape_count: int = 3, portrait_count: int = 3) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    目标：返回指定数量的横屏与竖屏照片（分别为 landscape_count / portrait_count）。
    策略：与 choose_photos_for_today 相同的按月日回溯查找 memory > MEMORY_THRESHOLD 的候选池，
    在候选池中根据实际图片尺寸区分横/竖并抽取所需数量；不足时从全局最高 memory 中补齐。
    返回 (chosen_list, info)，其中 chosen_list 长度为 landscape_count+portrait_count（不重复路径）。
    """
    if not items:
        raise RuntimeError("没有任何可用照片")

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    def is_landscape(path: str) -> Optional[bool]:
        try:
            p = Path(path)
            if not p.exists():
                return None
            im = Image.open(p)
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            return w >= h
        except Exception:
            return None

    pool: List[Dict[str, Any]] = []
    seen_paths = set()

    used_md = ""
    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD]
        if not candidates:
            continue

        # 将候选加入 pool（去重）
        for p in candidates:
            if p["path"] in seen_paths:
                continue
            pool.append(p)
            seen_paths.add(p["path"])

        # 统计当前 pool 中横/竖数量
        lands: List[Dict[str, Any]] = []
        ports: List[Dict[str, Any]] = []
        for p in pool:
            ori = is_landscape(p["path"])
            if ori is True:
                lands.append(p)
            elif ori is False:
                ports.append(p)
            # None（无法判断）则忽略

        if len(lands) >= landscape_count and len(ports) >= portrait_count:
            used_md = md
            break

    chosen: List[Dict[str, Any]] = []
    # 从 pool 中随机选取满足横竖要求
    lands = [p for p in pool if is_landscape(p["path"]) is True]
    ports = [p for p in pool if is_landscape(p["path"]) is False]

    if len(lands) >= landscape_count:
        chosen.extend(random.sample(lands, landscape_count))
    else:
        chosen.extend(lands)

    if len(ports) >= portrait_count:
        chosen.extend(random.sample(ports, portrait_count))
    else:
        chosen.extend(ports)

    # 如果不足，则从全局按 memory 取补齐（避免重复路径）
    if len(chosen) < (landscape_count + portrait_count):
        sorted_all = sorted(items, key=lambda x: x.get("memory", -1.0), reverse=True)
        for p in sorted_all:
            if p["path"] in {c["path"] for c in chosen}:
                continue
            # 判断方向并补到需要的分类
            ori = is_landscape(p["path"])
            if ori is True and sum(1 for c in chosen if is_landscape(c["path"]) is True) < landscape_count:
                chosen.append(p)
            elif ori is False and sum(1 for c in chosen if is_landscape(c["path"]) is False) < portrait_count:
                chosen.append(p)
            # 如果方向无法判断，则当作通用候选补齐任一不足类别
            elif ori is None:
                if sum(1 for c in chosen if is_landscape(c["path"]) is True) < landscape_count:
                    chosen.append(p)
                elif sum(1 for c in chosen if is_landscape(c["path"]) is False) < portrait_count:
                    chosen.append(p)
            if len(chosen) >= (landscape_count + portrait_count):
                break

    # 最终去重并修整顺序（先横后竖）
    final: List[Dict[str, Any]] = []
    seen = set()
    for c in chosen:
        if c["path"] in seen:
            continue
        final.append(c)
        seen.add(c["path"])

    # 如果仍然不足，用空列表补足（保持长度一致）
    # info 包含一些调试字段
    info = {
        "target_md": target_md,
        "used_md": used_md or (final[0]["md"] if final else ""),
        "requested_landscape": landscape_count,
        "requested_portrait": portrait_count,
        "returned_count": len(final),
        "threshold": MEMORY_THRESHOLD,
    }

    return final, info
# ========== 绘制 + 抖动 ==========


def wrap_text_chinese(draw: ImageDraw.ImageDraw,
                      text: str,
                      font: ImageFont.FreeTypeFont,
                      max_width: int,
                      max_lines: int) -> List[str]:
    """
    简单中文按字符宽度折行。
    """
    if not text:
        return []
    lines: List[str] = []
    line = ""
    for ch in text:
        test = line + ch
        w = draw.textlength(test, font=font)
        if w <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = ch
            if len(lines) >= max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)
    return lines


def format_date_display(date_str: str) -> str:
    """
    "YYYY-MM-DD" -> "YYYY.M.D"
    """
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) < 3:
        return date_str
    y = parts[0]
    try:
        m = str(int(parts[1]))
        d = str(int(parts[2]))
    except Exception:
        return date_str
    return f"{y}.{m}.{d}"


def format_location(lat, lon, city: str) -> str:
    """
    地点字符串：
    - 有 city 用 city
    - 否则如果有 lat/lon，用 "lat, lon"（5 位小数）
    - 否则空字符串（不写“未知地点”）
    """
    if city and str(city).strip():
        return str(city).strip()
    if lat is None or lon is None:
        return ""
    try:
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except Exception:
        return ""


def render_image(item: Dict[str, Any]) -> Image.Image:
    """
    - 上方图片：占 [0, CANVAS_HEIGHT - TEXT_AREA_HEIGHT)
    - 底部 TEXT_AREA_HEIGHT 像素为文字区：第一行 side 文案（最多两行），第二行日期 + 地点
    """
    # 根据原图方向选择画布尺寸：横屏使用 LANDSCAPE_CANVAS，竖屏使用 PORTRAIT_CANVAS
    img_path = Path(item["path"])
    if not img_path.exists():
        raise RuntimeError(f"图片不存在: {img_path}")
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img).convert("RGB")

    img_w, img_h = img.size
    # 横屏或方形视为横屏
    if img_w >= img_h:
        canvas_w, canvas_h = LANDSCAPE_CANVAS
    else:
        canvas_w, canvas_h = PORTRAIT_CANVAS

    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    if img_w == 0 or img_h == 0:
        raise RuntimeError(f"图片尺寸非法: {img.size}")

    # ---------- 照片区域 ----------
    img_area_w = canvas_w
    img_area_h = canvas_h - TEXT_AREA_HEIGHT  # 底部留给文字

    # “铺满裁剪”：缩放到至少覆盖区域，再从中间裁一块
    scale = max(img_area_w / img_w, img_area_h / img_h)
    draw_w = int(img_w * scale)
    draw_h = int(img_h * scale)

    img_resized = img.resize((draw_w, draw_h), Image.LANCZOS)

    left = max(0, (draw_w - img_area_w) // 2)
    top = max(0, (draw_h - img_area_h) // 2)
    right = left + img_area_w
    bottom = top + img_area_h
    img_cropped = img_resized.crop((left, top, right, bottom))

    # 贴到上方
    canvas.paste(img_cropped, (0, 0))

    # ---------- 底部文字区域 ----------
    padding_x = 48
    text_area_top = canvas_h - TEXT_AREA_HEIGHT + 16
    text_width = canvas_w - 2 * padding_x

    font_big = None
    font_small = None

    # 如果 config 中指定了有效的字体文件，则优先使用
    fp = str(FONT_PATH).strip()
    try:
        if fp and Path(fp).is_file():
            font_big = ImageFont.truetype(fp, 48)
            font_small = ImageFont.truetype(fp, 36)
    except Exception:
        font_big = None
        font_small = None

    # 未指定或加载失败时，尝试系统中文字体（优先 simkai）
    if font_big is None or font_small is None:
        sys_font = find_system_chinese_font()
        if sys_font:
            try:
                font_big = ImageFont.truetype(str(sys_font), 48)
                font_small = ImageFont.truetype(str(sys_font), 36)
                print(f"[INFO] 使用系统中文字体: {sys_font}")
            except Exception:
                font_big = ImageFont.load_default()
                font_small = ImageFont.load_default()
        else:
            font_big = ImageFont.load_default()
            font_small = ImageFont.load_default()

    side_text = item.get("side") or ""

    # 文案：最多两行，从 text_area_top 开始
    y = text_area_top
    if side_text:
        lines = wrap_text_chinese(draw, side_text, font_big, text_width, max_lines=2)
        line_height = int(font_big.size * 1.15)
        for line in lines:
            draw.text((padding_x, y), line, font=font_big, fill=(0, 0, 0))
            y += line_height

    # 日期 + 地点：固定在底部区域内靠近底边
    date_display = format_date_display(item["date"])
    loc_display = format_location(item.get("lat"), item.get("lon"), item.get("city") or "")

    second_line_y = canvas_h - int(font_small.size * 1.2) - 12
    draw.text((padding_x, second_line_y), date_display, font=font_small, fill=(0, 0, 0))

    loc_w = draw.textlength(loc_display, font=font_small)
    loc_x = padding_x + text_width - loc_w
    if loc_x < padding_x:
        loc_x = padding_x
    draw.text((loc_x, second_line_y), loc_display, font=font_small, fill=(0, 0, 0))

    return canvas

 


# ========== 主流程 ==========

def main():
    items = load_sim_rows()
    if not items:
        raise SystemExit("没有可用照片（exif_json 为空或解析失败）。")

    # 选 6 张：3 横屏，3 竖屏
    photos, info = choose_photos_by_orientation(items, TODAY, landscape_count=3, portrait_count=3)

    print("[INFO] 目标月日:", info.get("target_md"))
    print("[INFO] 实际使用月日:", info.get("used_md"))
    print("[INFO] 回溯天数(day_offset):", info.get("day_offset", "N/A"))
    print("[INFO] 候选数(>阈值):", info.get("candidate_count", "N/A"))
    print("[INFO] 当日总数:", info.get("total_count_md", "N/A"))
    print("[INFO] 使用兜底全局最大:", info.get("fallback_global_max", False))

    if not photos:
        raise SystemExit("选片结果为空。")

    import shutil

    # 对今天选出的多张照片逐一渲染
    for idx, chosen in enumerate(photos):
        print(f"[INFO] 第 {idx} 张选中照片:", chosen["path"])
        print("[INFO] 拍摄日期:", chosen["date"])
        print("[INFO] 回忆度:", chosen["memory"])
        # 额外调试信息：城市 / 经纬度 / 文案
        print("[DEBUG] 城市:", chosen.get("city", ""))
        print("[DEBUG] 经纬度:", chosen.get("lat"), chosen.get("lon"))
        print("[DEBUG] 文案:", chosen.get("side", ""))

        # 渲染成完整成品图（照片 + 文案 + 日期 + 地点）并保存预览 PNG
        img = render_image(chosen)
        preview_path = BIN_OUTPUT_DIR / f"preview_{idx}.png"
        img.save(preview_path)
        print(f"[OK] 已保存预览 PNG: {preview_path}")



if __name__ == "__main__":
    main()