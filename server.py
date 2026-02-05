#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from pathlib import Path
from flask import Flask, abort, send_file, Response, request, redirect
import mimetypes
import sqlite3
import json
import html
import config as cfg
from io import BytesIO
import render_daily_photo as rdp
import threading

ROOT_DIR = Path(__file__).resolve().parent

# --- config ---
DOWNLOAD_KEY = str(getattr(cfg, "DOWNLOAD_KEY", "") or "").strip()
if not DOWNLOAD_KEY:
    raise SystemExit("config.py 里没有配置 DOWNLOAD_KEY")

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "./photos.db") or "./photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "./output") or "./output")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FLASK_HOST = str(getattr(cfg, "FLASK_HOST", "0.0.0.0") or "0.0.0.0")
FLASK_PORT = int(getattr(cfg, "FLASK_PORT", 8765) or 8765)

# 是否开启照片库 WebUI（跑通后建议关闭，只保留 ESP32 下载接口）
ENABLE_REVIEW_WEBUI = bool(getattr(cfg, "ENABLE_REVIEW_WEBUI", True))

DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)
if DAILY_PHOTO_QUANTITY < 1:
    DAILY_PHOTO_QUANTITY = 1


# review 分页：每页 100 张
REVIEW_PAGE_SIZE = 100

# /review 日期筛选的可用 MM-DD 列表缓存（避免每次都扫全库）
_MD_CACHE: dict[str, object] = {"md_list": [], "built_at": 0.0}
_MD_CACHE_TTL_SEC = 300.0  # 5 分钟

# 用于浏览器刷新轮换今日多张候选图
_CYCLE_LOCK = threading.Lock()
# 使用字典为每个方向维护独立的状态
_CYCLE_STATES = {}  # {orientation: {'photos': [...], 'idx': int, 'built_at': float}}
_CYCLE_TTL_SEC = 300.0

# 记录上次获取照片的日期，用于判断是否需要重置
_LAST_PHOTO_DATE = ""

# 服务器启动时选好的照片
_STARTUP_SELECTED_PHOTOS = {}

def _load_all_md_list() -> list[str]:
    """从全库提取所有存在的 MM-DD（去重、排序）。用于前端“随机一天”。"""
    if not DB_PATH.exists():
        return []

    # 简单 TTL 缓存
    import time
    now = time.time()
    try:
        built_at = float(_MD_CACHE.get("built_at") or 0.0)
    except Exception:
        built_at = 0.0
    if (now - built_at) < _MD_CACHE_TTL_SEC:
        cached = _MD_CACHE.get("md_list")
        if isinstance(cached, list):
            return [str(x) for x in cached]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("SELECT exif_json FROM photo_scores").fetchall()
    conn.close()

    s: set[str] = set()
    for (exif_json,) in rows:
        d = extract_date_from_exif(exif_json)
        if d and len(d) >= 10:
            md = d[5:10]
            if len(md) == 5 and md[2] == "-":
                s.add(md)

    md_list = sorted(s)
    _MD_CACHE["md_list"] = md_list
    _MD_CACHE["built_at"] = now
    return md_list

app = Flask(__name__)
def _require_webui_enabled() -> None:
    if not ENABLE_REVIEW_WEBUI:
        abort(404)


def _safe_join(base: Path, rel: str) -> Path:
    """防目录穿越：只允许 base 下的相对路径"""
    p = (base / rel).resolve()
    if not str(p).startswith(str(base.resolve())):
        raise ValueError("path traversal blocked")
    return p


def _send_static_file(p: Path) -> Response:
    if not p.exists() or not p.is_file():
        abort(404)

    if p.suffix.lower() == ".bin":
        return send_file(p, mimetype="application/octet-stream", as_attachment=False)

    mt, _ = mimetypes.guess_type(str(p))
    if mt:
        return send_file(p, mimetype=mt, as_attachment=False)
    return send_file(p, as_attachment=False)


def _make_image_url(path_str: str) -> str:
    """
    把数据库里的本地图片路径转换成 HTTP 可访问的 /images/... 路径。
    要求图片在 IMAGE_DIR 目录下；不在则返回空，避免 file:// 污染与 canvas 跨域。
    """
    try:
        p = Path(path_str).expanduser().resolve()
        rel = p.relative_to(IMAGE_DIR.resolve())
        return "/images/" + str(rel).replace("\\", "/")
    except Exception:
        return ""


# --------------------------
# DB helpers
# --------------------------


def load_rows(page: int = 1, page_size: int = REVIEW_PAGE_SIZE, md: str = "", sort: str = "memory", month: str = "", day: str = ""):
    """分页读取 review 数据。支持按 MM-DD、月份或日期过滤与排序。返回 (rows, total_count)."""
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    if page < 1:
        page = 1
    if page_size < 1:
        page_size = REVIEW_PAGE_SIZE

    offset = (page - 1) * page_size

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 从 exif_json 里提取 datetime，再拼 MM-DD
    # 期望格式："YYYY:MM:DD HH:MM:SS"（extract_date_from_exif 也按这个假设）
    dt_expr = "json_extract(exif_json, '$.datetime')"
    md_expr = f"(substr({dt_expr}, 6, 2) || '-' || substr({dt_expr}, 9, 2))"
    month_expr = f"substr({dt_expr}, 6, 2)"  # 提取月份部分 (MM)
    day_expr = f"substr({dt_expr}, 9, 2)"    # 提取日期部分 (DD)

    where_parts = []
    params: list[object] = []

    # 优先处理 MM-DD 筛选（最高优先级）
    md = (md or "").strip()
    if md and len(md) == 5 and md[2] == "-":
        where_parts.append(f"{dt_expr} IS NOT NULL AND {md_expr} = ?")
        params.append(md)
    else:
        # 如果没有 MM-DD 筛选，则处理月份和日期筛选
        month = (month or "").strip()
        day = (day or "").strip()

        # 如果同时提供了月份和日期，则组合成 MM-DD 格式
        if month and day and len(month) == 2 and len(day) == 2:
            combined_md = f"{month}-{day}"
            where_parts.append(f"{dt_expr} IS NOT NULL AND {md_expr} = ?")
            params.append(combined_md)
        else:
            # 只有月份或只有日期的情况下分别处理
            if month and len(month) == 2:
                where_parts.append(f"{dt_expr} IS NOT NULL AND {month_expr} = ?")
                params.append(month)
            if day and len(day) == 2:
                where_parts.append(f"{dt_expr} IS NOT NULL AND {day_expr} = ?")
                params.append(day)

    # 组合 WHERE 子句
    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    # total_count 也要跟随过滤
    if where_parts:
        total_count = c.execute(f"SELECT COUNT(1) FROM photo_scores {where_sql}", params).fetchone()[0]
    else:
        total_count = c.execute("SELECT COUNT(1) FROM photo_scores").fetchone()[0]

    # 排序
    sort = (sort or "memory").strip()
    if sort == "beauty":
        order_sql = "ORDER BY COALESCE(beauty_score, -1) DESC, COALESCE(memory_score, -1) DESC, path"
    elif sort == "time_new":
        # 直接按 datetime 字符串排序（固定格式下可按字典序比较）；NULL 放最后
        order_sql = f"ORDER BY ({dt_expr} IS NULL) ASC, {dt_expr} DESC, path"
    elif sort == "time_old":
        order_sql = f"ORDER BY ({dt_expr} IS NULL) ASC, {dt_expr} ASC, path"
    else:
        # 默认 memory
        order_sql = "ORDER BY COALESCE(memory_score, -1) DESC, COALESCE(beauty_score, -1) DESC, path"

    base_sql = f"""
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               side_caption
        FROM photo_scores
        {where_sql}
        {order_sql}
        LIMIT ? OFFSET ?
    """

    q_params = list(params) + [page_size, offset]
    rows = c.execute(base_sql, q_params).fetchall()

    conn.close()
    return rows, int(total_count)


def load_sim_rows():
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    rows = c.execute(
        """
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               side_caption,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        """
    ).fetchall()

    conn.close()
    return rows


# 新增：只加载指定日期集合的照片，加速 /sim
def load_sim_rows_for_dates(dates: list[str]):
    """只加载指定日期（YYYY-MM-DD）集合内的照片，用于 /sim 加速。"""
    if not dates:
        return []
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    # 过滤掉不合法日期字符串，避免 SQL 注入（虽然我们用参数化，但也别喂垃圾）
    safe_dates = []
    for d in dates:
        d = (d or "").strip()
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            safe_dates.append(d)
    if not safe_dates:
        return []

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    dt_expr = "json_extract(exif_json, '$.datetime')"
    # exif datetime 形如 YYYY:MM:DD HH:MM:SS，取前 10 位并把 : 替换成 - -> YYYY-MM-DD
    date_expr = f"replace(substr({dt_expr}, 1, 10), ':', '-')"

    placeholders = ",".join(["?"] * len(safe_dates))
    sql = f"""
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               side_caption,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        WHERE {dt_expr} IS NOT NULL
          AND {date_expr} IN ({placeholders})
    """

    rows = c.execute(sql, tuple(safe_dates)).fetchall()
    conn.close()
    return rows

def get_photo_meta_by_path(abs_path: str):
    """
    从 DB 找到渲染需要的字段：date/side/lat/lon/city。
    abs_path 必须是数据库里 photo_scores.path 的原值（通常是绝对路径）。
    """
    if not DB_PATH.exists():
        return None

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute(
        """
        SELECT path,
               exif_json,
               side_caption,
               memory_score,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        WHERE path = ?
        LIMIT 1
        """,
        (abs_path,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city = row
    date_str = extract_date_from_exif(exif_json)
    if not date_str:
        return None

    return {
        "path": str(path),
        "date": date_str,
        "side": side_caption or "",
        "memory": float(memory_score) if memory_score is not None else None,
        "lat": gps_lat,
        "lon": gps_lon,
        "city": exif_city or "",
    }

def summarize_exif(exif_json: str | None) -> str:
    if not exif_json:
        return ""

    try:
        data = json.loads(exif_json)
    except Exception:
        return ""

    dtv = data.get("datetime")
    make = data.get("make")
    model = data.get("model")
    iso = data.get("iso")
    exp = data.get("exposure_time")
    fnum = data.get("f_number")
    fl = data.get("focal_length")
    lat = data.get("gps_lat")
    lon = data.get("gps_lon")

    parts = []
    if dtv:
        parts.append(f"时间: {dtv}")
    if make or model:
        cam = f"{make or ''} {model or ''}".strip()
        if cam:
            parts.append(f"设备: {cam}")
    exp_parts = []
    if iso:
        exp_parts.append(f"ISO {iso}")
    if exp:
        exp_parts.append(f"快门 {exp}")
    if fnum:
        exp_parts.append(f"光圈 {fnum}")
    if fl:
        exp_parts.append(f"焦距 {fl}")
    if exp_parts:
        parts.append(" / ".join(exp_parts))
    if lat is not None and lon is not None:
        try:
            parts.append(f"GPS: {float(lat):.5f}, {float(lon):.5f}")
        except Exception:
            parts.append(f"GPS: {lat}, {lon}")

    return "；".join(str(p) for p in parts if p)


def extract_date_from_exif(exif_json: str | None) -> str:
    if not exif_json:
        return ""
    try:
        data = json.loads(exif_json)
    except Exception:
        return ""
    dtv = data.get("datetime")
    if not dtv:
        return ""
    try:
        date_part = str(dtv).split()[0]  # "2018:03:18"
        parts = date_part.replace(":", "-").split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""
    return ""


# --------------------------
# HTML builders
# --------------------------

def build_html(rows, page: int, page_size: int, total_count: int):
    items_html = []

    for path, caption, ptype, m_score, b_score, reason, exif_json, width, height, orientation, used_at, side_caption in rows:
        safe_caption = html.escape(caption or "").replace("\n", "<br>")
        safe_side = html.escape(side_caption or "").replace("\n", "<br>")
        safe_type = html.escape(ptype or "")
        safe_reason = html.escape(reason or "")
        exif_summary = summarize_exif(exif_json)
        safe_exif = html.escape(exif_summary or "")

        date_str = extract_date_from_exif(exif_json)
        safe_date = html.escape(date_str or "")

        md_str = ""
        if date_str and len(date_str) >= 10:
            md_str = date_str[5:10]
        safe_md = html.escape(md_str or "")

        res_str = ""
        if width and height:
            try:
                res_str = f"{int(width)} x {int(height)}"
            except Exception:
                res_str = f"{width} x {height}"
        orient_str = orientation or ""
        used_str = used_at or ""

        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        score_html = ""
        if m_score is not None or b_score is not None:
            parts = []
            if m_score is not None:
                parts.append(f"回忆度: {m_score:.1f}")
            if b_score is not None:
                parts.append(f"美观度: {b_score:.1f}")
            score_line = " / ".join(parts)
            score_html = f'<div class="score">{score_line}</div>'

        type_html = f'<div class="type">类型: {safe_type}</div>' if safe_type else ""
        exif_html = f'<div class="exif">{safe_exif}</div>' if safe_exif else ""
        reason_html = f'<div class="reason">理由: {safe_reason}</div>' if safe_reason else ""

        items_html.append(f"""
        <div class="item"
             data-date="{safe_date}"
             data-md="{safe_md}"
             data-memory="{m_score if m_score is not None else ''}"
             data-beauty="{b_score if b_score is not None else ''}">
            <div class="img-wrap">
                <a class="img-link" href="/sim?img={html.escape(img_uri)}" title="打开该照片的模拟器" onclick="window.stop();">
                    <img src="{img_uri}" loading="lazy">
                </a>
            </div>
            {f'<div class="side-under">{safe_side}</div>' if safe_side else ''}
            <div class="meta">
                <div class="path">{html.escape(str(path))}</div>
                {type_html}
                {score_html}
                {reason_html}
                {exif_html}
                <div class="extra">
                    {f"拍摄日期: {safe_date}" if safe_date else ""}
                    {(" · 分辨率: " + html.escape(res_str)) if res_str else ""}
                    {(" · 方向: " + html.escape(orient_str)) if orient_str else ""}
                    {(" · 已上屏: " + html.escape(used_str)) if used_str else ""}
                </div>
                <div class="caption">{safe_caption}</div>
            </div>
        </div>
        """)

    items_str = "\n".join(items_html)
    total_pages = (total_count + page_size - 1) // page_size

    # 从请求参数回填（用于显示）
    md_q = (request.args.get("md", "") or "").strip()
    sort_q = (request.args.get("sort", "") or "memory").strip() or "memory"
    md_hint = f" · 筛选日期 {html.escape(md_q)}" if (md_q and len(md_q) == 5) else ""

    html_str = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>InkTime照片数据库</title>
  <style>
    :root{{
      --bg: #0b0c10;
      --panel: rgba(255,255,255,0.06);
      --card: rgba(255,255,255,0.10);
      --card2: rgba(255,255,255,0.08);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --muted2: rgba(255,255,255,0.48);
      --line: rgba(255,255,255,0.14);
      --accent: #8ab4ff;
      --accent2:#9cffd6;
      --shadow: 0 18px 60px rgba(0,0,0,0.45);
      --shadow2: 0 10px 28px rgba(0,0,0,0.35);
      --radius: 14px;
    }}
    body{{
      margin:0;
      padding:0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
      background: #9FB8A9; /* 豆沙绿 */
      color: var(--text);
    }}
    .container{{
      max-width: 1320px;
      margin: 26px auto 60px;
      padding: 0 18px;
    }}
    h1{{
      font-size: 22px;
      margin: 0 0 8px;
      letter-spacing: 0.2px;
    }}
    .subtitle{{
      font-size: 13px;
      color: var(--muted);
      margin: 0 0 14px;
      line-height: 1.35;
    }}

    .controls{{
      display:flex;
      flex-wrap:wrap;
      gap: 10px;
      align-items:center;
      margin: 12px 0 14px;
      font-size: 13px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .controls label{{
      display:inline-flex;
      align-items:center;
      gap: 8px;
      color: var(--muted);
      white-space: nowrap;
    }}
    .controls select{{
      padding: 7px 10px;
      font-size: 13px;
      color: #ffffff;  /* 白色文字，确保可见性 */
      background: rgba(0, 0, 0, 0.45);  /* 深色背景，提高对比度 */
      border: 1px solid rgba(255,255,255,0.20);
      border-radius: 10px;
      outline: none;
    }}
    .controls select option{{
      background: #1a1a1a;  /* 下拉选项的深色背景 */
      color: #ffffff;       /* 下拉选项的白色文字 */
    }}
    .controls select:focus{{
      border-color: rgba(138,180,255,0.7);
      box-shadow: 0 0 0 3px rgba(138,180,255,0.16);
    }}
    .controls button{{
      padding: 7px 12px;
      font-size: 13px;
      cursor: pointer;
      color: var(--text);
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      transition: transform .08s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
    }}
    .controls button:hover{{
      background: rgba(255,255,255,0.14);
      border-color: rgba(255,255,255,0.26);
    }}
    .controls button:active{{
      transform: translateY(1px);
    }}
    .controls button:disabled{{
      opacity: 0.45;
      cursor: not-allowed;
    }}
    .controls.pager{{
      background: rgba(255,255,255,0.05);
    }}

    .status{{
      font-size: 12px;
      color: var(--muted);
      margin: 8px 0 12px;
    }}

    .grid{{
      display:grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 16px;
    }}
    .item{{
      background: linear-gradient(180deg, var(--card) 0%, var(--card2) 100%);
      border: 1px solid rgba(255,255,255,0.14);
      border-radius: var(--radius);
      overflow: hidden;
      box-shadow: var(--shadow2);
      display:flex;
      flex-direction:column;
      transition: transform .12s ease, border-color .15s ease, box-shadow .15s ease;
    }}
    .item:hover{{
      transform: translateY(-2px);
      border-color: rgba(138,180,255,0.38);
      box-shadow: var(--shadow);
    }}

    .img-wrap{{
      width:100%;
      background: rgba(0,0,0,0.55);
      display:flex;
      align-items:center;
      justify-content:center;
      max-height: 260px;
      overflow:hidden;
    }}
    .img-wrap img{{
      width:100%;
      height:auto;
      display:block;
      object-fit: cover;
      filter: saturate(1.04) contrast(1.02);
    }}
    .img-link{{ display:block; width:100%; }}
    .img-link:link, .img-link:visited{{ text-decoration:none; }}

    .side-under{{
      padding: 10px 12px 0;
      font-size: 12px;
      color: var(--text);
      line-height: 1.45;
      word-break: break-word;
      opacity: 0.92;
    }}

    .meta{{
      padding: 10px 12px 12px;
      font-size: 13px;
      color: var(--text);
    }}
    .path{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 6px;
      word-break: break-all;
    }}
    .type{{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .score{{
      font-size: 13px;
      font-weight: 650;
      margin-bottom: 6px;
      color: var(--accent2);
    }}
    .reason{{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      line-height: 1.45;
    }}
    .exif{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 8px;
      line-height: 1.45;
    }}
    .extra{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 8px;
      line-height: 1.45;
    }}
    .caption{{
      margin-top: 6px;
      font-size: 13px;
      line-height: 1.55;
      color: var(--text);
    }}

    /* 横屏模式下的自适应 */
    @media screen and (orientation: landscape) {{
      .grid {{
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); /* 横屏时稍微增加卡片最小宽度 */
      }}
      .container {{
        max-width: 1400px; /* 横屏时稍微增加最大宽度 */
      }}
    }}

    /* 竖屏模式下的自适应 */
    @media screen and (orientation: portrait) {{
      .grid {{
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); /* 竖屏时减少卡片最小宽度 */
      }}
      .container {{
        margin: 20px auto 50px; /* 竖屏时调整边距 */
        padding: 0 12px;
      }}
      .controls {{
        flex-direction: column; /* 竖屏时控制元素垂直排列 */
        align-items: stretch;
      }}
      .controls label {{
        justify-content: flex-start;
      }}
    }}

    /* 小屏幕设备的适配 */
    @media (max-width: 560px) {{
      .container{{ padding: 0 14px; }}
      .grid{{ grid-template-columns: 1fr; }}
      .controls{{ gap: 8px; }}

      /* 竖屏小设备进一步优化 */
      @media screen and (orientation: portrait) {{
        .grid {{
          grid-template-columns: 1fr; /* 小屏竖屏强制单列 */
        }}
      }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>InkTime照片数据库</h1>
    <div class="subtitle">
      数据库：{html.escape(str(DB_PATH))}{md_hint} · 当前页 {page} · 本页 {len(rows)} 张 · 总计 {total_count} 张（每页 {page_size} 张）
    </div>

    <div class="controls">
      <label>
        月份：
        <select id="monthFilter">
          <option value="">全部</option>
          <option value="01">1 月</option><option value="02">2 月</option><option value="03">3 月</option>
          <option value="04">4 月</option><option value="05">5 月</option><option value="06">6 月</option>
          <option value="07">7 月</option><option value="08">8 月</option><option value="09">9 月</option>
          <option value="10">10 月</option><option value="11">11 月</option><option value="12">12 月</option>
        </select>
      </label>
      <label>
        日期：
        <select id="dayFilter">
          <option value="">全部</option>
          {''.join([f'<option value="{i:02d}">{i} 日</option>' for i in range(1, 32)])}
        </select>
      </label>
      <label>
        排序：
        <select id="sortBy">
          <option value="memory">按回忆度</option>
          <option value="beauty">按美观度</option>
          <option value="time_new">按时间（新→旧）</option>
          <option value="time_old">按时间（旧→新）</option>
        </select>
      </label>
      <button type="button" id="randomDateBtn">随机一天</button>
      <button type="button" id="homeBtn">回到首页</button>
    </div>

    <div class="controls pager" style="justify-content: space-between;">
      <div>
        <button type="button" id="prevPageBtn">上一页</button>
        <button type="button" id="nextPageBtn">下一页</button>
      </div>
      <div class="subtitle" style="margin:0;">第 <span id="pageNum">{page}</span> 页 / 共 <span id="pageTotal">{total_pages}</span> 页</div>
    </div>

    <div class="status" id="statusLine"></div>

    <div class="grid">
      {items_str}
    </div>

    <div class="controls pager" style="justify-content: space-between; margin-top: 18px;">
      <div>
        <button type="button" id="prevPageBtnBottom">上一页</button>
        <button type="button" id="nextPageBtnBottom">下一页</button>
      </div>
      <div class="subtitle" style="margin:0;">第 <span>{page}</span> 页 / 共 <span>{total_pages}</span> 页</div>
    </div>
  </div>

  <script>
    document.addEventListener('DOMContentLoaded', function () {{
      const monthSelect = document.getElementById('monthFilter');
      const daySelect = document.getElementById('dayFilter');
      const sortSelect = document.getElementById('sortBy');
      const statusLine = document.getElementById('statusLine');
      const randomBtn = document.getElementById('randomDateBtn');
      const homeBtn = document.getElementById('homeBtn');

      const currentPage = {page};
      const totalPages = {total_pages};
      const prevBtn = document.getElementById('prevPageBtn');
      const nextBtn = document.getElementById('nextPageBtn');
      const prevBtnBottom = document.getElementById('prevPageBtnBottom');
      const nextBtnBottom = document.getElementById('nextPageBtnBottom');

      // 任何跳转前先中断当前页面的图片/资源加载，避免请求排队导致“点击无响应”
      function navigateTo(urlStr) {{
        try {{
          window.stop();
        }} catch (e) {{
          // ignore
        }}
        window.location.href = urlStr;
      }}

      function getParams() {{
        const url = new URL(window.location.href);
        const md = (url.searchParams.get('md') || '').trim();
        const month = (url.searchParams.get('month') || '').trim();
        const day = (url.searchParams.get('day') || '').trim();
        const sort = (url.searchParams.get('sort') || '').trim() || 'memory';
        const page = parseInt(url.searchParams.get('page') || '1', 10) || 1;
        return {{ url, md, month, day, sort, page }};
      }}

      function setSelectsFromUrl() {{
        const p = getParams();
        // sort
        if (sortSelect) sortSelect.value = p.sort;
        // 检查是否有月份或日期参数
        if (p.month || p.day) {{
          if (monthSelect) monthSelect.value = p.month;
          if (daySelect) daySelect.value = p.day;
          if (statusLine) {{
            let filterDesc = '当前筛选：';
            if (p.month) filterDesc += '月份 ' + p.month;
            if (p.day) filterDesc += (p.month ? ' & ' : '') + '日期 ' + p.day;
            filterDesc += '（全库）';
            statusLine.textContent = filterDesc;
          }}
        }}
        // md -> month/day (保持向后兼容)
        else if (p.md && p.md.length === 5 && p.md.indexOf('-') === 2) {{
          const parts = p.md.split('-');
          if (parts.length === 2) {{
            if (monthSelect) monthSelect.value = parts[0];
            if (daySelect) daySelect.value = parts[1];
          }}
          if (statusLine) statusLine.textContent = '当前筛选：' + p.md + '（全库）';
        }} else {{
          if (monthSelect) monthSelect.value = '';
          if (daySelect) daySelect.value = '';
          if (statusLine) statusLine.textContent = '';
        }}
      }}

      function buildReviewUrl(md, sort, page) {{
        // 获取当前 URL 参数，包括月份和日期
        const url = new URL(window.location.href);
        url.pathname = '/review';
        if (md && md.length === 5 && md.indexOf('-') === 2) url.searchParams.set('md', md);
        else url.searchParams.delete('md');
        if (sort) url.searchParams.set('sort', sort);
        else url.searchParams.delete('sort');
        url.searchParams.set('page', String(page || 1));
        return url.toString();
      }}

      function buildReviewUrlWithFilters(month, day, sort, page) {{
        const url = new URL(window.location.href);
        url.pathname = '/review';
        // 设置月份和日期参数
        if (month && month.length === 2) url.searchParams.set('month', month);
        else url.searchParams.delete('month');
        if (day && day.length === 2) url.searchParams.set('day', day);
        else url.searchParams.delete('day');
        // 删除 md 参数，避免冲突
        url.searchParams.delete('md');
        if (sort) url.searchParams.set('sort', sort);
        else url.searchParams.delete('sort');
        url.searchParams.set('page', String(page || 1));
        return url.toString();
      }}

      function goPage(p) {{
        const params = getParams();

        // 如果有月份或日期参数，使用 buildReviewUrlWithFilters
        if (params.month || params.day) {{
          navigateTo(buildReviewUrlWithFilters(params.month, params.day, params.sort, p));
        }} else {{
          // 否则使用原有的 buildReviewUrl
          navigateTo(buildReviewUrl(params.md, params.sort, p));
        }}
      }}

      function goHome() {{
        const params = getParams();
        // 清除月份、日期和 md 参数，返回到完整列表
        navigateTo(buildReviewUrl('', params.sort || 'memory', 1));
      }}

      async function pickRandomDate() {{
        // 从后端拿“真实存在的日期集合”，前端随机一个，然后让后端按 md 过滤
        try {{
          // 先停止当前页面的图片加载，释放连接
          try {{ window.stop(); }} catch (e) {{}}
          const resp = await fetch('/api/md_list');
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          const data = await resp.json();
          const arr = Array.isArray(data) ? data : (Array.isArray(data.md_list) ? data.md_list : []);
          if (!arr.length) {{
            if (statusLine) statusLine.textContent = '全库没有任何可用日期（exif datetime 缺失）。';
            return;
          }}
          const idx = Math.floor(Math.random() * arr.length);
          const md = String(arr[idx] || '').trim();
          const params = getParams();
          navigateTo(buildReviewUrl(md, params.sort || 'memory', 1));
        }} catch (e) {{
          if (statusLine) statusLine.textContent = '随机失败：' + e;
        }}
      }}

      function onMonthDayChange() {{
        const mVal = (monthSelect && monthSelect.value) ? monthSelect.value : '';
        const dVal = (daySelect && daySelect.value) ? daySelect.value : '';
        const sortBy = (sortSelect && sortSelect.value) ? sortSelect.value : 'memory';

        if (!mVal && !dVal) {{
          navigateTo(buildReviewUrl('', sortBy, 1));
          return;
        }}

        if (mVal && dVal) {{
          // 同时选择了月份和日期
          const md = mVal + '-' + dVal;
          navigateTo(buildReviewUrl(md, sortBy, 1));
          return;
        }}

        if (mVal && !dVal) {{
          // 只选择了月份
          navigateTo(buildReviewUrlWithFilters(mVal, '', sort, 1));
          return;
        }}

        if (!mVal && dVal) {{
          // 只选择了日期
          navigateTo(buildReviewUrlWithFilters('', dVal, sort, 1));
          return;
        }}
      }}

      function onSortChange() {{
        const params = getParams();
        const sortBy = (sortSelect && sortSelect.value) ? sortSelect.value : 'memory';

        // 如果有月份或日期参数，使用 buildReviewUrlWithFilters
        if (params.month || params.day) {{
          navigateTo(buildReviewUrlWithFilters(params.month, params.day, sortBy, 1));
        }} else {{
          // 否则使用原有的 buildReviewUrl
          navigateTo(buildReviewUrl(params.md, sortBy, 1));
        }}
      }}

      // 分页按钮
      if (prevBtn) {{
        prevBtn.disabled = currentPage <= 1;
        prevBtn.addEventListener('click', () => goPage(Math.max(1, currentPage - 1)));
      }}
      if (nextBtn) {{
        nextBtn.disabled = currentPage >= totalPages;
        nextBtn.addEventListener('click', () => goPage(Math.min(totalPages, currentPage + 1)));
      }}
      if (prevBtnBottom) {{
        prevBtnBottom.disabled = currentPage <= 1;
        prevBtnBottom.addEventListener('click', () => goPage(Math.max(1, currentPage - 1)));
      }}
      if (nextBtnBottom) {{
        nextBtnBottom.disabled = currentPage >= totalPages;
        nextBtnBottom.addEventListener('click', () => goPage(Math.min(totalPages, currentPage + 1)));
      }}

      if (monthSelect) monthSelect.addEventListener('change', onMonthDayChange);
      if (daySelect) daySelect.addEventListener('change', onMonthDayChange);
      if (sortSelect) sortSelect.addEventListener('change', onSortChange);
      if (randomBtn) randomBtn.addEventListener('click', pickRandomDate);
      if (homeBtn) homeBtn.addEventListener('click', goHome);

      // 兜底：用户在图片疯狂加载时点击任何链接/按钮，先 stop()，避免导航请求排队
      document.addEventListener('click', function (ev) {{
        const t = ev.target;
        if (!t) return;
        const a = t.closest ? t.closest('a') : null;
        const btn = t.closest ? t.closest('button') : null;
        // 只要是链接或按钮点击，就先中断当前加载
        if (a || btn) {{
          try {{ window.stop(); }} catch (e) {{}}
        }}
      }}, true);

      setSelectsFromUrl();
    }});
  </script>
</body>
</html>
"""
    return html_str


def build_simulator_html(sim_rows, selected_img: str = ""):
    # 空数据时不要做任何无意义的循环，避免前端 JS 大对象
    if not sim_rows:
        sim_rows = []
    items = []

    def _parse_tags(ptype_val) -> list[str]:
        """把 DB 的 type 字段解析成 tag 数组。
        兼容三种常见存储：
        - JSON 数组：   ["人物","日常"]
        - 伪数组文本：  [人物, 日常] / [人物，日常]
        - 普通字符串：  人物,日常 / 人物
        注意：这里是容错解析，目的是不让 /sim 因坏数据 500。
        """
        if ptype_val is None:
            return []
        s = str(ptype_val).strip()
        if not s:
            return []

        # 1) 先尝试严格 JSON
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    out = []
                    for x in arr:
                        t = str(x).strip()
                        if t:
                            out.append(t)
                    return out
            except Exception:
                # JSON 不合法：继续走容错
                pass

        # 2) 容错：去掉最外层 [] 以及引号，然后按逗号/中文逗号切
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1].strip()

        # 去掉可能出现的引号
        s = s.replace('"', '').replace("'", "")

        parts = [p.strip() for p in s.replace('，', ',').split(',')]
        out = [p for p in parts if p]
        return out
    for (
        path,
        caption,
        ptype,
        memory_score,
        beauty_score,
        reason,
        side_caption,
        exif_json,
        width,
        height,
        orientation,
        used_at,
        gps_lat,
        gps_lon,
        exif_city,
    ) in sim_rows:
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        # tags: 保证为数组，优先解析 JSON/容错
        type_value = _parse_tags(ptype)

        items.append({
            "path": img_uri,
            "date": date_str,
            "memory": float(memory_score) if memory_score is not None else None,
            "beauty": float(beauty_score) if beauty_score is not None else None,
            "city": exif_city or "",
            "lat": gps_lat,
            "lon": gps_lon,
            "side": side_caption or "",
            "caption": caption or "",
            "type": type_value,
            "reason": reason or "",
            "exif_json": exif_json or "",
            "exif_summary": summarize_exif(exif_json) if exif_json else "",
            "width": width if width is not None else "",
            "height": height if height is not None else "",
            "orientation": orientation or "",
            "used_at": used_at or "",
        })

    data_json = json.dumps(items, ensure_ascii=False).replace("</", "<\\/") if items else "[]"
    selected_json = json.dumps(selected_img or "", ensure_ascii=False).replace("</", "<\\/")

    html_str = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>墨水屏渲染效果预览</title>
  <style>
    :root {{
      --bg: #0b0c10;
      --panel: rgba(255,255,255,0.06);
      --line: rgba(255,255,255,0.14);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --muted2: rgba(255,255,255,0.48);
      --accent: #8ab4ff;
      --accent2: #9cffd6;
      --shadow: 0 18px 60px rgba(0,0,0,0.45);
      --shadow2: 0 10px 28px rgba(0,0,0,0.35);
      --radius: 14px;
    }}
    body {{
      margin:0; padding:0;
      font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
      background: #9FB8A9; /* 豆沙绿 */
      color: var(--text);
    }}
    .container {{
      max-width: 1120px;
      margin: 22px auto 42px;
      padding: 0 16px;
    }}
    a.back {{
      display:inline-block;
      margin-bottom: 10px;
      color: var(--accent);
      text-decoration: none;
    }}
    h1 {{
      font-size: 22px;
      margin: 0 0 8px;
      letter-spacing: 0.2px;
    }}
    .subtitle {{
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 14px;
      line-height: 1.45;
    }}
    .controls {{
      display:flex;
      align-items:center;
      gap: 10px;
      margin-bottom: 14px;
      font-size: 13px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .controls button {{
      padding: 7px 12px;
      font-size: 13px;
      cursor: pointer;
      color: var(--text);
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      transition: transform .08s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
    }}
    .controls button:hover {{
      background: rgba(255,255,255,0.14);
      border-color: rgba(255,255,255,0.26);
    }}
    .controls button:active {{
      transform: translateY(1px);
    }}

    .status {{
      font-size: 12px;
      color: var(--muted);
      margin: 6px 0 10px;
      min-height: 16px;
    }}

    .preview-wrap {{
      display:flex;
      flex-direction:column;
      gap: 18px;
      align-items: center;
    }}
    .canvas-box {{
      width: 100%;
      display:flex;
      justify-content: center;
      background: transparent;
      border: none;
      border-radius: 0;
      padding: 0;
      box-shadow: none;
      backdrop-filter: none;
    }}
    .canvas-box h2 {{
      font-size: 13px;
      margin: 0 0 8px;
      color: rgba(255,255,255,0.78);
    }}
    #previewCanvas {{
      display:block;
      background: transparent;
      border: none;
      border-radius: 8px;
      width: 100%;
      height: auto;
      max-width: 960px;
    }}

    .meta-box {{
      width: 100%;
      max-width: 960px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 14px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
      font-size: 16px;
      line-height: 1.6;
    }}
    .meta-title {{
      font-size: 13px;
      color: rgba(255,255,255,0.78);
      margin: 0 0 10px;
    }}
    .kpi {{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(255,255,255,0.10);
    }}
    .kpi .cell {{
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px;
    }}
    .kpi .label {{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 4px;
    }}
    .kpi .value {{
      font-size: 16px;
      font-weight: 700;
      color: var(--text);
      line-height: 1.2;
      word-break: break-word;
    }}
    .kpi .value.accent {{
      color: var(--accent2);
    }}

    .field {{
      display:flex;
      gap: 10px;
      margin-bottom: 8px;
      line-height: 1.45;
      font-size: 12px;
    }}
    .field .label {{
      width: 92px;
      flex: 0 0 92px;
      color: var(--muted2);
    }}
    .field .value {{
      flex: 1;
      color: var(--text);
      word-break: break-word;
    }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 11px;
      color: rgba(255,255,255,0.80);
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(0,0,0,0.22);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px;
    }}

    .section {{
      padding-top: 10px;
      margin-top: 10px;
      border-top: 1px solid rgba(255,255,255,0.10);
    }}
    .section:first-of-type {{
      padding-top: 0;
      margin-top: 0;
      border-top: none;
    }}
    .section-title {{
      display:flex;
      align-items:center;
      justify-content: space-between;
      gap: 10px;
      font-size: 12px;
      color: rgba(255,255,255,0.78);
      margin: 0 0 8px;
      letter-spacing: .2px;
    }}

    .chips {{
      display:flex;
      flex-wrap:wrap;
      gap: 8px;
      margin: 12px 0 14px;
    }}
    .chip {{
      display:inline-flex;
      align-items:center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      line-height: 1;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(255,255,255,0.07);
      color: rgba(255,255,255,0.92);
      user-select: none;
    }}
    .chip-dot {{
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.7);
      flex: 0 0 8px;
    }}

    .big-text {{
      font-size: 13px;
      line-height: 1.55;
      color: rgba(255,255,255,0.92);
      padding: 10px 12px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      word-break: break-word;
      white-space: pre-wrap;
    }}

    details.fold {{
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px 12px;
      margin-top: 18px;
      font-size: 14px;
    }}
    details.fold > summary {{
      cursor: pointer;
      list-style: none;
      outline: none;
      color: rgba(255,255,255,0.86);
      font-size: 12px;
      display:flex;
      align-items:center;
      justify-content: space-between;
      gap: 10px;
    }}
    details.fold > summary::-webkit-details-marker {{ display: none; }}
    .fold-hint {{
      color: rgba(255,255,255,0.55);
      font-size: 11px;
    }}

    .kv-grid {{
      display:grid;
      grid-template-columns: 92px 1fr;
      gap: 8px 10px;
      margin-top: 10px;
      font-size: 12px;
      line-height: 1.45;
    }}
    .kv-k {{
      color: rgba(255,255,255,0.52);
    }}
    .kv-v {{
      color: rgba(255,255,255,0.92);
      word-break: break-word;
    }}

    .hero-text {{
      font-size: 26px;
      line-height: 1.7;
      font-weight: 650;
      margin-bottom: 18px;
      color: rgba(255,255,255,0.98);
      word-break: break-word;
      white-space: pre-wrap;
    }}

    .sub-text {{
      font-size: 17px;
      line-height: 1.8;
      color: rgba(255,255,255,0.90);
      margin: 14px 0 18px;
      word-break: break-word;
      white-space: pre-wrap;
    }}

    .score-bars {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      margin: 18px 0 20px;
    }}

    .score-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 14px;
      color: rgba(255,255,255,0.75);
    }}

    .score-track {{
      position: relative;
      flex: 1;
      height: 10px;
      background: rgba(255,255,255,0.12);
      border-radius: 999px;
      overflow: hidden;
    }}

    .score-fill {{
      position: absolute;
      left: 0; top: 0; bottom: 0;
      width: 0%;
      border-radius: 999px;
    }}

    .score-fill.memory {{ background: linear-gradient(90deg, #6fd6ff, #9cffd6); }}
    .score-fill.beauty {{ background: linear-gradient(90deg, #ffd36f, #ff9f6f); }}

    .score-num {{
      width: 44px;
      text-align: right;
      font-variant-numeric: tabular-nums;
      color: rgba(255,255,255,0.9);
    }}

    #fieldReason {{
      font-size: 16px;
      line-height: 1.8;
      margin-top: 6px;
    }}

    @media (max-width: 560px) {{
      .kpi {{ grid-template-columns: 1fr; }}
      .meta-box {{ min-width: 0; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back" href="/review">← 返回 Review</a>
    <h1>预览</h1>
    <div class="subtitle">
      屏幕预览（按原始比例显示）&nbsp;&nbsp;
      <span style="display:inline-flex; gap:6px; vertical-align:middle;">
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#000;border:1px solid rgba(255,255,255,0.70);"></span>
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#fff;border:1px solid rgba(255,255,255,0.45);"></span>
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#c80000;border:1px solid rgba(255,255,255,0.18);"></span>
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#e0b400;border:1px solid rgba(255,255,255,0.18);"></span>
      </span>
    </div>

    <div class="controls">
      <button type="button" id="rerollBtn">同一天换一张</button>
    </div>

    <div class="status" id="statusLine"></div>

      <div class="preview-wrap">
      <div class="canvas-box">
        <canvas id="previewCanvas" width="960" height="540"></canvas>
      </div>

      <div class="meta-box">
        <div class="hero-text" id="kpiSide"></div>

        <div class="chips" id="fieldType"></div>

        <div class="sub-text" id="fieldCaption"></div>

        <div class="score-bars">
          <div class="score-row">
            <div>回忆度</div>
            <div class="score-track">
              <div class="score-fill memory" id="barMemory"></div>
            </div>
            <div class="score-num" id="numMemory"></div>
          </div>
          <div class="score-row">
            <div>美观度</div>
            <div class="score-track">
              <div class="score-fill beauty" id="barBeauty"></div>
            </div>
            <div class="score-num" id="numBeauty"></div>
          </div>
        </div>

        <div class="sub-text" id="fieldReason"></div>

        <details class="fold">
          <summary>
            <span>更多信息</span>
            <span class="fold-hint">EXIF / 路径 / 调试</span>
          </summary>

          <div class="kv-grid">
            <div class="kv-k">日期</div><div class="kv-v" id="kpiDate"></div>
            <div class="kv-k">地点</div><div class="kv-v" id="kpiLocation"></div>
            <div class="kv-k">图片URL</div><div class="kv-v" id="fieldPath"></div>
            <div class="kv-k">原始路径</div><div class="kv-v" id="fieldOrigPath"></div>
            <div class="kv-k">分辨率</div><div class="kv-v" id="fieldRes"></div>
            <div class="kv-k">方向</div><div class="kv-v" id="fieldOrientation"></div>
            <div class="kv-k">已上屏</div><div class="kv-v" id="fieldUsedAt"></div>
            <div class="kv-k">EXIF摘要</div><div class="kv-v" id="fieldExifSummary"></div>
          </div>

          <details class="fold" style="margin-top:10px;">
            <summary>
              <span>EXIF JSON</span>
              <span class="fold-hint">调试</span>
            </summary>
            <div class="mono" id="fieldExifJson"></div>
          </details>
        </details>
      </div>
    </div>
  </div>

  <script>
    const PHOTOS = {data_json};
    const SELECTED_IMG = {selected_json};

    const byDate = new Map();
    for (const p of PHOTOS) {{
      if (!p.date) continue;
      if (!byDate.has(p.date)) byDate.set(p.date, []);
      byDate.get(p.date).push(p);
    }}
    for (const [d, arr] of byDate.entries()) {{
      arr.sort((a, b) => ((b.memory ?? -1) - (a.memory ?? -1)));
    }}

    const canvas = document.getElementById('previewCanvas');
    const ctx = canvas.getContext('2d');
    const statusLine = document.getElementById('statusLine');

    const kpiDate = document.getElementById('kpiDate');
    const kpiLocation = document.getElementById('kpiLocation');
    const kpiSide = document.getElementById('kpiSide');

    const fieldPath = document.getElementById('fieldPath');
    const fieldOrigPath = document.getElementById('fieldOrigPath');
    const fieldType = document.getElementById('fieldType');
    const fieldCaption = document.getElementById('fieldCaption');
    const fieldReason = document.getElementById('fieldReason');
    const fieldRes = document.getElementById('fieldRes');
    const fieldOrientation = document.getElementById('fieldOrientation');
    const fieldUsedAt = document.getElementById('fieldUsedAt');
    const fieldExifSummary = document.getElementById('fieldExifSummary');
    const fieldExifJson = document.getElementById('fieldExifJson');

    // 评分条
    const barMemory = document.getElementById('barMemory');
    const barBeauty = document.getElementById('barBeauty');
    const numMemory = document.getElementById('numMemory');
    const numBeauty = document.getElementById('numBeauty');

    let currentDate = null;
    let currentPhoto = null;

    function formatLocation(lat, lon, city) {{
      const c = (city || '').trim();
      if (c.length > 0) return c;
      if (lat == null || lon == null) return '';
      try {{
        return Number(lat).toFixed(5) + ', ' + Number(lon).toFixed(5);
      }} catch (e) {{
        return String(lat) + ', ' + String(lon);
      }}
    }}

    function formatDateDisplay(dateStr) {{
      if (!dateStr) return '';
      const parts = dateStr.split('-');
      if (parts.length < 3) return dateStr;
      const y = parts[0];
      const m = String(parseInt(parts[1], 10));
      const d = String(parseInt(parts[2], 10));
      return y + '.' + m + '.' + d;
    }}

    function escapeHtml(s) {{
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function hashToHue(str) {{
      // 简单稳定 hash -> 0..359
      let h = 0;
      const s = String(str || '');
      for (let i = 0; i < s.length; i++) {{
        h = (h * 31 + s.charCodeAt(i)) >>> 0;
      }}
      return h % 360;
    }}

    function renderTags(tags) {{
      if (!Array.isArray(tags) || tags.length === 0) return '';
      let htmlOut = '';
      for (const t of tags) {{
        if (!t) continue;
        const hue = hashToHue(t);
        const bg = 'hsla(' + hue + ', 90%, 55%, 0.14)';
        const bd = 'hsla(' + hue + ', 90%, 55%, 0.30)';
        const dot = 'hsl(' + hue + ', 90%, 62%)';
        htmlOut += '<span class="chip" style="background:' + bg + '; border-color:' + bd + ';">'
          + '<span class="chip-dot" style="background:' + dot + ';"></span>'
          + escapeHtml(t)
          + '</span>';
      }}
      return htmlOut;
    }}

    function safeText(v) {{
      if (v === null || v === undefined) return '';
      return String(v);
    }}

    function wrapText(ctx, text, x, y, maxWidth, lineHeight, maxLines) {{
      if (!text) return;
      const words = text.split(/\\s+/);
      let line = '';
      let lineCount = 0;
      for (let n = 0; n < words.length; n++) {{
        const testLine = line ? (line + ' ' + words[n]) : words[n];
        const metrics = ctx.measureText(testLine);
        if (metrics.width > maxWidth && n > 0) {{
          ctx.fillText(line, x, y);
          line = words[n];
          y += lineHeight;
          lineCount++;
          if (lineCount >= maxLines) break;
        }} else {{
          line = testLine;
        }}
      }}
      if (line && lineCount < maxLines) ctx.fillText(line, x, y);
    }}

    function applyFourColorDither() {{
      const w = canvas.width, h = canvas.height;
      let imgData;
      try {{
        imgData = ctx.getImageData(0, 0, w, h);
      }} catch (e) {{
        statusLine.textContent = '无法从画布读取像素（跨域或图片未走 /images）：' + e;
        return;
      }}
      const data = imgData.data;

      const palette = [
        {{ r: 0, g: 0, b: 0 }},
        {{ r: 255, g: 255, b: 255 }},
        {{ r: 200, g: 0, b: 0 }},
        {{ r: 220, g: 180, b: 0 }}
      ];

      const errR = new Float32Array(w);
      const errG = new Float32Array(w);
      const errB = new Float32Array(w);
      const nextErrR = new Float32Array(w);
      const nextErrG = new Float32Array(w);
      const nextErrB = new Float32Array(w);

      function nearestColor(r, g, b) {{
        let bestIndex = 0;
        let bestDist = Infinity;
        for (let i = 0; i < palette.length; i++) {{
          const pr = palette[i].r, pg = palette[i].g, pb = palette[i].b;
          const dr = r - pr, dg = g - pg, db = b - pb;
          const dist = dr*dr + dg*dg + db*db;
          if (dist < bestDist) {{ bestDist = dist; bestIndex = i; }}
        }}
        return palette[bestIndex];
      }}

      for (let y = 0; y < h; y++) {{
        for (let x = 0; x < w; x++) {{
          const idx = (y * w + x) * 4;

          let r = data[idx] + errR[x];
          let g = data[idx + 1] + errG[x];
          let b = data[idx + 2] + errB[x];

          r = r < 0 ? 0 : (r > 255 ? 255 : r);
          g = g < 0 ? 0 : (g > 255 ? 255 : g);
          b = b < 0 ? 0 : (b > 255 ? 255 : b);

          const nc = nearestColor(r, g, b);

          data[idx] = nc.r;
          data[idx + 1] = nc.g;
          data[idx + 2] = nc.b;

          const er = r - nc.r, eg = g - nc.g, eb = b - nc.b;

          if (x + 1 < w) {{
            errR[x + 1] += er * (7 / 16);
            errG[x + 1] += eg * (7 / 16);
            errB[x + 1] += eb * (7 / 16);
          }}
          if (y + 1 < h) {{
            if (x > 0) {{
              nextErrR[x - 1] += er * (3 / 16);
              nextErrG[x - 1] += eg * (3 / 16);
              nextErrB[x - 1] += eb * (3 / 16);
            }}
            nextErrR[x] += er * (5 / 16);
            nextErrG[x] += eg * (5 / 16);
            nextErrB[x] += eb * (5 / 16);
            if (x + 1 < w) {{
              nextErrR[x + 1] += er * (1 / 16);
              nextErrG[x + 1] += eg * (1 / 16);
              nextErrB[x + 1] += eb * (1 / 16);
            }}
          }}
        }}

        if (y + 1 < h) {{
          for (let i = 0; i < w; i++) {{
            errR[i] = nextErrR[i]; errG[i] = nextErrG[i]; errB[i] = nextErrB[i];
            nextErrR[i] = 0; nextErrG[i] = 0; nextErrB[i] = 0;
          }}
        }}
      }}

      ctx.putImageData(imgData, 0, 0);
    }}

    function updateMeta(photo) {{
      if (!photo) {{
        kpiDate.textContent = '';
        kpiLocation.textContent = '';
        kpiSide.textContent = '';

        fieldPath.textContent = '';
        fieldOrigPath.textContent = '';
        fieldType.innerHTML = '';
        fieldCaption.textContent = '';
        fieldReason.textContent = '';
        fieldRes.textContent = '';
        fieldOrientation.textContent = '';
        fieldUsedAt.textContent = '';
        fieldExifSummary.textContent = '';
        fieldExifJson.textContent = '';
        // 清空评分条
        barMemory.style.width = '0%';
        barBeauty.style.width = '0%';
        numMemory.textContent = '';
        numBeauty.textContent = '';
        return;
      }}

      // 填充 meta-box 新结构
      kpiSide.textContent = photo.side ? '「' + safeText(photo.side) + '」' : '';
      fieldType.innerHTML = renderTags(photo.type);
      fieldCaption.textContent = safeText(photo.caption);

      // 评分条
      const m = photo.memory != null ? Math.max(0, Math.min(100, photo.memory)) : 0;
      const b = photo.beauty != null ? Math.max(0, Math.min(100, photo.beauty)) : 0;
      barMemory.style.width = m + '%';
      barBeauty.style.width = b + '%';
      numMemory.textContent = m ? m.toFixed(1) : '';
      numBeauty.textContent = b ? b.toFixed(1) : '';

      fieldReason.textContent = photo.reason ? '评分理由：' + safeText(photo.reason) : '';

      // 更多信息区
      const loc = formatLocation(photo.lat, photo.lon, photo.city);
      kpiDate.textContent = safeText(photo.date);
      kpiLocation.textContent = safeText(loc);
      fieldPath.textContent = safeText(photo.path);
      fieldOrigPath.textContent = safeText(photo.orig_path || '');
      const res = (safeText(photo.width) || safeText(photo.height)) ? (safeText(photo.width) + ' x ' + safeText(photo.height)) : '';
      fieldRes.textContent = res;
      fieldOrientation.textContent = safeText(photo.orientation);
      fieldUsedAt.textContent = safeText(photo.used_at);
      fieldExifSummary.textContent = safeText(photo.exif_summary);
      fieldExifJson.textContent = safeText(photo.exif_json);
    }}

    function drawPreview(photo) {{
      if (!photo) {{
        statusLine.textContent = '未指定照片。请从 /review 点击某张照片进入预览。';
        return;
      }}

      statusLine.textContent = ''; // 正常情况不显示信息

      // 固定画布显示区域（与 UI 保持一致），改为根据容器宽度自适应并支持高清渲染（devicePixelRatio）
      const BASE_W = 960;
      const BASE_H = 540;
      const dpr = window.devicePixelRatio || 1;
      const parent = canvas.parentElement || document.body;
      const cssWidth = Math.min(parent.clientWidth, BASE_W);
      const cssHeight = Math.round(cssWidth * BASE_H / BASE_W);
      // 设置 CSS 尺寸用于布局，并设置实际像素用于高清渲染
      canvas.style.width = cssWidth + 'px';
      canvas.style.height = cssHeight + 'px';
      canvas.width = Math.round(cssWidth * dpr);
      canvas.height = Math.round(cssHeight * dpr);
      // 将绘图坐标系映射到 CSS 像素单位（方便后续绘制使用原本的坐标值）
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = '#FFFFFF';
      ctx.fillRect(0, 0, cssWidth, cssHeight);

      const img = new Image();
      img.onload = function() {{
          // 按原始比例缩放，使图片完整显示并居中（不裁切），使用 CSS 像素尺寸
          const iw = img.naturalWidth || img.width;
          const ih = img.naturalHeight || img.height;
          const scale = Math.min(cssWidth / iw, cssHeight / ih);
          const dw = Math.round(iw * scale);
          const dh = Math.round(ih * scale);
          const dx = Math.round((cssWidth - dw) / 2);
          const dy = Math.round((cssHeight - dh) / 2);

          ctx.clearRect(0, 0, cssWidth, cssHeight);
          ctx.fillStyle = '#FFFFFF';
          ctx.fillRect(0, 0, cssWidth, cssHeight);
          ctx.drawImage(img, dx, dy, dw, dh);
        }};
      img.onerror = function() {{
        statusLine.textContent = '图片加载失败：' + photo.path;
      }};
      img.src = '/sim_render?img=' + encodeURIComponent(photo.path);
    }}

    function pickPhotoFromDate(date, excludePath) {{
      const arr = byDate.get(date) || [];
      if (!arr.length) return null;

      const THRESHOLD = {float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)};
      // 先过滤掉要排除的路径（如果有）
      const pool = excludePath ? arr.filter(p => p.path !== excludePath) : arr.slice();
      if (!pool.length) return null;

      const candidates = pool.filter(p => p.memory != null && p.memory > THRESHOLD);
      if (candidates.length > 0) {{
        const idx = Math.floor(Math.random() * candidates.length);
        return {{ photo: candidates[idx], dateUsed: date }};
      }}

      // 兜底：当天随便挑（已排除 excludePath）
      const idx = Math.floor(Math.random() * pool.length);
      return {{ photo: pool[idx], dateUsed: date, fallbackNoThreshold: true }};
    }}

    function getPreviousDateStr(dateStr) {{
      if (!dateStr) return null;
      const parts = dateStr.split('-');
      if (parts.length < 3) return null;
      const y = parseInt(parts[0], 10);
      const m = parseInt(parts[1], 10);
      const d = parseInt(parts[2], 10);
      if (!y || !m || !d) return null;
      const dt = new Date(y, m - 1, d);
      dt.setDate(dt.getDate() - 1);
      const yy = dt.getFullYear();
      const mm = String(dt.getMonth() + 1).padStart(2, '0');
      const dd = String(dt.getDate()).padStart(2, '0');
      return yy + '-' + mm + '-' + dd;
    }}

    function pickPhotoWithLookback(baseDate, excludePath) {{
      if (!baseDate) return null;
      let date = baseDate;
      const MAX_LOOKBACK = 30;

      for (let i = 0; i < MAX_LOOKBACK; i++) {{
        const picked = pickPhotoFromDate(date, excludePath);
        if (picked && picked.photo) return picked;
        const prev = getPreviousDateStr(date);
        if (!prev) break;
        date = prev;
      }}

      // 最终兜底：目标日期没找到 map，啥也不干
      return null;
    }}

    function findSelectedPhoto() {{
      if (!SELECTED_IMG) return null;
      for (const p of PHOTOS) {{
        if (p.path === SELECTED_IMG) return p;
      }}
      return null;
    }}

    function onRerollSameDay() {{
      if (!currentDate) {{
        statusLine.textContent = '请从 /review 点击某张照片进入模拟器。';
        return;
      }}

      const pick = pickPhotoWithLookback(currentDate, currentPhoto ? currentPhoto.path : null);
      if (!pick || !pick.photo) {{
        statusLine.textContent = '该日期及向前 30 天内没有可用照片。';
        return;
      }}

      // 如果刚好又抽到自己（理论上已尽量排除），再允许尝试几次（继续排除当前路径）
      let tries = 0;
      let chosen = pick;
      while (tries < 6 && chosen && chosen.photo && currentPhoto && chosen.photo.path === currentPhoto.path) {{
        const again = pickPhotoWithLookback(currentDate, currentPhoto ? currentPhoto.path : null);
        if (!again || !again.photo) break;
        chosen = again;
        tries++;
      }}

      currentPhoto = chosen.photo;
      updateMeta(currentPhoto);
      drawPreview(currentPhoto);
    }}

    document.getElementById('rerollBtn').addEventListener('click', onRerollSameDay);

    // 默认进入：如果从 review 点进来，则显示该照片；否则提示用户从 review 进入
    const initPhoto = findSelectedPhoto();
    if (!initPhoto) {{
      updateMeta(null);
      drawPreview(null);
    }} else {{
      currentDate = initPhoto.date;
      currentPhoto = initPhoto;
      updateMeta(currentPhoto);
      drawPreview(currentPhoto);
    }}
  </script>
</body>
</html>
"""
    return html_str


# --------------------------
# Routes
# --------------------------

def build_index_html():
    """构���主页HTML，显示全局照片和今日精选两个选项"""
    html_str = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>InkTime 主页</title>
  <style>
    :root {
      --bg: #0b0c10;
      --panel: rgba(255,255,255,0.06);
      --card: rgba(255,255,255,0.10);
      --card2: rgba(255,255,255,0.08);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --muted2: rgba(255,255,255,0.48);
      --line: rgba(255,255,255,0.14);
      --accent: #8ab4ff;
      --accent2:#9cffd6;
      --shadow: 0 18px 60px rgba(0,0,0,0.45);
      --shadow2: 0 10px 28px rgba(0,0,0,0.35);
      --radius: 14px;
    }
    body {
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
      background: #9FB8A9; /* 豆沙绿 */
      color: var(--text);
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
    }
    .container {
      max-width: 600px;
      margin: 0 auto;
      padding: 0 20px;
    }
    .title {
      text-align: center;
      font-size: 28px;
      margin: 0 0 40px;
      letter-spacing: 0.5px;
      color: var(--text);
    }
    .options-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 20px;
    }
    .option-card {
      background: linear-gradient(180deg, var(--card) 0%, var(--card2) 100%);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 30px;
      text-align: center;
      cursor: pointer;
      transition: transform .12s ease, border-color .15s ease, box-shadow .15s ease;
      text-decoration: none;
      display: block;
      color: inherit;
    }
    .option-card:hover {
      transform: translateY(-4px);
      border-color: var(--accent);
      box-shadow: var(--shadow);
    }
    .option-title {
      font-size: 20px;
      margin: 0 0 12px;
      font-weight: 600;
    }
    .option-desc {
      font-size: 14px;
      color: var(--muted);
      margin: 0;
      line-height: 1.5;
    }

    /* 横屏模式下的自适应 */
    @media screen and (orientation: landscape) {
      .options-grid {
        grid-template-columns: 1fr 1fr; /* 横屏时显示两列 */
        gap: 30px;
      }
      .option-card {
        padding: 35px 25px; /* 横屏时调整内边距 */
      }
    }

    /* 竖屏模式下的自适应 */
    @media screen and (orientation: portrait) {
      .title {
        font-size: 26px; /* 竖屏时稍微减小标题大小 */
        margin: 0 0 30px;
      }
      .options-grid {
        gap: 16px; /* 竖屏时减小间距 */
      }
      .option-card {
        padding: 25px 20px; /* 竖屏时调整内边距 */
      }
    }
  </style>
</head>
<body>
  <div class="container">
    <h1 class="title">I 照片管理</h1>
    <div class="options-grid">
      <a href="/review" class="option-card">
        <h2 class="option-title">全局照片</h2>
        <p class="option-desc">浏览和管理所有照片，支持按日期、评分等条件筛选</p>
      </a>
      <a href="/today-image" class="option-card">
        <h2 class="option-title">今日精选</h2>
        <p class="option-desc">查看今日推荐的照片</p>
      </a>
    </div>
  </div>
</body>
</html>"""
    return html_str


@app.get("/")
def index():
    if ENABLE_REVIEW_WEBUI:
        html_str = build_index_html()
        return Response(html_str, mimetype="text/html; charset=utf-8")
    return Response("InkTime server running. WebUI disabled.", mimetype="text/plain; charset=utf-8")


@app.get("/review")
def review():
    _require_webui_enabled()
    try:
        page = int(request.args.get("page", "1"))
    except Exception:
        page = 1

    md = (request.args.get('md', '') or '').strip()
    month = (request.args.get('month', '') or '').strip()
    day = (request.args.get('day', '') or '').strip()
    sort = (request.args.get('sort', '') or 'memory').strip() or 'memory'

    rows, total_count = load_rows(page=page, page_size=REVIEW_PAGE_SIZE, md=md, sort=sort, month=month, day=day)
    if not rows:
        return Response(
            "数据库里没有可展示的数据。请先运行你的分析脚本生成评分与文案。",
            status=404,
            mimetype="text/plain; charset=utf-8",
        )

    html_str = build_html(rows, page=page, page_size=REVIEW_PAGE_SIZE, total_count=total_count)
    return Response(html_str, mimetype="text/html; charset=utf-8")


# API endpoint for md list
@app.get('/api/md_list')
def api_md_list():
    _require_webui_enabled()
    md_list = _load_all_md_list()
    return Response(json.dumps(md_list, ensure_ascii=False), mimetype='application/json; charset=utf-8')


@app.get("/sim")
def sim():
    _require_webui_enabled()
    selected_img = request.args.get("img", "")

    # 默认不再全库加载，避免 /sim 页面巨大 JSON 导致浏览器转圈
    sim_rows = []

    # 仅当从 /review 点进来且参数合法时，按“该日期 + 向前 30 天”加载
    if selected_img and isinstance(selected_img, str) and selected_img.startswith("/images/"):
        subpath = selected_img[len("/images/"):]
        try:
            p = _safe_join(IMAGE_DIR, subpath)
        except Exception:
            p = None

        if p is not None and p.exists() and p.is_file():
            meta = get_photo_meta_by_path(str(p))
            base_date = meta.get("date") if meta else ""

            if base_date:
                try:
                    from datetime import datetime, timedelta
                    dt0 = datetime.strptime(base_date, "%Y-%m-%d")
                    dates = [(dt0 - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 31)]
                except Exception:
                    dates = [base_date]

                sim_rows = load_sim_rows_for_dates(dates)

    html_str = build_simulator_html(sim_rows, selected_img=selected_img)
    return Response(html_str, mimetype="text/html; charset=utf-8")


@app.get("/images/<path:subpath>")
def images(subpath: str):
    _require_webui_enabled()
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)
    return _send_static_file(p)

@app.get("/sim_render")
def sim_render():
    _require_webui_enabled()

    img_uri = request.args.get("img", "")
    if not img_uri or not img_uri.startswith("/images/"):
        abort(400)

    subpath = img_uri[len("/images/"):]
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)

    if not p.exists() or not p.is_file():
        abort(404)

    meta = get_photo_meta_by_path(str(p))
    if meta is None:
        # 兜底：DB 没命中就渲染纯图（不建议长期这样）
        meta = {
            "path": str(p),
            "date": "",
            "side": "",
            "memory": None,
            "lat": None,
            "lon": None,
            "city": "",
        }

    try:
        img = rdp.render_image(meta)
        bio = BytesIO()
        # 直接返回渲染结果，不做设备特定抖动
        img.save(bio, format="PNG")
        bio.seek(0)
        return send_file(bio, mimetype="image/png", as_attachment=False)
    except Exception:
        abort(500)


def _ensure_cycle_photos(orientation=None):
    """确保 _CYCLE_PHOTOS 填充为今日的候选图片（基于服务器启动时已选好的照片）。
    orientation: 'landscape', 'portrait', 或 None (默认，使用所有方向)"""
    import time
    from datetime import datetime
    global _CYCLE_STATES, _CYCLE_PHOTOS, _CYCLE_IDX, _LAST_PHOTO_DATE, _STARTUP_SELECTED_PHOTOS

    now = time.time()
    current_date = datetime.now().strftime("%Y-%m-%d")  # 获取当前日期

    # 注意：此函数假定在调用时已经获得了 _CYCLE_LOCK 锁
    # 如果直接调用此函数，请确保在锁保护下执行

    # 检查日期是否变化，如果是，则清空缓存
    if _LAST_PHOTO_DATE != current_date:
        _CYCLE_STATES.clear()  # 清空所有缓存
        _LAST_PHOTO_DATE = current_date  # 更新记录的日期

    # 如果指定了方向，使用不同的缓存键
    cache_key = orientation if orientation else 'all'
    state = _CYCLE_STATES.get(cache_key, {})
    cached_photos = state.get('photos', [])
    cached_idx = state.get('idx', 0)  # 获取缓存的索引
    built_at = state.get('built_at', 0)

    if cached_photos and (now - built_at) < _CYCLE_TTL_SEC:
        # 更新全局变量供调用者使用
        _CYCLE_PHOTOS = cached_photos
        _CYCLE_IDX = cached_idx  # 使用缓存的索引
        return

    try:
        # 使用服务器启动时已经选好的照片，不再重复选片
        startup_photos = _STARTUP_SELECTED_PHOTOS.get(cache_key, [])
        photos = startup_photos

        # photos 是一组 item dict，直接保存
        _CYCLE_PHOTOS = photos
        _CYCLE_IDX = 0  # 对于新加载的照片集，从索引0开始

        # 更新全局状态
        _CYCLE_STATES[cache_key] = {
            'photos': photos,
            'idx': _CYCLE_IDX,  # 保存当前索引
            'built_at': now
        }

        print(f"[server] cycle_photos loaded: {len(_CYCLE_PHOTOS)} items (orientation: {orientation or 'all'})")
    except Exception:
        _CYCLE_PHOTOS = []
        _CYCLE_IDX = 0
        _CYCLE_STATES[cache_key] = {
            'photos': [],
            'idx': 0,
            'built_at': now
        }


@app.get('/today-image')
def today_image():
    """返回今日候选图之一；每次访问按循环顺序切换，方便浏览器刷新切换图片。"""
    _require_webui_enabled()

    # 检测屏幕方向
    user_agent = request.headers.get('User-Agent', '').lower()
    # 检查请求参数中是否包含方向信息
    orientation_param = request.args.get('orientation', '').lower()

    # 如果请求参数中指定了方向，则使用该方向
    if orientation_param in ['landscape', 'portrait']:
        orientation = orientation_param
    # 否则尝试从User-Agent或其他头部信息推断（简单实现）
    elif 'orientation=landscape' in user_agent or 'landscape' in user_agent:
        orientation = 'landscape'
    elif 'orientation=portrait' in user_agent or 'portrait' in user_agent:
        orientation = 'portrait'
    else:
        # 无法确定方向，使用所有方向的照片
        orientation = None

    global _CYCLE_PHOTOS, _CYCLE_IDX, _CYCLE_STATES
    with _CYCLE_LOCK:
        # 在锁内调用 _ensure_cycle_photos，确保一致性
        _ensure_cycle_photos(orientation=orientation)
        if not _CYCLE_PHOTOS:
            abort(404)
        idx = _CYCLE_IDX
        next_idx = (_CYCLE_IDX + 1) % len(_CYCLE_PHOTOS)
        photo = _CYCLE_PHOTOS[idx]

        # 更新状态中的索引
        cache_key = orientation if orientation else 'all'
        if cache_key in _CYCLE_STATES:
            _CYCLE_STATES[cache_key]['idx'] = next_idx
        _CYCLE_IDX = next_idx  # 更新全局变量以供当前请求使用

    # photo 格式与 rdp.render_image 接受的 item 一致
    try:
        img = rdp.render_image(photo)
        bio = BytesIO()
        img.save(bio, format='PNG')
        bio.seek(0)

        # 创建响应并添加缓存控制头，防止浏览器缓存
        response = send_file(bio, mimetype='image/png', as_attachment=False)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception:
        abort(500)


# 移除前端页面实现，保持后台检测逻辑


# ESP32 专用下载接口已移除 — 现在直接通过网页访问渲染结果


@app.get("/files/")
@app.get("/files/<path:subpath>")
def browse(subpath: str = ""):
    _require_webui_enabled()
    try:
        p = _safe_join(BIN_OUTPUT_DIR, subpath)
    except Exception:
        abort(400)

    if p.is_file():
        return _send_static_file(p)

    if not p.exists() or not p.is_dir():
        abort(404)

    items = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        name = child.name + ("/" if child.is_dir() else "")
        rel = child.relative_to(BIN_OUTPUT_DIR)
        href = "/files/" + str(rel).replace("\\", "/")
        items.append(f'<li><a href="{html.escape(href)}">{html.escape(name)}</a></li>')

    up = ""
    if p != BIN_OUTPUT_DIR:
        parent_rel = p.parent.relative_to(BIN_OUTPUT_DIR)
        up_href = "/files/" + str(parent_rel).replace("\\", "/")
        up = f'<a href="{html.escape(up_href)}">⬅ 返回上级</a><br><br>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>InkTime Files</title>
<style>
body {{ font-family: -apple-system,BlinkMacSystemFont,system-ui,sans-serif; padding: 24px; }}
ul {{ line-height: 1.8; }}
code {{ background:#f2f2f2; padding:2px 6px; border-radius:4px; }}
</style>
</head>
<body>
<h3>输出目录浏览</h3>
<p>当前：<code>{html.escape(str(p.relative_to(BIN_OUTPUT_DIR) if p != BIN_OUTPUT_DIR else "."))}</code></p>
{up}
<ul>
{''.join(items)}
</ul>
</body>
</html>
"""


if __name__ == "__main__":
    mimetypes.add_type("application/octet-stream", ".bin")
    print(f"[InkTime] DB: {DB_PATH}")
    print(f"[InkTime] IMAGE_DIR: {IMAGE_DIR}")
    print(f"[InkTime] OUT: {BIN_OUTPUT_DIR}")
    print(f"[InkTime] key: {DOWNLOAD_KEY}")
    print(f"[InkTime] listen: {FLASK_HOST}:{FLASK_PORT}")
    print(f"[InkTime] open: http://127.0.0.1:{FLASK_PORT}/  (本机)")

    # 启动时运行一次选片功能
    try:
        import render_daily_photo as rdp
        items = rdp.load_sim_rows()
        # 优先使用按方向选片（3 横 + 3 竖），若不可用则回退到旧的按数量选片
        try:
            photos, info = rdp.choose_photos_by_orientation(items, rdp.TODAY, landscape_count=3, portrait_count=3)
        except Exception:
            photos, info = rdp.choose_photos_for_today(items, rdp.TODAY, count=6)  # 总共6张

        # 将选好的照片按方向分类存储
        landscape_photos = [p for p in photos if p.get('orientation') in ['Landscape', 'landscape', 'L', 'l']]
        portrait_photos = [p for p in photos if p.get('orientation') in ['Portrait', 'portrait', 'P', 'p']]

        # 如果无法按方向分类，手动分配前5个为横屏，后5个为竖屏（如果总数是10）
        if len(landscape_photos) == 0 and len(portrait_photos) == 0 and len(photos) >= 2:
            landscape_photos = photos[:min(5, len(photos)//2 + len(photos)%2)]
            portrait_photos = photos[min(5, len(photos)//2 + len(photos)%2):]

        # 更新全局变量
        globals()['_STARTUP_SELECTED_PHOTOS'] = {
            'all': photos,
            'landscape': landscape_photos,
            'portrait': portrait_photos
        }
        print("今日照片已筛选完毕")
    except Exception as e:
        print(f"选片功能运行出错: {e}")
        # 如果出错，至少初始化一个空字典
        globals()['_STARTUP_SELECTED_PHOTOS'] = {'all': [], 'landscape': [], 'portrait': []}

    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)