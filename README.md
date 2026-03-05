# 🕸️ Python Crawler Downloader

<p align="center">
  一个**礼貌型网页爬取工具**，支持按需下载 **图片 / 视频 / 文本内容**。<br/>
  适合做资料归档、页面采集与轻量离线备份。
</p>

---

## ✨ 功能亮点

- ✅ 支持三种抓取类型：`images`、`videos`、`text`
- ✅ 仅同域名爬取，避免无边界扩散
- ✅ 支持爬取深度控制（`--depth`）与页面上限（`--max-pages`）
- ✅ 自动遵守 `robots.txt`（礼貌访问）
- ✅ 请求失败自动重试（429/5xx）
- ✅ 可配置请求头（`--headers`）和访问节奏（`--delay`）

---

## 📦 环境要求

- Python 3.10+
- 依赖：
  - `requests>=2.31.0`
  - `beautifulsoup4>=4.12.0`

安装依赖：

```bash
pip install requests beautifulsoup4
```

---

## 🚀 快速开始

### 1) 下载图片

```bash
python crawler.py \
  --url "https://example.com" \
  --type images \
  --out downloads/images \
  --depth 1 \
  --max-pages 30
```

### 2) 下载视频

```bash
python crawler.py \
  --url "https://example.com" \
  --type videos \
  --out downloads/videos \
  --depth 1
```

### 3) 抓取文本

```bash
python crawler.py \
  --url "https://example.com" \
  --type text \
  --out downloads/text \
  --depth 2
```

---

## ⚙️ 参数说明

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--url` | 起始 URL（必填） | - |
| `--type` | 抓取类型：`images` / `videos` / `text`（必填） | - |
| `--out` | 输出目录 | `downloads` |
| `--depth` | 同域爬取深度 | `0` |
| `--max-pages` | 最多访问 HTML 页面数 | `30` |
| `--timeout` | HTTP 超时时间（秒） | `20` |
| `--delay` | 每次请求间隔（秒） | `1.0` |
| `--headers` | 可选 JSON 请求头字符串 | `None` |
| `--verbose` | 输出调试日志 | `False` |

`--headers` 示例：

```bash
python crawler.py \
  --url "https://example.com" \
  --type images \
  --headers '{"User-Agent":"MyCrawler/1.0","Accept-Language":"zh-CN,zh;q=0.9"}'
```

---

## 📁 输出结构（示例）

```text
downloads/
├── images/
├── videos/
└── text/
```

程序会对文件名做安全处理，并按内容类型自动推断扩展名。

---

## 🤝 使用建议

1. 优先抓取你有权限访问与保存的内容。
2. 合理设置 `--delay`，避免对目标站点造成压力。
3. 大规模抓取前先小范围测试（较小 `--depth` 与 `--max-pages`）。
4. 生产使用建议自定义 `User-Agent` 与请求头。

---

## 📌 免责声明

请遵守目标网站的服务条款、`robots.txt`、以及当地法律法规。使用者应自行承担合规责任。

