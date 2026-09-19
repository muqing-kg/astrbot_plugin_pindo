"""astrbot_plugin_pindo 引擎自检。

用法：python test_core.py
可选对拍：设置 PINDO_REPO 指向 Pindo 检出目录且本机有 Node ≥22.6 时，
额外与原版 TypeScript 引擎（downscaler / color-matcher）做逐像素对拍。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pindo_core
from pindo_core import (
    BRAND_LABELS, BRAND_ORDER, COMMAND_RE, _get_legend_layout, _match_indices,
    _natural_key, compute_size, downscale_average, generate, limit_palette_with_key_colors,
    load_palette, render_pattern_png, resolve_brand,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def lcg(s: int) -> int:
    return (s * 1664525 + 1013904223) & 0xFFFFFFFF


def make_parity_image() -> np.ndarray:
    """与 tools/parity_runner.ts 严格一致的确定性图像 (700,400,3)。"""
    w, h = 700, 400
    data = np.empty(h * w * 3, dtype=np.uint8)
    s = 42
    for i in range(h * w):
        s = lcg(s); data[i * 3] = s & 255
        s = lcg(s); data[i * 3 + 1] = s & 255
        s = lcg(s); data[i * 3 + 2] = s & 255
    return data.reshape(h, w, 3)


# ---------------------------------------------------------------- 1. 品牌与命令解析

def test_brand_and_command() -> None:
    print("[1] 品牌别名与命令解析")
    check("默认序号 1 → mard", resolve_brand("1") == "mard")
    check("序号 8 → artkal-s", resolve_brand("8") == "artkal-s")
    check("序号越界 → None", resolve_brand("9") is None)
    check("大写 MARD", resolve_brand("MARD") == "mard")
    check("中文 漫漫", resolve_brand("漫漫") == "manman")
    check("artkal 简写", resolve_brand("artkal") == "artkal-s")
    check("小写 panpan", resolve_brand("panpan") == "panpan")
    check("未知品牌 → None", resolve_brand("foo") is None)
    check("空串 → None", resolve_brand("") is None)
    for text, ok in [("拼豆", True), ("/拼豆", True), ("拼豆 MARD", True), ("拼豆 1", True),
                     ("拼豆品牌方", True), ("拼豆 品牌方", True), (" 拼豆 mard ", True),
                     ("拼豆真好玩", False), ("一起来拼豆", False), ("拼豆mard", False)]:
        check(f"正则 {text!r} → {ok}", bool(COMMAND_RE.match(text)) == ok)
    m = COMMAND_RE.match("拼豆品牌方")
    check("拼豆品牌方 走第二分支捕获组", m is not None and m.group(1) is None and m.group(2) == "品牌方")
    m = COMMAND_RE.match("/拼豆品牌方")
    check("/拼豆品牌方 同样捕获", m is not None and (m.group(1) or m.group(2)) == "品牌方")
    m = COMMAND_RE.match("拼豆 品牌方")
    check("拼豆 品牌方 走第一分支捕获组", m is not None and m.group(1) == "品牌方")
    m = COMMAND_RE.match("拼豆")
    check("拼豆 无参数两组皆空", m is not None and m.group(1) is None and m.group(2) is None)
    check("品牌表 8 家且顺序固定", BRAND_ORDER == ("mard", "coco", "manman", "panpan", "mixiaowo", "hama", "perler", "artkal-s"))


# ---------------------------------------------------------------- 2. 降采样

def test_downscale() -> None:
    print("[2] 线性平均降采样")
    solid = np.full((8, 8, 3), (120, 150, 180), dtype=np.uint8)
    out = downscale_average(solid, 4, 4)
    check("纯色输入输出一致", np.all(out == out[0, 0]))
    check("纯色往返偏差 ≤1", all(abs(int(out[0, 0][i]) - c) <= 1 for i, c in enumerate((120, 150, 180))),
          f"got {out[0, 0].tolist()}")
    grad = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1))
    grad = np.stack([grad] * 3, axis=-1)
    out2 = downscale_average(grad, 8, 8)
    check("单调梯度保持单调", bool(np.all(np.diff(out2[0, :, 0].astype(int)) >= 0)))
    check("输出尺寸 8×8", out2.shape == (8, 8, 3))


# ---------------------------------------------------------------- 3. 与原版 TS 引擎对拍

def test_parity() -> None:
    repo = os.environ.get("PINDO_REPO")
    node = "node"
    have_node = subprocess.run(["node", "--version"], capture_output=True, text=True).returncode == 0
    if not repo or not Path(repo).is_dir() or not have_node:
        print("[3] 原版 TS 对拍：跳过（未设置 PINDO_REPO 或无 node）")
        return
    print("[3] 与 Pindo 原版 TypeScript 引擎对拍")
    with tempfile.TemporaryDirectory() as td:
        out_file = str(Path(td) / "parity.json")
        env = dict(os.environ, PINDO_REPO=repo, OUT=out_file)
        runner = Path(__file__).resolve().parent / "tools" / "parity_runner.ts"
        proc = subprocess.run(
            ["node", "--experimental-strip-types", "--no-warnings", str(runner)],
            env=env, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            check("TS 对拍运行", False, proc.stderr[-500:])
            return
        ts = json.loads(Path(out_file).read_text(encoding="utf-8"))

    py_img = make_parity_image()
    py_down = downscale_average(py_img, ts["dstW"], ts["dstH"]).reshape(-1, 3)
    ts_down = np.array(ts["downscaled"], dtype=np.uint8)
    check(f"降采样逐像素一致（{ts['dstW']}×{ts['dstH']}）", np.array_equal(py_down, ts_down),
          f"差异 {int((py_down.astype(int) != ts_down.astype(int)).any(axis=1).sum())} 像素")

    py_idx = _match_indices(py_down, load_palette("mard"))
    palette = load_palette("mard")
    py_ids = [palette[i]["id"] for i in py_idx]
    check("CIEDE2000 最近色逐像素一致", py_ids == list(ts["matched"]),
          f"差异 {sum(1 for a, b in zip(py_ids, ts['matched']) if a != b)} 像素")


# ---------------------------------------------------------------- 4. 管线

def _make_photo(path: Path) -> None:
    img = Image.new("RGBA", (700, 400), (255, 255, 255, 255))
    px = img.load()
    for y in range(100, 300):
        for x in range(100, 300):
            px[x, y] = (20, 20, 20, 255)
    for y in range(40, 80):
        for x in range(400, 620):
            px[x, y] = (200, 30, 30, 255)
    for y in range(0, 20):        # 左上角透明区 → 应合成到白底
        for x in range(0, 20):
            px[x, y] = (255, 0, 0, 0)
    img.save(path)


def test_pipeline() -> None:
    print("[4] 生成管线")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "photo.png"
        _make_photo(src)
        r1 = generate(src, "mard")
        r2 = generate(src, "mard")

        check("宽 35", r1.width == 35)
        check("高按比例 = 20", r1.height == 20, f"got {r1.height}")
        check("网格尺寸一致", len(r1.cells) == 20 and all(len(row) == 35 for row in r1.cells))
        check("限色 ≤16", len(r1.palette) <= 16, f"got {len(r1.palette)}")
        check("用量总和 = 总颗数", sum(c for _, c in r1.usage) == 35 * 20)
        check("所有 colorId 在色板内", all(cid in {c['id'] for c in r1.palette} for row in r1.cells for cid in row))
        check("确定性：两次生成一致", r1.cells == r2.cells)
        check("用量排序：色号自然序", [ _natural_key(c['code']) for c, _ in r1.usage ]
              == sorted(_natural_key(c['code']) for c, _ in r1.usage))

        white_expected = load_palette("mard")[
            _match_indices(np.array([[255, 255, 255]], dtype=np.uint8), load_palette("mard"))[0]
        ]["id"]
        check("透明区合成到白底", r1.cells[0][0] == white_expected, f"got {r1.cells[0][0]}")

        # 长边上限
        w, h = compute_size(400, 1400)
        check("1:3.5 长图压缩到 (34,120)", (w, h) == (34, 120), f"got {(w, h)}")
        w, h = compute_size(1400, 400)
        check("横图未超上限保持默认宽 35", (w, h) == (35, 10), f"got {(w, h)}")
        w, h = compute_size(700, 400)
        check("正常比例不受影响", (w, h) == (35, 20), f"got {(w, h)}")


# ---------------------------------------------------------------- 5. 渲染

def test_render() -> None:
    print("[5] 图纸 PNG 渲染")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "photo.png"
        _make_photo(src)
        result = generate(src, "mard")

        cell, border = 60, 24
        total_w = result.width * cell + border * 2
        layout = _get_legend_layout(len(result.usage), total_w, border, cell, None)
        info_h = round(layout["font_px"] * 1.9)
        expected_h = result.height * cell + border * 2 + layout["height"] + info_h

        img_plain = render_pattern_png(result)
        img_marked = render_pattern_png(result, watermark="云霄Bot")
        check("画布宽度 2148", img_plain.size[0] == total_w, f"got {img_plain.size[0]}")
        check("画布高度含图例与信息条", img_plain.size[1] == expected_h, f"got {img_plain.size[1]} != {expected_h}")
        check("水印使图片不同", list(img_plain.getdata()) != list(img_marked.getdata()))

        bar_top = border * 2 + result.height * cell + layout["height"]
        sep = img_plain.getpixel((5, bar_top))
        check("信息条分隔线颜色", all(abs(a - b) <= 2 for a, b in zip(sep, (229, 231, 235))), f"got {sep}")

        bar = img_plain.crop((0, bar_top + 1, img_plain.size[0], img_plain.size[1]))
        darkest = min(min(px) for px in bar.getdata())
        check("信息条有文字像素", darkest < 150, f"min={darkest}")

        legend_top = border * 2 + result.height * cell
        legend_region = img_plain.crop((border, legend_top, img_plain.size[0] - border, legend_top + layout["height"]))
        colors_in_legend = {px for px in legend_region.getdata() if px != (255, 255, 255)}
        used_hexes = {tuple(c["rgb"]) for c, _ in result.usage}
        check("图例区出现用量色块", any(any(all(abs(a - b) <= 6 for a, b in zip(px, h)) for h in used_hexes)
                                        for px in list(colors_in_legend)[:500]))

        out = Path(td) / "out.png"
        img_plain.save(out, format="PNG")
        reloaded = Image.open(out)
        reloaded.load()
        check("PNG 落盘可回读", reloaded.size == img_plain.size)


# ---------------------------------------------------------------- 6. 限色保护

def test_palette_limit() -> None:
    print("[6] 关键色保护限色")
    palette = load_palette("mard")
    usage = {c["id"]: 3 for c in palette}
    limited, protected = limit_palette_with_key_colors(palette, usage, 16)
    ids = {c["id"] for c in limited}
    check("限制到 16 色", len(limited) == 16, f"got {len(limited)}")
    by_luma = sorted(palette, key=lambda c: 0.2126 * c["rgb"][0] + 0.7152 * c["rgb"][1] + 0.0722 * c["rgb"][2])
    check("最暗色被保护", by_luma[0]["id"] in ids)
    check("最亮色被保护", by_luma[-1]["id"] in ids)
    check("protectedIds ⊆ limited", protected <= ids)
    few = limit_palette_with_key_colors(palette, {palette[0]["id"]: 1}, 16)
    check("已用色不足上限时全保留", len(few[0]) == 1)


# ---------------------------------------------------------------- 7. 色板完整性

def test_palettes() -> None:
    print("[7] 色板数据完整性")
    check("8 份色板文件", len(BRAND_ORDER) == 8)
    for brand in BRAND_ORDER:
        pal = load_palette(brand)
        ok = (len(pal) > 0 and all(
            isinstance(c.get("id"), str) and isinstance(c.get("code"), str)
            and len(c.get("rgb", [])) == 3 and len(c.get("lab", [])) == 3
            for c in pal))
        check(f"{BRAND_LABELS[brand]} {len(pal)} 色结构完整", ok)


def main() -> int:
    test_brand_and_command()
    test_downscale()
    test_parity()
    test_pipeline()
    test_render()
    test_palette_limit()
    test_palettes()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
