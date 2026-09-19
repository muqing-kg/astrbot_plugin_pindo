// Pindo 原版 TypeScript 引擎对拍驱动：node --experimental-strip-types parity_runner.ts
// 从环境变量 PINDO_REPO 读取 Pindo 检出目录，OUT 指定输出 JSON 路径。
// 生成确定性伪随机图像，跑原版 downscale('average') 与 matchColors（MARD 色板）。
import { writeFileSync, readFileSync } from 'node:fs';

const repo = process.env.PINDO_REPO.replace(/\\/g, '/').replace(/\/$/, '');
const { downscale } = await import(`file:///${repo}/lib/engine/downscaler.ts`);
const { matchColors } = await import(`file:///${repo}/lib/engine/color-matcher.ts`);

const SRC_W = 700, SRC_H = 400, DST_W = 35, DST_H = 20;

// 与 test_core.py 中 lcg()/make_image() 严格一致的确定性图像
function lcg(s) { return (Math.imul(s, 1664525) + 1013904223) >>> 0; }

const data = new Uint8ClampedArray(SRC_W * SRC_H * 4);
let s = 42 >>> 0;
for (let i = 0; i < data.length; i += 4) {
  s = lcg(s); data[i] = s & 255;
  s = lcg(s); data[i + 1] = s & 255;
  s = lcg(s); data[i + 2] = s & 255;
  data[i + 3] = 255;
}

const pixels = downscale(data, SRC_W, SRC_H, DST_W, DST_H, 'average');

const palette = JSON.parse(readFileSync(`${repo}/lib/data/palettes/mard.json`, 'utf-8'));
const matched = matchColors(pixels, palette);

writeFileSync(process.env.OUT, JSON.stringify({
  dstW: DST_W, dstH: DST_H,
  downscaled: pixels.map(p => [p.r, p.g, p.b]),
  matched: matched.map(c => c.id),
}));
console.log('parity runner done');
