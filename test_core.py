"""astrbot_plugin_pindo 引擎自检。

用法：python test_core.py
可选对拍：设置 PINDO_REPO 指向 Pindo 检出目录且本机有 Node ≥22.6 时，
额外与原版 TypeScript 引擎（downscaler / color-matcher）做逐像素对拍。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pindo_core import (
    BRAND_LABELS,
    BRAND_ORDER,
    COMMAND_RE,
    _get_legend_layout,
    _match_indices,
    _natural_key,
    compute_size,
    downscale_average,
    generate,
    limit_palette_with_key_colors,
    load_palette,
    render_pattern_png,
    resolve_brand,
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
        s = lcg(s)
        data[i * 3] = s & 255
        s = lcg(s)
        data[i * 3 + 1] = s & 255
        s = lcg(s)
        data[i * 3 + 2] = s & 255
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
    for text, ok in [
        ("拼豆", True),
        ("/拼豆", True),
        ("拼豆 MARD", True),
        ("拼豆 1", True),
        ("拼豆品牌方", True),
        ("拼豆 品牌方", True),
        (" 拼豆 mard ", True),
        ("拼豆真好玩", False),
        ("一起来拼豆", False),
        ("拼豆mard", False),
    ]:
        check(f"正则 {text!r} → {ok}", bool(COMMAND_RE.match(text)) == ok)
    m = COMMAND_RE.match("拼豆品牌方")
    check(
        "拼豆品牌方 走第二分支捕获组",
        m is not None and m.group(1) is None and m.group(2) == "品牌方",
    )
    m = COMMAND_RE.match("/拼豆品牌方")
    check("/拼豆品牌方 同样捕获", m is not None and (m.group(1) or m.group(2)) == "品牌方")
    m = COMMAND_RE.match("拼豆 品牌方")
    check("拼豆 品牌方 走第一分支捕获组", m is not None and m.group(1) == "品牌方")
    m = COMMAND_RE.match("拼豆")
    check("拼豆 无参数两组皆空", m is not None and m.group(1) is None and m.group(2) is None)
    check(
        "品牌表 8 家且顺序固定",
        BRAND_ORDER
        == ("mard", "coco", "manman", "panpan", "mixiaowo", "hama", "perler", "artkal-s"),
    )


# ---------------------------------------------------------------- 2. 降采样


def test_downscale() -> None:
    print("[2] 线性平均降采样")
    solid = np.full((8, 8, 3), (120, 150, 180), dtype=np.uint8)
    out = downscale_average(solid, 4, 4)
    check("纯色输入输出一致", np.all(out == out[0, 0]))
    check(
        "纯色往返偏差 ≤1",
        all(abs(int(out[0, 0][i]) - c) <= 1 for i, c in enumerate((120, 150, 180))),
        f"got {out[0, 0].tolist()}",
    )
    grad = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1))
    grad = np.stack([grad] * 3, axis=-1)
    out2 = downscale_average(grad, 8, 8)
    check("单调梯度保持单调", bool(np.all(np.diff(out2[0, :, 0].astype(int)) >= 0)))
    check("输出尺寸 8×8", out2.shape == (8, 8, 3))


# ---------------------------------------------------------------- 3. 与原版 TS 引擎对拍


def test_parity() -> None:
    repo = os.environ.get("PINDO_REPO")
    have_node = (
        subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=False
        ).returncode
        == 0
    )
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
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            check("TS 对拍运行", False, proc.stderr[-500:])
            return
        ts = json.loads(Path(out_file).read_text(encoding="utf-8"))

    py_img = make_parity_image()
    py_down = downscale_average(py_img, ts["dstW"], ts["dstH"]).reshape(-1, 3)
    ts_down = np.array(ts["downscaled"], dtype=np.uint8)
    check(
        f"降采样逐像素一致（{ts['dstW']}×{ts['dstH']}）",
        np.array_equal(py_down, ts_down),
        f"差异 {int((py_down.astype(int) != ts_down.astype(int)).any(axis=1).sum())} 像素",
    )

    py_idx = _match_indices(py_down, load_palette("mard"))
    palette = load_palette("mard")
    py_ids = [palette[i]["id"] for i in py_idx]
    check(
        "CIEDE2000 最近色逐像素一致",
        py_ids == list(ts["matched"]),
        f"差异 {sum(1 for a, b in zip(py_ids, ts['matched']) if a != b)} 像素",
    )


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
    for y in range(20):  # 左上角透明区 → 应合成到白底
        for x in range(20):
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
        check(
            "所有 colorId 在色板内",
            all(cid in {c["id"] for c in r1.palette} for row in r1.cells for cid in row),
        )
        check("确定性：两次生成一致", r1.cells == r2.cells)
        check(
            "用量排序：色号自然序",
            [_natural_key(c["code"]) for c, _ in r1.usage]
            == sorted(_natural_key(c["code"]) for c, _ in r1.usage),
        )

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
        check(
            "画布高度含图例与信息条",
            img_plain.size[1] == expected_h,
            f"got {img_plain.size[1]} != {expected_h}",
        )
        check("水印使图片不同", list(img_plain.getdata()) != list(img_marked.getdata()))

        bar_top = border * 2 + result.height * cell + layout["height"]
        sep = img_plain.getpixel((5, bar_top))
        check(
            "信息条分隔线颜色",
            all(abs(a - b) <= 2 for a, b in zip(sep, (229, 231, 235))),
            f"got {sep}",
        )

        bar = img_plain.crop((0, bar_top + 1, img_plain.size[0], img_plain.size[1]))
        darkest = min(min(px) for px in bar.getdata())
        check("信息条有文字像素", darkest < 150, f"min={darkest}")

        legend_top = border * 2 + result.height * cell
        legend_region = img_plain.crop(
            (border, legend_top, img_plain.size[0] - border, legend_top + layout["height"])
        )
        colors_in_legend = {px for px in legend_region.getdata() if px != (255, 255, 255)}
        used_hexes = {tuple(c["rgb"]) for c, _ in result.usage}
        check(
            "图例区出现用量色块",
            any(
                any(all(abs(a - b) <= 6 for a, b in zip(px, h)) for h in used_hexes)
                for px in list(colors_in_legend)[:500]
            ),
        )

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
    by_luma = sorted(
        palette, key=lambda c: 0.2126 * c["rgb"][0] + 0.7152 * c["rgb"][1] + 0.0722 * c["rgb"][2]
    )
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
        ok = len(pal) > 0 and all(
            isinstance(c.get("id"), str)
            and isinstance(c.get("code"), str)
            and len(c.get("rgb", [])) == 3
            and len(c.get("lab", [])) == 3
            for c in pal
        )
        check(f"{BRAND_LABELS[brand]} {len(pal)} 色结构完整", ok)


def test_interaction_logic() -> None:
    """交互全链路：直发不引用、stop_event、图片 > @头像、补图与撤销、超时静默。"""
    print("[8] 收图交互决策（main.py 包式加载，假事件全链路）")
    import asyncio
    import importlib
    import logging
    import types

    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("t")
    api.AstrBotConfig = type("AstrBotConfig", (dict,), {})
    event_m = types.ModuleType("astrbot.api.event")
    event_m.AstrMessageEvent = object

    class FakeChain:
        def __init__(self):
            self.items = []

        def message(self, text):
            self.items.append(("text", text))
            return self

        def file_image(self, path):
            self.items.append(("file", str(path)))
            return self

    event_m.MessageChain = FakeChain
    event_m.filter = types.SimpleNamespace(
        regex=lambda *a, **k: lambda f: f,
        event_message_type=lambda *a, **k: lambda f: f,
        EventMessageType=types.SimpleNamespace(ALL=1),
    )
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = type("Star", (), {"__init__": lambda self, ctx: None})
    comp = types.ModuleType("astrbot.api.message_components")

    class Image:
        from_url = None
        download_target = None  # 桩：模拟框架把 url 下载为本地路径

        def __init__(self, path=None, url=None):
            self.path, self.url, self.file = path, url, None

        async def convert_to_file_path(self):
            if self.path:
                return self.path
            if self.url and type(self).download_target:
                return type(self).download_target  # 框架行为：URL 自动下载
            raise RuntimeError("no local file (avatar url)")

        @classmethod
        def fromURL(cls, url):
            return cls(url=url)

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path=path)

    class Reply:
        def __init__(self, chain=None):
            self.chain = chain or []

    class At:
        def __init__(self, qq):
            self.qq = qq

    comp.Image, comp.Reply, comp.At = Image, Reply, At
    sys.modules.update(
        {
            "astrbot": pkg,
            "astrbot.api": api,
            "astrbot.api.event": event_m,
            "astrbot.api.star": star,
            "astrbot.api.message_components": comp,
        }
    )
    parent = str(Path(__file__).resolve().parent.parent)
    sys.path.insert(0, parent)

    class FakeEvent:
        def __init__(
            self, chain=(), text="", sender="10001", platform="aiocqhttp", umo="aiocqhttp:Group_1"
        ):
            self._chain = list(chain)
            self.message_str = text
            self.sender, self.platform, self.unified_msg_origin = sender, platform, umo
            self.sent = []
            self.stopped = False

        def get_messages(self):
            return self._chain

        def get_sender_id(self):
            return self.sender

        def get_platform_name(self):
            return self.platform

        async def send(self, chain):
            self.sent.append(list(chain.items))

        def stop_event(self):
            self.stopped = True

    async def scenarios(mod):
        P = mod.PindoPlugin
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.png"
            _make_photo(src)

            # 1. 品牌方列表
            p = P(object(), {})
            ev = FakeEvent(text="拼豆品牌方")
            await p.pindou(ev)
            texts = [t for kind, t in (item for s in ev.sent for item in s) if kind == "text"]
            check(
                "品牌方返回列表而非图片",
                len(ev.sent) == 1
                and all(k == "text" for s in ev.sent for k, _ in s)
                and "1. MARD" in texts[0]
                and "8. Artkal S" in texts[0],
                f"got {ev.sent}",
            )
            check("品牌方后 stop_event", ev.stopped)

            # 2. 拼豆 + 图片 → 单条消息直发图片（未配置网站地址，无提示）
            p2 = P(object(), {"robot_watermark": "云霄"})
            ev2 = FakeEvent(chain=[Image(path=str(src))], text="拼豆")
            await p2.pindou(ev2)
            files = [t for s in ev2.sent for k, t in s if k == "file"]
            check(
                "有图单条消息直发图片",
                len(ev2.sent) == 1 and len(files) == 1 and Path(files[0]).exists(),
                f"got {ev2.sent}",
            )
            check("出图后 stop_event", ev2.stopped)
            check("未配置网站地址时不发提示", [k for k, _ in ev2.sent[0]] == ["file"])

            # 2b. 开关开 → 图与提示同一条消息，图在前；{url} 替换为网站地址
            p2b = P(object(), {"site_url": "http://1.2.3.4:8765", "send_site_hint": True})
            ev2b = FakeEvent(chain=[Image(path=str(src))], text="拼豆")
            await p2b.pindou(ev2b)
            s0 = ev2b.sent[0] if ev2b.sent else []
            check(
                "图与提示同一条消息（图在前）",
                len(ev2b.sent) == 1
                and len(s0) == 2
                and s0[0][0] == "file"
                and s0[1] == ("text", "完整功能请前往 http://1.2.3.4:8765"),
                f"got {ev2b.sent}",
            )

            # 2c. 开关关（新默认） → 单条消息只发图
            p2c = P(object(), {"site_url": "http://1.2.3.4:8765"})
            ev2c = FakeEvent(chain=[Image(path=str(src))], text="拼豆")
            await p2c.pindou(ev2c)
            check(
                "开关默认关闭时只发图",
                len(ev2c.sent) == 1 and [k for k, _ in ev2c.sent[0]] == ["file"],
            )

            # 2d. 自定义模板：{url} 占位符 / 无占位符 / 渲染后为空
            p2d = P(
                object(),
                {
                    "send_site_hint": True,
                    "site_url": "http://x:8765",
                    "site_hint_text": "拼豆完整版在 {url} 哦",
                },
            )
            ev2d = FakeEvent(chain=[Image(path=str(src))], text="拼豆")
            await p2d.pindou(ev2d)
            check(
                "自定义模板替换 {url}",
                ev2d.sent[0][-1] == ("text", "拼豆完整版在 http://x:8765 哦"),
            )
            p2e = P(object(), {"send_site_hint": True, "site_hint_text": "纯文字没有占位符"})
            ev2e = FakeEvent(chain=[Image(path=str(src))], text="拼豆")
            await p2e.pindou(ev2e)
            check("无占位符原样发送", ev2e.sent[0][-1] == ("text", "纯文字没有占位符"))
            p2f = P(object(), {"send_site_hint": True, "site_hint_text": "  "})
            ev2f = FakeEvent(chain=[Image(path=str(src))], text="拼豆")
            await p2f.pindou(ev2f)
            check("渲染后为空不附加", [k for k, _ in ev2f.sent[0]] == ["file"])

            # 2g. 默认品牌用显示名（含中文），旧小写 id 兼容
            p2g = P(object(), {"default_brand": "漫漫"})
            check("显示名 漫漫 → manman", p2g._default_brand() == "manman")
            p2h = P(object(), {"default_brand": "Artkal S"})
            check("显示名 Artkal S → artkal-s", p2h._default_brand() == "artkal-s")
            p2i = P(object(), {"default_brand": "mard"})
            check("旧小写 id 仍兼容", p2i._default_brand() == "mard")

            # 3. @ + 图片同存 → 图片优先，头像不参与
            p3 = P(object(), {})
            ev3 = FakeEvent(chain=[At("114514"), Image(path=str(src))], text="拼豆")
            await p3.pindou(ev3)
            check("@+图片时图片优先", len([t for s in ev3.sent for k, t in s if k == "file"]) == 1)

            # 4. 无图仅 @ → 取被 @ 者头像出图
            p4 = P(object(), {})
            Image.download_target = str(src)
            ev4 = FakeEvent(chain=[At("222333")], text="拼豆 coco")
            await p4.pindou(ev4)
            check(
                "仅 @ 时取被 @ 者头像出图",
                len([t for s in ev4.sent for k, t in s if k == "file"]) == 1,
            )

            p5 = P(object(), {})
            ev5 = FakeEvent(text="拼豆 coco")
            await p5.pindou(ev5)
            prompts = [t for s in ev5.sent for k, t in s if k == "text"]
            check(
                "无图无 @ 进入补图等待",
                "30 秒内发送" in prompts[0] and len(p5._pending) == 1,
                f"got {ev5.sent}",
            )
            ev5b = FakeEvent(chain=[Image(path=str(src))], text="[图片]")
            await p5.on_message(ev5b)
            check(
                "补图出图并清空等待",
                len(p5._pending) == 0
                and len([t for s in ev5b.sent for k, t in s if k == "file"]) == 1,
            )

            # 5. 撤销
            p6 = P(object(), {})
            ev6 = FakeEvent(text="拼豆")
            await p6.pindou(ev6)
            ev6b = FakeEvent(text="撤销")
            await p6.on_message(ev6b)
            texts6 = [t for s in ev6b.sent for k, t in s if k == "text"]
            check("撤销回「已取消」并清空等待", texts6 == ["已取消"] and len(p6._pending) == 0)

            # 6. 超时静默
            p7 = P(object(), {"wait_timeout": 1})
            ev7 = FakeEvent(text="拼豆")
            await p7.pindou(ev7)
            key = next(iter(p7._pending))
            n_sent = len(ev7.sent)
            p7._expire(key)
            await asyncio.sleep(0.05)
            check("超时静默：无任何提示", len(p7._pending) == 0 and len(ev7.sent) == n_sent)

            # 7. 未知品牌
            p8 = P(object(), {})
            ev8 = FakeEvent(text="拼豆 不存在的牌子")
            await p8.pindou(ev8)
            texts8 = [t for s in ev8.sent for k, t in s if k == "text"]
            check("未知品牌给出引导", "未知的品牌" in texts8[0] and "拼豆品牌方" in texts8[0])

            # 8. 非 QQ 平台无 @ 头像通道
            p9 = P(object(), {})
            ev9 = FakeEvent(text="拼豆", platform="telegram")
            await p9.pindou(ev9)
            check("非 QQ 平台无头像兜底，进等待", "30 秒内发送" in ev9.sent[0][0][1])

    try:
        mod = importlib.import_module("astrbot_plugin_pindo.main")
        asyncio.run(scenarios(mod))
    finally:
        sys.path.remove(parent)
        for k in [k for k in sys.modules if k.startswith("astrbot_plugin_pindo")]:
            del sys.modules[k]


def test_webui_title_rewrite() -> None:
    """SiteServer 标题/Logo 替换：html/manifest/JS bundle 生效、二进制不动、未配置用内置。"""
    print("[9] WebUI 标题与 Logo 自定义替换")
    import asyncio

    import aiohttp

    from web_server import DEFAULT_SITE_TITLE as DEFAULT_TITLE
    from web_server import SiteServer

    pinv = Path(__file__).resolve().parent
    webui = pinv / "webui"
    title_chunk_rel = next(
        p.relative_to(webui).as_posix()
        for p in sorted((webui / "_next").rglob("*.js"))
        if DEFAULT_TITLE.encode() in p.read_bytes()
    )
    logo_chunk_rel = next(
        p.relative_to(webui).as_posix()
        for p in sorted((webui / "_next").rglob("*.js"))
        if b"__SITE_LOGO__" in p.read_bytes()
    )
    png_rel = next(p.relative_to(webui).as_posix() for p in sorted(webui.rglob("*.png")))
    png_bytes = (webui / png_rel).read_bytes()

    async def fetch(session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url) as resp:
            return await resp.text()

    async def run() -> None:
        custom = "沐倾的拼豆小站"
        s = SiteServer(
            webui,
            host="127.0.0.1",
            port=8791,
            site_title=custom,
            site_logo="https://example.com/my-logo.png",
        )
        await s.start()
        try:
            async with aiohttp.ClientSession() as http:
                index = await fetch(http, "http://127.0.0.1:8791/")
                check("首页标题替换", custom in index and DEFAULT_TITLE not in index)
                check(
                    "首页 Logo 替换为自定义链接",
                    'href="https://example.com/my-logo.png"' in index
                    and 'src="https://example.com/my-logo.png"' in index
                    and "__SITE_LOGO__" not in index,
                )
                manifest = await fetch(http, "http://127.0.0.1:8791/manifest.json")
                check("manifest 名称替换", custom in manifest)
                focus = await fetch(http, "http://127.0.0.1:8791/focus")
                check("专注页标题替换", custom in focus)
                chunk = await fetch(http, f"http://127.0.0.1:8791/{title_chunk_rel}")
                check(
                    "JS bundle 内标题替换（hydration 源）",
                    custom in chunk and DEFAULT_TITLE not in chunk,
                )
                logo_chunk = await fetch(http, f"http://127.0.0.1:8791/{logo_chunk_rel}")
                check(
                    "JS bundle 内 Logo 占位符替换",
                    "https://example.com/my-logo.png" in logo_chunk
                    and "__SITE_LOGO__" not in logo_chunk,
                )
                async with (
                    aiohttp.ClientSession() as http2,
                    http2.get(f"http://127.0.0.1:8791/{png_rel}") as resp,
                ):
                    body = await resp.read()
                    check(
                        "二进制资源字节不变",
                        resp.status == 200
                        and resp.content_type == "image/png"
                        and body == png_bytes,
                    )
        finally:
            await s.stop()

        s2 = SiteServer(webui, host="127.0.0.1", port=8792)
        await s2.start()
        try:
            async with aiohttp.ClientSession() as http:
                index = await fetch(http, "http://127.0.0.1:8792/")
                check("未配置时保持默认标题", DEFAULT_TITLE in index)
                check(
                    "未配置 Logo 时用内置 /logo.png",
                    'href="/logo.png"' in index
                    and 'src="/logo.png"' in index
                    and "__SITE_LOGO__" not in index,
                )
                async with http.get("http://127.0.0.1:8792/logo.png") as resp:
                    body = await resp.read()
                    check(
                        "内置 logo.png 可访问",
                        resp.status == 200 and resp.content_type == "image/png" and len(body) > 0,
                    )
        finally:
            await s2.stop()

    asyncio.run(run())


def main() -> int:
    test_brand_and_command()
    test_downscale()
    test_parity()
    test_pipeline()
    test_render()
    test_palette_limit()
    test_palettes()
    test_interaction_logic()
    test_webui_title_rewrite()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
