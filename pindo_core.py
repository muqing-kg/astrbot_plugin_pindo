"""Pindo 拼豆引擎核心：LunarXuan/Pindo 默认管线的 Python 移植。

移植范围对应网页端默认参数（写实模式 / 白底 / 无抖动 / 最多 16 色）：
RGBA 白底合成 → 线性 RGB 面积平均降采样 → CIEDE2000 最近色匹配
→ 关键色保护限色重匹配 → 图纸 PNG 渲染（网格/拼板线/色号/用量图例/信息条）。

原项目：https://github.com/LunarXuan/Pindo （GPL-3.0）
本文件为其衍生实现，同样以 GPL-3.0 发布。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

PLUGIN_DIR = Path(__file__).resolve().parent
PALETTE_DIR = PLUGIN_DIR / "palettes"
FONT_DIR = PLUGIN_DIR / "fonts"

BRAND_ORDER = ("mard", "coco", "manman", "panpan", "mixiaowo", "hama", "perler", "artkal-s")
BRAND_LABELS = {
    "mard": "MARD",
    "coco": "COCO",
    "manman": "漫漫",
    "panpan": "盼盼",
    "mixiaowo": "咪小窝",
    "hama": "Hama",
    "perler": "Perler",
    "artkal-s": "Artkal S",
}

MAX_CANVAS_DIM = 16384
MAX_CANVAS_PIXELS = 268_435_456
BOARD_SIZE = 29
BORDER_COLOR = (0x68, 0x70, 0xB8)
SUB_GRID = 5
INFO_TEXT_COLOR = (0x37, 0x41, 0x51)
INFO_SEPARATOR = (0xE5, 0xE7, 0xEB)

COMMAND_RE = re.compile(r"^\s*/?\s*拼豆(?:\s+(品牌方|\S+))?\s*$|^\s*/?\s*拼豆品牌方\s*$")


def resolve_brand(token: str | None) -> str | None:
    """品牌别名解析：序号 / 色板 id / 显示名（大小写不敏感）/ artkal 简写。"""
    if token is None:
        return None
    t = token.strip().lower()
    if not t:
        return None
    if t.isdigit():
        idx = int(t)
        return BRAND_ORDER[idx - 1] if 1 <= idx <= len(BRAND_ORDER) else None
    if t in BRAND_ORDER:
        return t
    aliases = {"artkal": "artkal-s"}
    for bid, label in BRAND_LABELS.items():
        aliases[label.lower()] = bid
    return aliases.get(t)

_PALETTE_CACHE: dict[str, list[dict]] = {}

# ---------------------------------------------------------------- fonts

_FONT_CANDIDATES = (
    FONT_DIR / "NotoSansSC-Regular.ttf",
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    Path("/usr/share/fonts/wqy-microhei/wqy-microhei.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("C:/Windows/Fonts/msyh.ttc"),
)
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def get_font(size: int) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    font = None
    for candidate in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(str(candidate), size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


# ---------------------------------------------------------------- palette

def load_palette(brand: str) -> list[dict]:
    cached = _PALETTE_CACHE.get(brand)
    if cached is not None:
        return cached
    path = PALETTE_DIR / f"{brand}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    _PALETTE_CACHE[brand] = data
    return data


# ---------------------------------------------------------------- sRGB / Lab

def _build_srgb_lut() -> np.ndarray:
    s = np.arange(256, dtype=np.float64) / 255.0
    lin = np.where(s <= 0.04045, s / 12.92, np.power((s + 0.055) / 1.055, 2.4))
    return lin


_SRGB_TO_LINEAR = _build_srgb_lut()


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    v = np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * np.power(np.maximum(values, 1e-12), 1 / 2.4) - 0.055,
    )
    return np.clip(np.rint(v * 255.0), 0, 255).astype(np.uint8)


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """rgb: (..., 3) 0-255 → Lab (D65, 与 color-matcher.ts 同常数)。"""
    s = rgb.astype(np.float64) / 255.0
    lin = np.where(s <= 0.04045, s / 12.92, np.power((s + 0.055) / 1.055, 2.4))
    x = lin[..., 0] * 0.4124564 + lin[..., 1] * 0.3575761 + lin[..., 2] * 0.1804375
    y = lin[..., 0] * 0.2126729 + lin[..., 1] * 0.7151522 + lin[..., 2] * 0.0721750
    z = lin[..., 0] * 0.0193339 + lin[..., 1] * 0.1191920 + lin[..., 2] * 0.9503041
    xn, yn, zn = x / 0.95047, y / 1.0, z / 1.08883
    xyz = np.stack([xn, yn, zn], axis=-1)
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    lab = np.empty_like(f)
    lab[..., 0] = 116.0 * fy - 16.0
    lab[..., 1] = 500.0 * (fx - fy)
    lab[..., 2] = 200.0 * (fy - fz)
    return lab


def _ciede2000_matrix(lab1: np.ndarray, lab2: np.ndarray, chunk: int = 2048) -> np.ndarray:
    """lab1: (N,3), lab2: (P,3) → (N,P) 距离矩阵。逐行移植 color-matcher.ts 的简化 CIEDE2000。"""
    n = lab1.shape[0]
    p = lab2.shape[0]
    out = np.empty((n, p), dtype=np.float64)
    l2, a2, b2 = lab2[:, 0], lab2[:, 1], lab2[:, 2]
    c2 = np.sqrt(a2 * a2 + b2 * b2)

    for start in range(0, n, chunk):
        block = lab1[start:start + chunk]
        l1 = block[:, 0:1]
        a1 = block[:, 1:2]
        b1 = block[:, 2:3]

        c1 = np.sqrt(a1 * a1 + b1 * b1)
        cab7 = ((c1 + c2) / 2.0) ** 7
        g = 0.5 * (1.0 - np.sqrt(cab7 / (cab7 + 25.0 ** 7)))

        a1p = a1 * (1.0 + g)
        a2p = a2 * (1.0 + g)
        c1p = np.sqrt(a1p * a1p + b1 * b1)
        c2p = np.sqrt(a2p * a2p + b2 * b2)

        h1p = np.degrees(np.arctan2(b1, a1p)) + np.where(b1 < 0, 360.0, 0.0)
        h2p = np.degrees(np.arctan2(b2, a2p)) + np.where(b2 < 0, 360.0, 0.0)

        dlp = l2 - l1
        dcp = c2p - c1p
        dhp = h2p - h1p
        dhp = np.where(np.abs(dhp) > 180.0, dhp + np.where(dhp > 0, -360.0, 360.0), dhp)
        dhp_big = 2.0 * np.sqrt(c1p * c2p) * np.sin(np.radians(dhp) / 2.0)

        lp = (l1 + l2) / 2.0
        cp = (c1p + c2p) / 2.0
        hp = (h1p + h2p) / 2.0
        hp = np.where(np.abs(h1p - h2p) > 180.0, hp - 180.0, hp)
        hp = np.where(hp < 0, hp + 360.0, hp)

        t = (1.0
             - 0.17 * np.cos(np.radians(hp - 30.0))
             + 0.24 * np.cos(np.radians(2.0 * hp))
             + 0.32 * np.cos(np.radians(3.0 * hp + 6.0))
             - 0.20 * np.cos(np.radians(4.0 * hp - 63.0)))

        sl = 1.0 + 0.015 * (lp - 50.0) ** 2 / np.sqrt(20.0 + (lp - 50.0) ** 2)
        sc = 1.0 + 0.045 * cp
        sh = 1.0 + 0.015 * cp * t

        cp7 = cp ** 7
        rt = (-2.0 * np.sqrt(cp7 / (cp7 + 25.0 ** 7))
              * np.sin(np.radians(60.0 * np.exp(-((hp - 275.0) / 25.0) ** 2))))

        out[start:start + chunk] = np.sqrt(
            (dlp / sl) ** 2 + (dcp / sc) ** 2 + (dhp_big / sh) ** 2 + rt * (dcp / sc) * (dhp_big / sh)
        )
    return out


# ---------------------------------------------------------------- downscale

def downscale_average(rgb: np.ndarray, dst_w: int, dst_h: int) -> np.ndarray:
    """移植 downscaler.ts downscaleAverage：sRGB→线性→面积平均→sRGB。

    rgb: (H, W, 3) uint8 → (dstH, dstW, 3) uint8
    """
    src_h, src_w = rgb.shape[:2]
    lin = _SRGB_TO_LINEAR[rgb]
    scale_x = src_w / dst_w
    scale_y = src_h / dst_h

    xs = []
    for dx in range(dst_w):
        sx0 = math.floor(dx * scale_x)
        sx1 = min(math.ceil((dx + 1) * scale_x), src_w)
        xs.append((sx0, sx1))

    out = np.empty((dst_h, dst_w, 3), dtype=np.float64)
    for dy in range(dst_h):
        sy0 = math.floor(dy * scale_y)
        sy1 = min(math.ceil((dy + 1) * scale_y), src_h)
        row = lin[sy0:sy1]
        for dx, (sx0, sx1) in enumerate(xs):
            out[dy, dx] = row[:, sx0:sx1].mean(axis=(0, 1))
    return _linear_to_srgb(out)


# ---------------------------------------------------------------- 限色（palette-limit.ts 逐行移植）

def _luminance(color: dict) -> float:
    r, g, b = color["rgb"]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _saturation(color: dict) -> float:
    r, g, b = color["rgb"]
    return float(max(r, g, b) - min(r, g, b))


def select_key_color_ids(palette: list[dict], usage: dict[str, int]) -> set[str]:
    used = [c for c in palette if usage.get(c["id"], 0) > 0]
    if not used:
        return set()

    keys: set[str] = set()
    by_luma = sorted(palette, key=_luminance)
    keys.add(by_luma[0]["id"])
    keys.add(by_luma[-1]["id"])

    min_usage = 2 if any(usage.get(c["id"], 0) >= 2 for c in used) else 1
    stable = [c for c in used if usage.get(c["id"], 0) >= min_usage]
    pool = stable if stable else used

    best = None
    best_score = -1.0
    for color in pool:
        score = _saturation(color) * 10 + usage.get(color["id"], 0)
        if score > best_score:
            best_score = score
            best = color
    if best is not None and _saturation(best) >= 28:
        keys.add(best["id"])
    return keys


def limit_palette_with_key_colors(
    palette: list[dict], usage: dict[str, int], max_colors: int
) -> tuple[list[dict], set[str]]:
    if max_colors <= 0 or max_colors >= len(palette):
        return palette, set()

    used = [c for c in palette if usage.get(c["id"], 0) > 0]
    protected_ids = select_key_color_ids(palette, usage)
    if len(used) <= max_colors:
        used_ids = {c["id"] for c in used}
        final = {i for i in protected_ids if i in used_ids}
        return [c for c in palette if c["id"] in used_ids], final

    by_count = sorted(used, key=lambda c: usage.get(c["id"], 0), reverse=True)
    selected: dict[str, None] = {c["id"]: None for c in by_count[:max_colors]}
    for cid in protected_ids:
        selected[cid] = None

    by_id = {c["id"]: c for c in palette}
    while len(selected) > max_colors:
        remove_id = None
        worst = math.inf
        for cid in selected:
            if cid in protected_ids:
                continue
            color = by_id[cid]
            count = usage.get(cid, 0)
            sat = _saturation(color)
            luma = _luminance(color)
            is_gray = sat < 30 and 50 < luma < 220
            score = count + (0 if is_gray else 10000)
            if score < worst:
                worst = score
                remove_id = cid
        if remove_id is None:
            trimmed = sorted(selected, key=lambda cid: usage.get(cid, 0), reverse=True)[:max_colors]
            selected = {cid: None for cid in trimmed}
            break
        del selected[remove_id]

    final = {i for i in protected_ids if i in selected}
    return [c for c in palette if c["id"] in selected], final


# ---------------------------------------------------------------- 管线

@dataclass
class GenerateResult:
    brand: str
    width: int
    height: int
    cells: list[list[str]]                 # colorId 网格
    palette: list[dict]                    # 最终使用的（限色后）色板
    usage: list[tuple[dict, int]]          # 用量，按色号自然序 + 数量降序


def _natural_key(code: str) -> tuple:
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", code) if part != ""
    )


def _match_indices(pixels: np.ndarray, palette: list[dict]) -> np.ndarray:
    labs = _rgb_to_lab(pixels.reshape(-1, 3))
    pal_lab = np.array([c["lab"] for c in palette], dtype=np.float64)
    dists = _ciede2000_matrix(labs, pal_lab)
    return dists.argmin(axis=1)


def compute_size(src_w: int, src_h: int, base_width: int = 35, max_long_edge: int = 120) -> tuple[int, int]:
    """不锁比例：宽取 base_width，高按原图比例；任意一边超出 max_long_edge 时等比压缩。"""
    w = max(1, int(base_width))
    h = max(1, round(w * src_h / src_w))
    if max(w, h) > max_long_edge:
        scale = max_long_edge / max(w, h)
        w = max(1, round(w * scale))
        h = max(1, round(h * scale))
    return w, h


def generate(
    image_path: str | Path,
    brand: str,
    *,
    base_width: int = 35,
    max_long_edge: int = 120,
    max_colors: int = 16,
) -> GenerateResult:
    palette = load_palette(brand)
    if not palette:
        raise ValueError(f"色板为空: {brand}")

    img = Image.open(image_path)
    img.load()
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    flat = Image.alpha_composite(bg, img).convert("RGB")
    arr = np.asarray(flat)
    src_h, src_w = arr.shape[:2]

    w, h = compute_size(src_w, src_h, base_width, max_long_edge)
    small = downscale_average(arr, w, h)

    matched_idx = _match_indices(small, palette)
    usage: dict[str, int] = Counter(palette[i]["id"] for i in matched_idx)

    effective_max_colors = max(1, min(max_colors, len(palette)))
    used_palette = palette
    if effective_max_colors < len(palette):
        limited, _protected = limit_palette_with_key_colors(palette, usage, effective_max_colors)
        used_palette = limited if limited else palette
        matched_idx = _match_indices(small, used_palette)
        usage = Counter(used_palette[i]["id"] for i in matched_idx)

    by_id = {c["id"]: c for c in used_palette}
    cells = [
        [used_palette[int(matched_idx[y * w + x])]["id"] for x in range(w)]
        for y in range(h)
    ]
    usage_list = sorted(
        ((by_id[cid], count) for cid, count in usage.items() if cid in by_id),
        key=lambda item: (_natural_key(item[0]["code"]), -item[1]),
    )
    return GenerateResult(brand=brand, width=w, height=h, cells=cells, palette=used_palette, usage=usage_list)


# ---------------------------------------------------------------- 渲染（png-exporter.ts 移植 + 信息条）

def _get_legend_layout(used_count: int, total_w: int, border_w: int, cell_size: int, measure) -> dict:
    font_px = max(14, round(cell_size * 0.3))
    gap = max(6, round(cell_size * 0.14))
    chip_h = max(28, round(font_px * 1.85))
    usable_w = max(1, total_w - border_w * 2 - gap * 2)
    min_chip_w = max(round(cell_size * 2.2), round(6 * font_px * 0.62 + font_px * 1.6))
    columns = max(1, math.floor((usable_w + gap) / (min_chip_w + gap)))
    chip_w = math.floor((usable_w - gap * (columns - 1)) / columns)
    rows = math.ceil(used_count / columns) if used_count else 0
    padding_top = max(10, round(cell_size * 0.25))
    padding_bottom = max(12, round(cell_size * 0.28))
    height = padding_top + rows * chip_h + max(0, rows - 1) * gap + padding_bottom if used_count else 0
    return {
        "columns": columns, "chip_w": chip_w, "chip_h": chip_h, "gap": gap,
        "font_px": font_px, "padding_top": padding_top, "height": height,
    }


def render_pattern_png(
    result: GenerateResult,
    *,
    cell_size: int = 60,
    show_grid: bool = True,
    show_codes: bool = True,
    watermark: str = "",
) -> Image.Image:
    width, height = result.width, result.height
    by_id = {c["id"]: c for c in result.palette}

    max_by_dim = math.floor(MAX_CANVAS_DIM / max(width, height))
    max_by_pixels = math.floor(math.sqrt(MAX_CANVAS_PIXELS / (width * height)))
    cell = max(1, min(cell_size, max_by_dim, max_by_pixels))

    border_w = max(2, round(cell * 0.4))
    grid_w = width * cell
    grid_h = height * cell
    total_w = grid_w + border_w * 2

    layout = _get_legend_layout(len(result.usage), total_w, border_w, cell, None)
    legend_h = layout["height"]
    info_font_px = layout["font_px"]
    info_h = round(info_font_px * 1.9)
    total_h = grid_h + border_w * 2 + legend_h + info_h

    img = Image.new("RGB", (total_w, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    code_font = get_font(max(8, cell * 0.35))
    legend_font = get_font(layout["font_px"])
    info_font = get_font(info_font_px)

    # 1. 色块 + 色号
    ox, oy = border_w, border_w
    for y in range(height):
        for x in range(width):
            color = by_id[result.cells[y][x]]
            px, py = ox + x * cell, oy + y * cell
            draw.rectangle([px, py, px + cell, py + cell], fill=tuple(color["rgb"]))
            if show_codes and cell >= 16:
                r, g, b = color["rgb"]
                text_color = (0, 0, 0) if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 else (255, 255, 255)
                draw.text((px + cell / 2, py + cell / 2), color["code"], font=code_font,
                          fill=text_color, anchor="mm")

    # 2. 半透明线条：细网格 → 中粗子网格 → 最粗拼板线（单层 RGBA 叠加后合成）
    if show_grid:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for bx in range(width + 1):
            odraw.line([(ox + bx * cell, oy), (ox + bx * cell, oy + grid_h)], fill=(0, 0, 0, 64), width=1)
        for by in range(height + 1):
            odraw.line([(ox, oy + by * cell), (ox + grid_w, oy + by * cell)], fill=(0, 0, 0, 64), width=1)
        mid_w = max(1, round(cell * 0.04))
        for bx in range(SUB_GRID, width, SUB_GRID):
            if bx % BOARD_SIZE == 0:
                continue
            odraw.line([(ox + bx * cell, oy), (ox + bx * cell, oy + grid_h)], fill=(0, 0, 0, 102), width=mid_w)
        for by in range(SUB_GRID, height, SUB_GRID):
            if by % BOARD_SIZE == 0:
                continue
            odraw.line([(ox, oy + by * cell), (ox + grid_w, oy + by * cell)], fill=(0, 0, 0, 102), width=mid_w)
        thick_w = max(2, round(cell * 0.08))
        for bx in range(0, width + 1, BOARD_SIZE):
            odraw.line([(ox + min(bx, width) * cell, oy), (ox + min(bx, width) * cell, oy + grid_h)],
                       fill=(0, 0, 0, 178), width=thick_w)
        for by in range(0, height + 1, BOARD_SIZE):
            odraw.line([(ox, oy + min(by, height) * cell), (ox + grid_w, oy + min(by, height) * cell)],
                       fill=(0, 0, 0, 178), width=thick_w)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

    # 3. 蓝紫色外框（网格区四周，不含图例区）
    draw.rectangle([0, 0, total_w - 1, grid_h + 2 * border_w - 1],
                   outline=BORDER_COLOR, width=border_w)

    # 4. 底部用量图例：圆角色块「色号（数量）」
    if legend_h > 0 and result.usage:
        legend_top = oy + grid_h + border_w + layout["padding_top"]
        for i, (color, count) in enumerate(result.usage):
            col = i % layout["columns"]
            row = i // layout["columns"]
            x = border_w + layout["gap"] + col * (layout["chip_w"] + layout["gap"])
            y = legend_top + row * (layout["chip_h"] + layout["gap"])
            radius = max(4, round(layout["chip_h"] * 0.18))
            label = f"{color['code']}（{count}）"
            r, g, b = color["rgb"]
            draw.rounded_rectangle(
                [x, y, x + layout["chip_w"], y + layout["chip_h"]], radius=radius,
                fill=tuple(color["rgb"]), outline=tuple(int(c * 0.82) for c in color["rgb"]), width=1,
            )
            text_color = (0x11, 0x18, 0x27) if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.58 else (255, 255, 255)
            draw.text((x + layout["chip_w"] / 2, y + layout["chip_h"] / 2), label,
                      font=legend_font, fill=text_color, anchor="mm")

    # 5. 信息条（Pindo 原版没有，按需求追加）：机器人名 · 品牌 ｜ W × H ｜ 共 N 粒
    if info_h > 0:
        bar_top = oy + grid_h + border_w + legend_h
        draw.line([(0, bar_top), (total_w, bar_top)], fill=INFO_SEPARATOR, width=1)
        total_beads = sum(count for _, count in result.usage)
        label = BRAND_LABELS.get(result.brand, result.brand)
        text = f"{watermark} · {label} ｜ {width} × {height} ｜ 共 {total_beads:,} 粒" if watermark \
            else f"{label} ｜ {width} × {height} ｜ 共 {total_beads:,} 粒"
        draw.text((total_w / 2, bar_top + info_h / 2), text, font=info_font,
                  fill=INFO_TEXT_COLOR, anchor="mm")

    return img
