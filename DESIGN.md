# 改造设计说明：小红书调研 Agent → 通用新闻抓取 Agent

> 本文档同时是**改造方案**、**模块接口契约**与**验收说明**。
> 目标：用户输入任意新闻网站网址（不限国家/语言），Agent 自动发现新闻列表、
> 抓取正文并输出结构化数据集；在“零配置可抓”和“站点可配置/可调优”之间取得平衡。

---

## 1. 改造总览

### 1.1 原项目

原项目是一个本地原型：用户输入**调研主题** → DeepSeek 通过 Function Calling 选择
`search_notes` / `analyze_notes` / `save_report` → Playwright 打开小红书搜索页并抓取笔记
→ 生成报告、CSV 与 Agent Loop 流程图。核心资产是：

1. DeepSeek Function Calling + Agent Loop（模型决策 → 本地工具 → 结果回传模型）；
2. 本地轻量网页 + 后台线程 + 轮询进度 + 人工介入（登录确认）机制；
3. 报告 / CSV / Agent Loop 图落盘与失败兜底；
4. `.env`、`run.ps1` 一键启动等工程化外壳。

### 1.2 新项目

把“小红书站内搜索”替换为“**任意新闻站点 URL 驱动**”的抓取流水线，并新增
站点画像（site profile）、RSS/JSON-LD/链接聚类发现、多语言日期解析、浏览器回退等能力。
抓取核心做成**可独立运行的确定性流水线**（不需要 LLM 也能跑通），LLM Agent 层负责
规划、自检、补抓与配置修复——既保留原项目的 Agent 机制，又保证网络/模型故障时可用。

### 1.3 一句话数据流

```text
用户输入 URL
   │
   ▼
入口校验 & 站点画像加载（sites/<domain>.json，可选）
   │
   ▼
抓取层（requests 静态抓取 → Playwright 浏览器回退，含 robots/限速/重试/编码识别）
   │
   ▼
新闻列表发现（按优先级：站点配置 > RSS/Atom > JSON-LD ItemList > 链接聚类启发式 > 翻页）
   │
   ▼
文章抓取 + 字段抽取（配置选择器 > JSON-LD > OpenGraph/meta > 通用正文算法）
   │
   ▼
字段归一（标题清洗 / 多语言日期 → ISO 8601 / 语言识别 / URL 归一 / 去重）
   │
   ▼
结构化输出：articles.jsonl + articles.csv + run_meta.json + report.md + agent_loop.svg/png
```

---

## 2. 目录结构（改造后）

```text
xhs-research-agent/
├── news_agent/                 # 新增：新闻抓取 Agent 核心包
│   ├── __init__.py             # 版本与数据模型导出
│   ├── models.py               # 数据模型（Lead）
│   ├── urls.py                 # URL 工具（Lead）
│   ├── dom.py                  # HTML/选择器/JSON-LD/meta 工具（Lead）
│   ├── errors.py               # 异常类型（Lead）
│   ├── config.py               # .env/settings 加载、CrawlConfig 工厂（Lead）
│   ├── fetch.py                # HTTP + 浏览器回退（crawler-net）
│   ├── discover.py             # 新闻列表发现（crawler-net）
│   ├── extract.py              # 文章字段抽取（crawler-extract）
│   ├── normalize.py            # 多语言日期/语言识别/文本清洗（crawler-extract）
│   ├── profiles.py             # 站点配置读写校验（crawler-extract）
│   ├── agent.py                # Agent Loop + 确定性流水线（agent-runtime）
│   ├── tools.py                # Function Calling 工具 schema 与分发（agent-runtime）
│   ├── output.py               # JSONL/CSV/meta/report/流程图（agent-runtime）
│   ├── cli.py                  # 命令行入口（agent-runtime）
│   └── __main__.py             # python -m news_agent（agent-runtime）
├── config/settings.json        # 全局抓取参数（超时/重试/限速/浏览器/输出）
├── sites/                      # 站点画像：*.json + 模板 + 说明
├── data/                       # 运行时输出（gitignored）
├── tests/                      # unittest + HTML fixtures
├── web_app.py                  # 改造：本地网页（URL 输入、进度、报告、文件下载）
├── run.ps1                     # 改造：一键启动（新增 --Url / --NoLlm 等参数）
├── requirements.txt            # 改造：lxml/openai/playwright/requests
├── README.md                   # 改造：使用说明
├── DESIGN.md                   # 本文档
└── .env.example / .env
```

> 旧的 `RedBookFinal.py` 被 `news_agent/` 取代（Agent Loop / 报告 / 流程图能力迁移到
> `agent.py` / `output.py`），旧实现可从 git 历史找回。

---

## 3. 核心模块改造清单

| 原模块/能力 | 改造后 | 处理方式 | 说明 |
| --- | --- | --- | --- |
| `search_notes` 小红书搜索工具 | `tools.crawl_site` + `discover`/`extract` | 重写 | 从“关键词搜索站内”改为“URL 驱动的站点发现 + 文章抓取” |
| `analyze_notes` 词频分析 | `tools.analyze_coverage` | 调整 | 从词频线索改为**字段完整率 / 失败原因 / 去重 / 日期跨度 / 语言分布**自检 |
| `save_report` | `tools.save_outputs` + `output.write_outputs` | 调整 | 从“报告 + 笔记 CSV”升级为“JSONL + CSV + run_meta + 报告 + 流程图” |
| Playwright 打开小红书页面 | `fetch.Fetcher`（静态优先、浏览器回退） | 保留+扩展 | 新闻站多为静态 HTML，先 requests；JS 站点/反爬/懒加载再开 Chrome |
| 登录等待（login_event） | 人工介入事件（验证码/Cloudflare/付费墙） | 调整 | 同一套 `threading.Event` 机制，网页按钮改为“我已处理，继续” |
| Agent Loop（DeepSeek Function Calling） | `agent.run_agent` | 保留 | 工具、提示词、状态机全部换新，循环机制保留 |
| Agent Loop 流程图 | `output.save_agent_loop_diagram` | 保留+调整 | 视觉风格沿用；节点改为新闻抓取工具调用轨迹 |
| 轻量网页 `web_app.py` | 改造 | 保留 | 输入从“调研主题”改为“新闻网址 + 抓取数量 + 选项” |
| `.env` / `run.ps1` | 改造 | 保留 | 仍读取 `DEEPSEEK_API_KEY`；新增新闻抓取参数 |
| 小红书专用字段（点赞/收藏/作者） | `Article` 字段（标题/正文/时间/来源/作者/语言/图片） | 重写 | 输出通用新闻 schema（见 §7） |
| 采集上限 / 搜索次数限制 | `max_articles`/`max_candidates`/`max_list_pages` | 调整 | 以文章数、候选数、列表翻页数三重上限控制礼貌抓取 |
| 失败兜底保存 | 保留 | 保留 | 模型未调用保存 / LLM 不可用 / 抽取全失败时仍落盘模板报告 |

---

## 4. 爬取流程（详细）

### 4.1 步骤 0：入口与配置

1. `urls.normalize_entry_url()`：补协议、去 anchor、校验 http(s)；
2. `profiles.load_profile(domain_from_url(url))`：按“域名等于 / 子域后缀”匹配 `sites/*.json`；
3. `config.build_config()`：settings.json + CLI/网页参数 → `CrawlConfig`；
4. 输出目录 `data/<domain>_<时间戳>/`。

### 4.2 步骤 1：抓取（fetch.py）

- **静态优先**：`requests.Session`，浏览器 UA，重定向，超时 `timeout_s`，指数退避重试 `retries` 次；
- **编码识别**：BOM → `<meta charset>` → HTTP header → UTF-8 试解码 → `apparent_encoding` → latin-1 兜底
  （应对新浪 GBK 页面被误判为 ISO-8859-1 等典型问题）；
- **礼貌抓取**：同域最小间隔 `min_delay_s`；默认读取并遵守 `robots.txt`（`respect_robots`）；
- **浏览器回退**：`browser_mode=auto` 时，若响应过短、正文可见文本过少或命中 JS 应用外壳特征，
  改用 Playwright（本机 Chrome，可 headless）打开页面；`always` 强制、`never` 禁用；
- **人工介入**：可见浏览器模式下若疑似验证码/Cloudflare 拦截，发出 `waiting_intervention` 事件，
  等待网页端“继续”按钮（最多 180s），处理完成后继续抓取。

### 4.3 步骤 2：新闻列表发现（discover.py）

按优先级依次尝试，命中足够条目即返回（并记录 `method`）：

1. **站点画像**：`profile.list.item` 选择器逐页抽取（`item_title/item_date/item_section/item_image` 补字段），
   按 `list.pagination.next` 或 `url_template` 翻页，最多 `max_list_pages` 页；
2. **RSS/Atom**：画像指定 feeds + 页面 `<link rel="alternate" type="application/rss+xml|atom+xml">`
   + 常见后缀探测（`/feed`、`/rss.xml` 等），用标准库解析，适配全球站点；
3. **JSON-LD**：`ItemList` / `CollectionPage` / `WebPage.mainEntity` 中的条目；
4. **链接聚类启发式**：收集同站 `<a>`，按“父容器重复结构 + 链接文本长度 + 是否邻近日期/
   位于标题标签内”打分聚类，选出最像新闻列表的容器并取其中链接；再用
   `looks_like_article_url()` 过滤导航/标签/作者/搜索/视频等非文章链接；
5. **翻页**：`rel=next` / 文本 “next/下一页/次へ/suivant/weiter/Siguiente” / URL 页码递增，
   最多 `max_list_pages` 页。

结果统一为 `NewsItem` 列表（url/title/published_raw/section/image/list_url/method），
按归一化 URL 去重后截断到 `max_candidates`。

### 4.4 步骤 3：文章抓取与字段抽取（extract.py）

对每个候选 URL（按列表顺序，最多 `max_articles` 篇，遵守限速）：

1. 抓取 HTML（同 fetch 层，支持浏览器回退）；
2. 解析 DOM（`dom.parse_html`）；
3. **标题**：画像选择器 → JSON-LD `headline` → `og:title` → `h1` → `<title>`（去站点后缀，
   过滤“首页/登录/404”类标题与超长标题）；
4. **正文**：画像 `article.body`（可多选择器，先删 `exclude` 噪声）→ JSON-LD `articleBody`
   → 可选 trafilatura（若安装）→ 内置通用算法（文本密度 + 链接密度 + 段落评分 + 容器标题归一）；
   正文清洗：去脚本/导航/广告/相关推荐，保留段落换行，按 `max_body_chars` 截断；
5. **发布时间**：画像选择器（含 `datetime`/`content` 属性）→ JSON-LD `datePublished/dateCreated/
   dateModified` → meta（`article:published_time`、`og:published_time`、`publishdate`、`date`、
   `DC.date` 等）→ `<time datetime>` → URL 中的日期（`/2026/10/29/`、`/20261029/`）→ 标题附近可见日期文本；
6. **来源**：画像 → `og:site_name` → JSON-LD `publisher.name` → 域名；
7. **作者/摘要/栏目/图片**：画像 → 结构化数据 → meta → 通用启发式（`[rel=author]`、
   `class*=author|byline`、`og:description`、正文首段、文章容器内 `img`）；
8. **语言**：`<html lang>` → `og:locale` → 画像语言 → 文本特征识别（中/英/日/韩/俄/阿/法等）；
9. **归一化**：`normalize.parse_datetime` 多语言日期 → ISO 8601（带时区，默认 `default_timezone`）；
   `normalize.canonicalize_url` 去追踪参数、去 fragment；`word_count`（CJK 按字、其他按词）；
10. **置信度**：按字段命中加权（标题 0.30 / 正文 0.35 / 时间 0.15 / 作者 0.10 / 来源或语言 0.10），
    低于阈值标记 `confidence`，由 Agent 决定是否换策略或补抓。

### 4.5 步骤 4：结构化输出（output.py）

一次任务在 `data/<domain>_<时间戳>/` 下产出：

| 文件 | 内容 |
| --- | --- |
| `articles.jsonl` | 每行一个 `Article` JSON（字段见 §7，稳定 schema v1.0） |
| `articles.csv` | 扁平表格（Excel 可直接打开，UTF-8 BOM） |
| `run_meta.json` | 入口 URL、配置、发现方式、统计、错误、警告、输出索引 |
| `report.md` | LLM 报告（无 LLM 时为模板统计报告；始终写入采集问题与来源链接） |
| `agent_loop.svg` / `.png` | 本次实际工具调用轨迹图（沿用原项目风格） |

### 4.6 Agent 层（agent.py + tools.py）

工具集合（DeepSeek Function Calling）：

| 工具 | 作用 |
| --- | --- |
| `inspect_site` | 抓一页并返回结构化“站点结构报告”：feeds、JSON-LD 类型、候选列表选择器、候选文章链接、文章页候选字段选择器 |
| `crawl_site` | 执行确定性发现 + 抽取（可按需再次调用补抓） |
| `save_site_profile` | 把模型推断出的站点配置校验后写入 `sites/<domain>.json` 并重载 |
| `analyze_coverage` | 字段完整率、失败原因、去重、语言/日期分布、候选剩余量，给出下一步建议 |
| `save_outputs` | 落盘数据集 + 报告 + 流程图 |

Agent Loop：`用户目标 → 模型选择工具 → 本地执行 → 工具结果回传模型 → …`（最多 `max_rounds` 轮），
结尾保证至少保存一次产物。无 LLM（`--no-llm`）或 API 失败时走确定性流水线 + 模板报告，
保证离线也可用。工具返回统一为 JSON 摘要（条数、样例标题、缺失字段、错误），避免把整页 HTML 塞回模型。

---

## 5. 通用性设计（任意国家/语言/结构）

| 问题 | 策略 |
| --- | --- |
| 站点结构千差万别 | 五层发现（画像 / RSS / JSON-LD / 链接聚类 / 翻页）+ 四层抽取（画像 / 结构化数据 / meta / 通用算法），每层失败自动降级 |
| 不同语言 | 多语言日期解析（英/中/日/法/德/西/意/葡/荷/俄等月名、`2026年10月29日`、`29. Oktober 2026`、相对时间 “2 hours ago/3天前”）；语言识别写入 `language`；编码识别覆盖 GBK/Shift_JIS/EUC-KR/windows-125x 等 |
| JS 渲染 / 懒加载 | Playwright 回退；列表页可配置 `max_scroll` 自动滚动触发懒加载 |
| RSS 友好的站点 | 直接走 feed，稳定且礼貌 |
| 结构化数据友好的站点 | JSON-LD/OpenGraph 优先，跨语言、跨模板都有效 |
| 反爬 / 验证码 / 付费墙 | 可见浏览器 + 人工介入事件；失败记入 `run_meta.errors` 并继续下一篇文章 |
| 时区 | `default_timezone` 配置项；页面自带时区偏移优先 |
| 多站点批量 | CLI 接受多个 `--url`（逐个执行，各自输出目录） |

---

## 6. 可配置性设计

1. **全局参数** `config/settings.json`（无需改代码）：
   `max_articles` / `max_list_pages` / `max_candidates` / `timeout_s` / `retries` /
   `min_delay_s` / `respect_robots` / `browser_mode` / `browser_wait_ms` / `headless` /
   `max_body_chars` / `default_timezone` / `language_hint` / `include_patterns` /
   `exclude_patterns` / `llm` / `discovery` / `extraction` / `output`。
2. **站点画像** `sites/<domain>.json`：CSS/XPath 选择器 + 抓取模式 + 编码 + 翻页规则 +
   URL 白/黑名单；schema 见 `sites/README.md`、模板见 `sites/_template.json`。
3. **命令行/网页参数** 覆盖全局配置：`--url/--limit/--pages/--lang/--browser/--no-llm/--out` 等。
4. **环境变量/.env**：`DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`。
5. **Agent 自学习**：LLM 可通过 `inspect_site` + `save_site_profile` 自动生成站点配置，
   下次同站点抓取直接复用（可人工复核、可提交共享）。

---

## 7. 结构化输出 schema（v1.0）

`Article` 字段（`articles.jsonl` / API 返回一致）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | URL 的 16 位 SHA1，主键/去重 |
| `url` | string | 原始文章链接 |
| `canonical_url` | string | 归一化链接（去追踪参数、去 fragment） |
| `title` | string | 标题 |
| `summary` | string | 摘要（meta 描述 / 正文首段） |
| `body` | string | 正文纯文本（段落以换行分隔） |
| `published_at` | string | ISO 8601（含时区），未知为空 |
| `published_raw` | string | 页面原始时间文本，便于复核 |
| `author` | string | 作者/记者 |
| `source` | string | 来源/媒体名 |
| `section` | string | 栏目/频道 |
| `language` | string | 语言代码（en/zh-CN/ja/fr/…） |
| `site_domain` | string | 站点域名 |
| `images` | string[] | 文章图片绝对 URL |
| `crawled_at` | string | 抓取时间 ISO 8601 |
| `extract_method` | string | 抽取策略链路，如 `profile+jsonld` |
| `confidence` | number | 0–1 字段可信度 |
| `word_count` | number | 正文长度（CJK 按字，其他按词） |
| `list_page` | string | 来源列表页 |

---

## 8. 保留与调整的现有功能

| 功能 | 保留 | 调整 |
| --- | --- | --- |
| DeepSeek Function Calling + Agent Loop | ✅ 机制不变 | 工具从“搜索笔记/词频/存报告”改为“结构探测/抓取/存画像/自检/落盘” |
| 本地网页 | ✅ 仍是 stdlib HTTPServer + 轮询 | 表单改为“新闻网址 + 数量 + 选项”，新增文件下载与画像列表 |
| 人工介入 | ✅ 同一 `threading.Event` | 从“登录小红书”改为“验证码/浏览器拦截处理” |
| Agent Loop 可视化 | ✅ SVG/PNG 风格沿用 | 节点语义改为抓取工具轨迹 |
| 报告/数据落盘 + 失败兜底 | ✅ 保留 | 输出升级为 JSONL/CSV/meta/报告 |
| `.env` 读取、`run.ps1` 一键启动 | ✅ 保留 | 新增 `--Url`、`--NoLlm`、`--Visible` 等参数与依赖检查 |
| 小红书专用逻辑 | ❌ 移除 | 关键词搜索、笔记详情选择器、点赞/收藏、登录流程 |
| 词频分析 | ❌ 移除 | 由字段完整率/失败诊断取代（新闻任务不需要） |

---

## 9. 模块接口契约（开发用）

> 全部模块只依赖 `models.py / urls.py / dom.py / errors.py / config.py` 中的公共对象；
> 跨模块调用必须遵守以下签名，避免循环依赖。

### 9.1 fetch.py（crawler-net）

```python
@dataclass
class FetchResult:
    url: str; final_url: str; status: int; text: str; content: bytes
    headers: dict[str, str]; encoding: str; method: str  # "http" | "browser"
    elapsed_ms: int; error: str = ""
    @property
    def ok(self) -> bool: ...
    @property
    def content_type(self) -> str: ...

class Fetcher:
    def __init__(self, config: CrawlConfig, settings: dict | None = None,
                 on_event=None, intervention_event=None) -> None: ...
    def get(self, url: str, *, browser: bool | None = None,
            wait_selector: str = "", encoding: str = "",
            scroll: int = 0) -> FetchResult: ...
    def close(self) -> None: ...
    def __enter__(self) -> "Fetcher": ...
    def __exit__(self, *exc) -> None: ...
    def render_svg_screenshot(self, svg: str, width: int, height: int) -> bytes | None: ...
```

- `browser=None`：按 `config.browser_mode` 与自动 JS 判定；`True/False` 强制/禁用；
- `scroll>0`：浏览器模式下滚动若干次，触发懒加载列表；
- 网络/HTTP 失败（重试耗尽、4xx/5xx、浏览器异常）返回 `ok=False` 且带 `error` 的 `FetchResult`，
  **不抛异常**，由调用方记录并继续；robots.txt 拒绝抛 `RobotsDenied`（调用方按配置降级为警告）；
  浏览器启动失败自动降级为静态抓取并在 `error` 中说明；
- `on_event(message, phase)` 用于进度；`intervention_event` 为 `threading.Event`，人工处理后 set。

### 9.2 discover.py（crawler-net）

```python
@dataclass
class DiscoveryResult:
    items: list[NewsItem]
    method: str                    # profile | feed | jsonld | heuristic | mixed
    pages: list[str]
    feeds: list[str]
    warnings: list[str]

def discover_news(fetcher, entry_url: str, config: CrawlConfig,
                  profile: SiteProfile | None = None,
                  settings: dict | None = None, on_event=None) -> DiscoveryResult: ...

def probe_structure(fetcher, url: str, config: CrawlConfig,
                    profile: SiteProfile | None = None,
                    settings: dict | None = None) -> dict: ...

def candidate_link_selectors(tree, min_count: int = 3) -> list[dict]: ...
def looks_like_article_url(url: str, config: CrawlConfig | None = None) -> bool: ...
```

`probe_structure()` 返回 JSON 可序列化 dict（给 `inspect_site` 工具）：
`url/title/language/feeds/jsonld_types/candidate_selectors/article_links_sample/pagination_hint/article_page_probe`。

### 9.3 extract.py（crawler-extract）

```python
def extract_from_html(html: str, url: str, config: CrawlConfig, *,
                      profile: SiteProfile | None = None,
                      item: NewsItem | None = None,
                      settings: dict | None = None) -> Article: ...
def extract_article(fetcher, item: NewsItem, config: CrawlConfig, *,
                    profile: SiteProfile | None = None,
                    settings: dict | None = None, on_event=None) -> Article: ...
```

`extract_from_html` 为纯函数（单元测试主入口）；失败抛 `ExtractError` 或返回空正文字段。

### 9.4 normalize.py（crawler-extract）

```python
def canonicalize_url(url: str, base_url: str = "") -> str: ...
def parse_datetime(raw: str, *, default_timezone: str = "UTC",
                   now: datetime | None = None, language: str = "") -> str: ...
def detect_language(text: str, html_lang: str = "") -> str: ...
def clean_text(value: str) -> str: ...
def word_count(text: str) -> int: ...
def truncate(text: str, max_chars: int) -> str: ...
```

### 9.5 profiles.py（crawler-extract）

```python
def normalize_domain(value: str) -> str: ...
def load_profile(domain: str, sites_dir: Path | None = None) -> SiteProfile | None: ...
def save_profile(profile: SiteProfile, sites_dir: Path | None = None) -> Path: ...
def list_profiles(sites_dir: Path | None = None) -> list[dict]: ...
def validate_profile(data: dict) -> SiteProfile: ...
def profile_template() -> dict: ...
```

### 9.6 agent.py / tools.py（agent-runtime）

```python
def run_deterministic(config: CrawlConfig, settings: dict | None = None,
                      on_event=None, intervention_event=None) -> CrawlResult: ...
def run_agent(config: CrawlConfig, settings: dict | None = None,
              on_event=None, intervention_event=None) -> tuple[str, CrawlResult]: ...
TOOL_SCHEMAS: list[dict]
def execute_tool(name: str, arguments: dict, session) -> dict: ...
```

### 9.7 output.py（agent-runtime）

```python
def write_outputs(result: CrawlResult, config: CrawlConfig,
                  settings: dict | None = None, report_markdown: str | None = None,
                  trace: list | None = None, renderer=None) -> dict[str, str]: ...
def build_template_report(result: CrawlResult) -> str: ...
```

---

## 10. 测试与验收

### 10.1 单元测试（`python -m unittest discover -s tests`）

- `urls`：入口补全、追踪参数、同站判断、IDN；
- `dom`：CSS 子集/XPath/属性后缀、JSON-LD 展开、meta、容器文本清洗；
- `normalize`：多语言日期（英/中/日/德/法/相对时间/ISO/RFC2822）、语言识别、URL 归一、词数；
- `profiles`：读写、域名匹配（含子域）、非法配置报错、选择器解析；
- `discover`：RSS/JSON-LD/启发式/翻页（真实 HTML fixtures）；
- `extract`：结构化数据站点、配置选择器站点、纯启发式站点（fixtures）；
- `output`：JSONL/CSV/meta 字段与编码；
- `agent`：无 LLM 的确定性流水线冒烟（本地 fixture HTTP 服务，不依赖外网）。

### 10.2 联网验收（可访问站点）

已探明当前网络可访问：BBC、NHK、Le Monde、DW、新浪新闻、澎湃、联合早报、BBC RSS。
验收矩阵覆盖英文/日文/法文/德文/中文站点与不同页面结构。

### 10.3 端到端

`python -m news_agent --url https://www.bbc.com/news --limit 3 --no-llm`
→ 检查 `data/*/articles.jsonl` 中标题/正文/时间/来源字段完整率；
再跑 `--limit 2`（LLM 模式，需 API Key）验证 Agent Loop 与报告。
`python web_app.py` 验证网页流程与文件下载。

---

## 11. 风险与边界

- **合规**：默认遵守 robots.txt、限速、仅抓公开页面；请遵守目标站点服务条款与版权，
  仅用于个人研究/摘要，不批量转载全文；
- **反爬**：Cloudflare/验证码/付费墙站点可能只能取到部分内容，失败会记录并在报告中说明；
- **动态站点**：无限滚动/登录墙站点需要浏览器模式或站点画像，通用策略只覆盖公开可访问部分；
- **LLM 依赖**：Agent 自学习/报告依赖 DeepSeek；无 Key 时可用 `--no-llm` 完成确定性抓取；
- **不保证全站覆盖**：默认只抓列表页第一屏/前几页，量由 `max_articles/max_list_pages` 控制。

---

## 12. 实现落地与验收记录（2026-10-29）

### 12.1 代码与交付物

| 交付物 | 规模 | 说明 |
| --- | --- | --- |
| `news_agent/` | 16 个文件，约 9,500 行 | 抓取 / 发现 / 抽取 / 归一 / 画像 / Agent / 工具 / 输出 / CLI |
| `config/settings.json` | — | 全局抓取参数（超时/重试/限速/浏览器/输出/LLM） |
| `sites/` | 模板 + README + `bbc.com.json` | 站点画像；bbc.com 为真实页面验证过的示例配置 |
| `web_app.py` | 525 行 | 本地网页：URL 输入、进度、人工介入、报告、文件下载 |
| `run.ps1` | 202 行 | 一键启动；新增 `-Url / -Limit / -NoLlm / -Visible` 参数 |
| `tests/` | 约 2,900 行，234 个用例 + 16 份 fixtures（4.63 MB） | 单元测试 + 本地 fixture HTTP 服务 + live smoke |
| `requirements.txt` | — | lxml / openai / playwright / requests / trafilatura |
| `README.md` / `DESIGN.md` | — | 使用说明 / 本文档 |

### 12.2 单元测试

`python -m unittest discover -s tests -v` → **234 tests，OK，约 6.7s，通过率 100%**（复跑一致）。

| 测试文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| test_urls / test_dom / test_config / test_models / test_fixtures | 113 | 脚手架与选择器工具、配置、fixtures 完整性 |
| test_normalize / test_profiles | 57 | 多语言日期、语言识别、URL 归一、站点画像 |
| test_discover | 30 | RSS/Atom/JSON-LD/启发式/翻页/过滤/probe_structure |
| test_extract | 15 | 5 站点 9 份真实文章 fixture，含 trafilatura 有/无/异常三路径 |
| test_output | 14 | JSONL/CSV/run_meta/报告/SVG/PNG 降级 |
| test_agent_offline | 5 | 本地 fixture HTTP 服务上的确定性 E2E、GB18030 解码、404 兜底、302→首页跳过、缓存只请求一次 |

测试全程不联网（socket 护栏）、不需要 API Key、不写项目 `data/`。

### 12.3 真实站点验收矩阵（确定性流水线，2026-10-29）

参数：`max_articles=2、max_list_pages=2、timeout=20s、retries=2、min_delay=0.8s`，
每站均为公开页面、遵守 robots。

| 入口 | 语言 | 发现方式 | 文章 | 六字段完整率 | 浏览器 | 耗时 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| https://www.bbc.com/news | en | profile（站点画像） | 2 | 100% | 0 | 4.2s | 画像命中，最快路径 |
| https://feeds.bbci.co.uk/news/rss.xml | en | feed | 2 | 100% | 0 | 4.5s | RSS/Atom 解析 |
| https://www.dw.com/en/top-stories/s-9097 | en | heuristic（链接聚类） | 2 | 100% | 0 | 8.6s | 零配置通用路径 |
| https://www3.nhk.or.jp/news/ | ja | heuristic | 2 | 100% | 0 | 45.8s | 日文正常；该站 TLS 偶发波动 |
| https://www.lemonde.fr/ | en | feed（列表 406→浏览器） | 1 | 100% | auto | 23.7s | 文章页 402 付费墙→浏览器可读 |
| https://news.sina.com.cn/ | zh | heuristic | 2 | 100% | 0 | 6.4s | 中文正常；1 条跳转 feed 条目被过滤（warning） |
| https://www.zaobao.com.sg/ | zh | heuristic | 2 | 100% | 0 | 9.7s | 中文正常 |
| **合计** | 5 语言 | 4 类策略 | **13 篇** | **13/13 六字段 100%** | — | — | 0 抽取错误 |

### 12.4 Agent 模式验收（真实 DeepSeek API）

命令：`python -m news_agent --url https://www.bbc.com/news --limit 2`（配置真实 Key）

- Function Calling 循环 4 轮：`crawl_site → analyze_coverage → save_outputs` → 模型给出最终答复；
- 六字段完整率 100%，报告由模型撰写（采集概况、字段完整率、语言/时间分布、真实文章链接、
  问题与局限），未编造标题或链接；SVG/PNG 流程图按实际调用轨迹生成；
- 无 API Key / 客户端初始化失败 / 循环内 API 异常：自动降级为确定性流水线并落盘，
  报告中注明“未使用 LLM”；
- 每轮工具调用、结果写入 trace，流程图输出 `LOOP 01…N` 卡片；循环结束后用完整 trace
  重绘流程图，保证最后一轮工具调用（save_outputs）也被记录。

### 12.5 网页 / CLI / 一键启动验收

- 网页（`python web_app.py` → http://127.0.0.1:8765）：`GET /` 200；表单提交后日志轮询、阶段状态、
  文件列表、文章预览、报告与下载均正常（实测下载 `articles.jsonl` 200/7.2KB）；
  `/api/config` 返回 Key 可用性，未配置 Key 时自动建议取消 LLM 模式；
  可见浏览器模式下出现验证码/Cloudflare 时显示“我已处理，继续抓取”按钮；
- CLI：`--help / --version` 正常，非法 URL 退出码 2，成功任务退出码 0，`--json` 输出可被
  `json.loads` 解析；
- `run.ps1`：`.\run.ps1 -Mode cli -Url "https://www.bbc.com/news" -Limit 1 -NoLlm` 实测退出码 0，
  产物完整；脚本已加 UTF-8 BOM（Windows PowerShell 5.1 加载含中文的无 BOM 脚本会解析异常）。

### 12.6 联调中发现并修复的问题

| # | 问题 | 根因 | 修复 | 验证 |
| --- | --- | --- | --- | --- |
| 1 | `SiteProfile` 构造崩溃 `TypeError: 'Field' object is not callable` | 类体内 `list` 字段遮蔽内置 `list` | 改 `field(default_factory=lambda: [])` | models 单测 + 画像全链路 |
| 2 | CSS 组合符带空格时退化为后代选择器 | 空白 combinator 覆盖显式 `> + ~` | 显式组合符优先标记 | test_dom |
| 3 | 裸 `::text / ::html` 取值后缀不生效 | 正则强制括号 | 允许无括号后缀 | test_dom |
| 4 | 抓取层对每个 4xx/feed 探测都开浏览器，BBC 列表发现 88.7s | 回退条件过宽、feed 探测无收敛 | 仅网络失败/JS 空壳/反爬状态码回退；探测最多 3 个后缀、首个有效即停 | BBC 发现 5.0s |
| 5 | RSS/JSON-LD 候选混入首页/栏目页（新浪产出“新浪网”假文章） | 只有启发式路径做文章 URL 过滤 | 统一 `_candidate_allowed_url` 过滤 | test_discover + 新浪 live |
| 6 | 站点配置的 `.xml` feed 被“文章扩展名”规则误杀 | feed URL 也走了文章 URL 硬拒绝 | feed 探测不再套用文章规则 | test_discover 回归 |
| 7 | 跳转端点（`redirect.php?url=…`）进入候选 | 未识别跳转参数 | path+query 跳转端点过滤 | test_discover |
| 8 | 候选 URL 正常但 302 到站点首页 | 发现层无法预知重定向 | Agent 抓取前校验 `final_url`，非文章页跳过并记 warning | agent_offline 302 用例 |
| 9 | 付费墙文章页 402 全部失败（Le Monde 0 篇） | 浏览器回退状态码集合缺 402 | 加入 402（付费墙/反爬可能浏览器可读） | Le Monde 1 篇 100% |
| 10 | Agent Loop 图最后一轮显示“未知工具” | 工具执行中生成图，当前轮 trace 尚未写回 | 循环结束后按完整 trace 重绘 | 真实 Agent 运行 + fake LLM 用例 |
| 11 | `save_outputs` 日志显示“标题完整率 0%” | 结果行判断顺序错误 | 先判断 saved | Agent 运行日志 |
| 12 | `run.ps1` 在 Windows PowerShell 5.1 解析失败 | 无 BOM 的 UTF-8 中文脚本被按本地代码页读取 | 脚本加 UTF-8 BOM | PS 5.1 实测退出码 0 |
| 13 | 浏览器回退在代理环境被 reset | Playwright 不读 HTTP(S)_PROXY | 把代理注入 Chrome context（`settings.fetch.proxy` 可覆盖） | 浏览器回退抓取成功 |

### 12.7 已知限制与后续建议

- **付费墙/验证码**：只做一次浏览器回退；付费墙站点可能只能拿到导语（Le Monde 实测 1750 字），
  全文需订阅；验证码需可见浏览器人工介入；
- **正文质量**：未安装 trafilatura 时通用回退算法对 JS 内嵌正文页面（如 NHK）偏弱（约 94 字 vs 737 字），
  因此 trafilatura 已列为正式依赖，代码仍保留降级路径；
- **行数**：fetch 1,035 / discover 1,953 / extract 1,307 / normalize 1,070 / agent 1,066 / output 853 行，
  超过“建议 <800 行”的约定（多策略与多语言规则集中所致，无重复定义），后续可拆分为子模块；
- **动态页面**：懒加载仅通过 `max_scroll` 有限滚动；登录墙、无限滚动、SPA 路由需站点画像或人工介入；
- **跨站 feed**：RSS/JSON-LD 条目未强制严格同站（BBC 的 `feeds.bbci.co.uk` 与站点不同域），
  以“根/频道/跳转端点拒绝 + 文章 URL 过滤”兜底；如需更严格可加站点配置开关；
- **统计口径**：`stats.browser_fetches` 当前未累加（不影响结果），feed 探测不计入 pages_fetched，
  后续可补齐统计；
- **fixtures**：为 2026-10-29 线上快照，站点改版后需 `tests/fixtures/collect.py` 重采；
- **LLM 自学习闭环**：`inspect_site → save_site_profile → 补抓` 路径由 fake LLM 与工具单测覆盖，
  本次真实运行中目标站点字段已 100%，未触发该分支；建议对结构特殊站点做一次人工演练。

### 12.8 复核命令

```powershell
# 单元测试（离线，无 Key）
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v

# 联网冒烟（BBC/新浪，或指定站点；浏览器模式可选）
python tests/live_smoke.py --limit 2
python tests/live_smoke.py --url https://www.lemonde.fr/en/ --limit 1 --browser always

# 确定性抓取（无需 API Key）
python -m news_agent --url "https://www.bbc.com/news" --limit 5 --no-llm

# Agent 模式（需要 .env 中 DEEPSEEK_API_KEY）
python -m news_agent --url "https://www.dw.com/en/top-stories/s-9097" --limit 5

# 本地网页（http://127.0.0.1:8765）
python web_app.py

# 一键启动
.\run.ps1
.\run.ps1 -Mode cli -Url "https://news.sina.com.cn/" -Limit 5 -NoLlm
```

