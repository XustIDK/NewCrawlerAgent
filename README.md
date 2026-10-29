# 新闻抓取 Agent

输入任意新闻网站网址（不限国家/地区/语言），自动发现新闻列表、抓取文章并抽取
**标题 / 正文 / 发布时间 / 来源 / 作者 / 语言 / 栏目 / 图片**等字段，输出结构化数据集。

本项目由原「小红书调研 Agent」改造而来：保留 DeepSeek Function Calling + Agent Loop、
本地轻量网页、一键启动（run.ps1）、报告/流程图等工程能力，把“站内关键词搜索”替换为
**URL 驱动的通用新闻站点抓取流水线**。改造设计见 [DESIGN.md](DESIGN.md)。

## 主要能力

| 能力 | 说明 |
| --- | --- |
| 任意站点 URL | 输入列表页/首页即可，自动识别站点结构；支持中、英、日、法、德、西、俄等语言站点 |
| 多策略列表发现 | 站点配置 → RSS/Atom → JSON-LD → 链接聚类启发式 → 自动翻页，逐级降级 |
| 多策略正文抽取 | 站点配置选择器 → JSON-LD/OpenGraph → trafilatura 通用算法（未安装时自动降级为内置启发式） |
| 字段归一 | 多语言日期 → ISO 8601、语言识别、URL 归一、去重、正文清洗、置信度 |
| 静态 + 动态页面 | requests 优先；JS 渲染/懒加载/验证码时自动回退 Playwright + 本机 Chrome |
| LLM Agent 模式 | 模型自检字段完整率、探测页面结构、自动生成站点配置、撰写报告（可选，无 Key 也能抓） |
| 结构化输出 | JSONL + CSV + run_meta.json + 报告 Markdown + Agent Loop 流程图（SVG/PNG） |
| 站点配置 | sites/<domain>.json 可声明选择器、翻页、编码、抓取模式，零代码适配特殊站点 |
| 本地网页 | 输入网址、查看进度/人工介入、下载结果，无需前端构建 |

## 工作原理

```text
用户输入新闻网站 URL
      │
      ▼
入口校验 + 站点配置加载（sites/<domain>.json，可选）
      │
      ▼
抓取层：requests 静态抓取 → 失败/JS 页面时 Playwright 浏览器回退
      │        （编码识别 / robots.txt / 限速 / 重试 / 人工介入）
      ▼
新闻列表发现：站点配置 > RSS/Atom > JSON-LD > 链接聚类 > 翻页
      │
      ▼
文章抓取 + 字段抽取：站点配置 > JSON-LD > meta > 通用正文算法
      │
      ▼
字段归一：多语言日期、语言识别、URL 归一、去重、置信度
      │
      ▼
输出：articles.jsonl / articles.csv / run_meta.json / report.md / agent_loop.svg

（可选）LLM Agent Loop：模型调用 inspect_site / crawl_site / save_site_profile /
analyze_coverage / save_outputs 工具，自检、补抓、生成配置并写报告。
```

## 环境要求

- Windows 10/11，PowerShell 5.1 或 PowerShell 7+
- Python 3.10 或更新版本（确保 python 已在 PATH 中）
- Google Chrome（仅浏览器回退 / JS 页面 / 验证码处理需要；纯静态抓取可不装）
- DeepSeek API Key（仅 Agent 模式需要；确定性抓取不需要）

## 安装（Windows + PowerShell）

在项目目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果 Activate.ps1 提示“禁止运行脚本”，只对当前窗口放开执行策略后重试：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

配置 DeepSeek API Key（写入项目目录 .env，不要提交到 git）：

```powershell
Set-Content -Path .env -Value "DEEPSEEK_API_KEY=sk-xxxx" -Encoding utf8
```

## 一键运行

```powershell
.\run.ps1                                          # 启动本地网页（推荐）
.\run.ps1 -Mode cli -Url "https://www.bbc.com/news" -Limit 5
.\run.ps1 -Mode cli -Url "https://news.sina.com.cn/" -NoLlm    # 不调用 LLM
.\run.ps1 -Mode cli -Url "https://example.com/news" -Visible   # 显示浏览器处理验证码
.\run.ps1 -ApiKey "sk-xxxx"                        # 顺便把 Key 写入 .env
.\run.ps1 -ForceReinstall                          # 强制重装依赖
```

## 网页使用

```powershell
.\.venv\Scripts\Activate.ps1
python web_app.py
```

浏览器打开 <http://127.0.0.1:8765>：

1. 输入新闻网站网址（列表页/首页/频道页均可），设置抓取篇数；
2. 选择是否使用 LLM 模式、是否显示浏览器；
3. 提交后实时查看进度；若出现验证码/Cloudflare 拦截，在浏览器处理后点“我已处理，继续抓取”；
4. 完成后查看报告与文章预览，下载 JSONL / CSV / run_meta / 报告 / 流程图。

## 命令行使用

```powershell
.\.venv\Scripts\Activate.ps1

# 确定性抓取（不需要 API Key）
python -m news_agent --url "https://www.bbc.com/news" --limit 5 --no-llm

# Agent 模式（需要 DEEPSEEK_API_KEY）
python -m news_agent --url "https://www.dw.com/en/top-stories/s-9097" --limit 5

# 常用参数
python -m news_agent --url "https://www3.nhk.or.jp/news/" --limit 3 --pages 2 --lang ja --no-llm
python -m news_agent --url "https://www.bbc.com/news" --limit 3 --visible
python -m news_agent --url "https://www.bbc.com/news" --limit 3 --browser never --no-llm
python -m news_agent --url "https://www.bbc.com/news" --limit 3 --out data --json
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| --url | 新闻网站网址，可重复传入多个，逐个抓取 |
| --limit | 抓取文章篇数（默认取 settings 中的 max_articles） |
| --pages | 列表页最多翻页数 |
| --lang | 语言提示（en/zh-CN/ja/fr/de...），用于日期/语言判断 |
| --no-llm | 不调用 LLM，使用确定性流水线 |
| --visible | 显示浏览器窗口（处理验证码/JS 页面） |
| --browser | auto / always / never |
| --no-robots | 不遵守 robots.txt（请确认有权抓取） |
| --out | 输出根目录（默认 data/） |
| --json | 在 stdout 输出结果 JSON 摘要 |
| --quiet | 安静模式 |

## 输出说明

每次抓取输出到 `data/<域名>_<时间戳>/` 目录：

| 文件 | 内容 |
| --- | --- |
| articles.jsonl | 每行一篇结构化文章（schema v1.0） |
| articles.csv | 扁平表格（UTF-8 BOM，Excel 可直接打开） |
| run_meta.json | 入口 URL、配置、发现方式、统计、错误、警告 |
| report.md | 抓取报告（LLM 模式由模型撰写，否则为统计模板报告） |
| agent_loop.svg / .png | 本次工具调用轨迹流程图 |

文章字段（articles.jsonl 每行）：

| 字段 | 说明 |
| --- | --- |
| id | URL 的 16 位 SHA1，主键/去重 |
| url / canonical_url | 原始链接 / 归一化链接 |
| title / summary / body | 标题 / 摘要 / 正文纯文本 |
| published_at / published_raw | ISO 8601 发布时间 / 页面原始时间文本 |
| author / source / section | 作者 / 来源 / 栏目 |
| language / site_domain | 语言 / 站点域名 |
| images | 文章图片绝对 URL 列表 |
| crawled_at | 抓取时间 |
| extract_method / confidence | 抽取策略链路 / 字段置信度 |
| word_count / list_page | 正文长度 / 来源列表页 |

## 站点配置（可选）

没有配置也能抓（通用策略），只有站点结构特殊或需要更高抽取质量时再写配置。
在 `sites/<归一化域名>.json` 中声明选择器即可，例如：

```json
{
  "domain": "example.com",
  "name": "Example News",
  "language": "en",
  "fetch": {"mode": "auto", "encoding": "auto", "wait_selector": ""},
  "list": {
    "item": "article a[href]::attr(href)",
    "item_title": "article h2, article h3",
    "pagination": {"next": "a[rel=next]::attr(href)", "max_pages": 3}
  },
  "article": {
    "title": "h1",
    "body": "article p",
    "date": "time::attr(datetime)",
    "author": "[rel=author]",
    "exclude": [".ad", ".related", ".share"]
  },
  "include": [],
  "exclude": ["/video/", "/live/"]
}
```

选择器支持 CSS 子集与 XPath（xpath: 前缀），取值后缀 `::text` / `::attr(name)` / `::html`。
完整字段说明见 [sites/README.md](sites/README.md)，模板见 [sites/_template.json](sites/_template.json)。
仓库内置 [sites/bbc.com.json](sites/bbc.com.json) 作为真实站点示例（画像命中时列表发现约 4 秒）。
Agent 模式在通用策略效果不佳时会自动探测页面结构并生成站点配置（标记 learned_by: agent）。

## 全局配置

`config/settings.json` 集中管理抓取参数，无需改代码：

- crawl：max_articles / max_list_pages / max_candidates / timeout_s / retries / min_delay_s /
  respect_robots / browser_mode / browser_wait_ms / headless / max_body_chars / default_timezone /
  language_hint / include_patterns / exclude_patterns
- llm：model / base_url / max_rounds / temperature
- discovery：feed_suffixes / max_feeds / anchor_min_text / max_scroll
- extraction：min_body_chars / min_confidence / max_title_chars
- output：dir / formats

命令行参数与网页表单会覆盖这里的默认值。

## 通用性与可配置性

- **五层列表发现**：站点配置 → RSS/Atom → JSON-LD → 链接聚类 → 翻页，任一层失败自动降级；
- **四层字段抽取**：站点配置选择器 → 结构化数据 → OpenGraph/meta → 通用正文算法；
- **多语言**：多语言日期解析、语言识别、编码识别（GBK/Shift_JIS/EUC-KR/UTF-8...）；
- **多结构**：静态 HTML / JS 渲染（浏览器回退）/ RSS 站点 / 懒加载列表（滚动）；
- **可配置**：全局 settings.json + 站点 sites/*.json + CLI/网页参数 + Agent 自动生成配置；
- **可离线**：--no-llm 确定性流水线不依赖任何模型，网络故障时仍保留错误信息与部分结果。

## 测试

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

测试使用 tests/fixtures/ 中保存的真实站点 HTML 快照，不依赖外网与 API Key。
另有 tests/live_smoke.py 可手动联网验证（不纳入 unittest 自动发现）。

## 限制与合规

- 默认遵守目标站点 robots.txt 并限速，只抓取公开页面；请遵守目标站点服务条款与版权法，
  仅用于个人研究/摘要，不要批量转载全文；
- 验证码/Cloudflare/付费墙/登录墙站点可能只能抓取到部分内容，失败会记录在 run_meta.json；
- 默认只抓取列表页前几页，数量由 max_articles / max_list_pages 控制；
- 站点改版后选择器可能失效，可删除对应 sites/*.json 回退通用策略，或用 Agent 重新生成。

## 项目结构

```text
news_agent/            核心包
  models.py            数据模型（Article/NewsItem/SiteProfile/CrawlConfig/...）
  urls.py dom.py        URL 与 HTML/选择器工具
  config.py            .env / settings 加载与 CrawlConfig 工厂
  fetch.py             静态抓取 + 浏览器回退 + robots/限速/重试/编码
  discover.py          RSS/JSON-LD/站点配置/链接聚类/翻页 列表发现
  extract.py           文章字段抽取（配置/结构化数据/通用算法）
  normalize.py         多语言日期/语言识别/URL 归一/词数
  profiles.py          站点配置读写校验
  agent.py tools.py    Agent Loop 与 Function Calling 工具
  output.py            JSONL/CSV/run_meta/报告/流程图
  cli.py __main__.py   命令行入口（python -m news_agent）
config/settings.json   全局抓取参数
sites/                 站点配置（*.json）
data/                  运行时输出（gitignored）
tests/                 单元测试与 HTML fixtures
web_app.py             本地网页
run.ps1                一键启动
DESIGN.md              改造设计说明
```


完整的模块改造清单、爬取流程与通用性/可配置性设计见 [DESIGN.md](DESIGN.md)。
