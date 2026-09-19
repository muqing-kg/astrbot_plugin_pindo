# astrbot_plugin_pindo

拼豆图纸生成器 AstrBot 插件：聊天中发送图片，机器人返回带色号与用量统计的拼豆图纸 PNG；同时内置可对外访问的 Pindo 网页版 WebUI。

引擎移植自开源项目 [LunarXuan/Pindo](https://github.com/LunarXuan/Pindo)（GPL-3.0），本插件为该项目的衍生作品，依据 GPL-3.0 同样以 GPL-3.0 开源发布。

## 使用

| 命令 | 说明 |
| --- | --- |
| `拼豆` 或 `/拼豆` | 用默认品牌（MARD）把图片转成图纸 |
| `拼豆 MARD` | 指定品牌（支持序号、品牌名大小写不敏感、`artkal` 简写） |
| `拼豆品牌方` | 列出全部 8 个品牌（带序号） |

- 收图优先级：消息内图片 > 引用图 > 被 @ 用户的 QQ 头像（仅 QQ 平台；只有消息中 @ 了某位用户才读取其头像，未 @ 时不读取）。
- 三者皆无时会提示在 30 秒内补发图片，「撤销」取消，超时静默结束；只认发起人本人的消息，多张图片只取第一张。
- 图纸不锁定比例：默认宽 35 颗、高按原图比例换算，任意一边超过 120 颗时等比压缩到 120。
- 输出为一张 PNG：网格、每 5 格中粗线、每 29 格拼板粗线、蓝紫色外框、底部圆角色块用量图例「色号（数量）」，最底部为信息条（机器人名 · 品牌 ｜ 尺寸 ｜ 总颗数）。
- 处理参数与 Pindo 网页默认一致：写实模式、平均池化、无抖动、白底、最多 16 色（CIEDE2000 关键色保护限色）。
- 回复经 `event.send(MessageChain)` 直发：不引用原消息、不附加 @，绕过框架结果装饰管线与其他插件的 `on_decorating_result` 钩子；发送后 `stop_event()` 隔离后续插件与 LLM 兜底。

## WebUI

插件自带 Pindo 静态站点，默认 `0.0.0.0:8765` 对外提供访问（`/` 主站、`/focus` 专注模式）。端口与开关在插件配置中修改，修改后需重载插件。公网访问需在防火墙/云安全组放行端口。

## 安装

将本仓库克隆到 AstrBot 的 `data/plugins/astrbot_plugin_pindo`，或在 WebUI 插件页从本地上传安装，随后重载插件。依赖仅 `numpy`（Pillow、aiohttp 均为 AstrBot 框架自带）。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `default_brand` | `MARD` | 默认品牌色板（显示名：MARD/COCO/漫漫/盼盼/咪小窝/Hama/Perler/Artkal S） |
| `robot_watermark` | 空 | 图纸底部信息条的机器人名，留空不显示 |
| `wait_timeout` | `30` | 补图等待秒数，超时静默 |
| `max_long_edge` | `120` | 图纸最长边颗数上限 |
| `enable_site` | `true` | 是否启用内置 WebUI |
| `site_host` / `site_port` | `0.0.0.0` / `8765` | WebUI 监听地址与端口 |
| `site_title` | 空 | 网站标题（页签与页面大标题），留空用默认「Pindo 拼豆图纸生成器」 |
| `site_url` | 空 | WebUI 外部访问地址；供附带文字的 `{url}` 占位符替换 |
| `send_site_hint` | `false` | 出图时附带下方自定义文字（渲染后为空则不发送） |
| `site_hint_text` | `完整功能请前往 {url}` | 出图附带文字模板，`{url}` 替换为外部访问地址，不含占位符则原样发送 |

## 开发自检

仓库根目录 `test_core.py` 为引擎自检：核心不变量断言 + 与 Pindo 原版 TypeScript 引擎的对拍（需环境变量 `PINDO_REPO` 指向 Pindo 检出目录、本机装有 Node ≥22.6）：

```bash
PINDO_REPO=/path/to/Pindo python test_core.py
```

## 许可证

Copyright © 2026 LunarXuan（原项目 Pindo）；本插件修改与新增部分 Copyright © 2026 沐倾。

本程序自由软件，依据 [GNU 通用公共许可证第 3 版](LICENSE) 授权，不附带任何担保。`fonts/` 目录下的 Noto Sans SC 字体依据 SIL Open Font License 1.1 单独授权。
