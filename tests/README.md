# tests/ — 单元测试与离线 fixtures

本目录是新闻抓取 Agent 的**离线单元测试**与**真实站点 HTML 快照**。
测试全程不联网（离线 E2E 用 127.0.0.1 上的 fixture HTTP 服务）、不需要 API Key，
可在 CI/无网络环境重复运行。

## 运行方式

项目根目录下（Windows PowerShell）：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

只跑某个模块：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_extract.py" -v
```

联网冒烟（可选、手动执行，不属于 unittest 自动发现）：

```powershell
# 默认 BBC + 新浪，各 2 篇
.venv\Scripts\python.exe tests\live_smoke.py --limit 2

# Le Monde 列表页 406 需浏览器回退（实测 title/body/date/author/source/language 100%）
.venv\Scripts\python.exe tests\live_smoke.py --url https://www.lemonde.fr/en/ --limit 1 --browser always
```

> 注意：必须从**项目根目录**运行，测试通过 `news_agent.*` 与 `tests.fixtures.loader` 导入。

## 目录结构

```text
tests/
├── __init__.py
├── test_urls.py          # URL 工具：入口补全/校验、域名归一、追踪参数、同站、正则
├── test_dom.py           # HTML/选择器：CSS 子集、XPath、取值后缀、JSON-LD、meta、容器文本
├── test_config.py        # settings/.env 加载、环境变量优先级、CrawlConfig 工厂
├── test_models.py        # Article/SiteProfile/CrawlConfig/CrawlStats/CrawlResult/to_dict 契约
├── test_normalize.py     # 多语言日期、语言识别、canonical URL、文本清洗、CJK 词数
├── test_profiles.py      # 站点画像读写校验、域名/子域匹配、非法配置、模板
├── test_discover.py      # RSS/Atom/JSON-LD/启发式/画像/翻页/feed 自动发现（离线）
├── test_extract.py       # 真实 fixtures 抽取 + 配置选择器 + 通用算法 + trafilatura 回退
├── test_output.py        # JSONL/CSV/run_meta/report/SVG 流程图字段与编码
├── test_agent_offline.py # 本地 fixture HTTP 服务的确定性流水线 E2E（不联网）
├── test_fixtures.py      # fixtures manifest 完整性（sha256/字节数/站点覆盖）与 loader
├── fixture_server.py     # 离线 fixture HTTP 服务辅助（E2E 用，不参与自动发现）
├── README.md
└── fixtures/
    ├── manifest.json     # 每份快照的 name/path/source_url/encoding_used/bytes/sha256/fetched_at/kind/notes
    ├── loader.py         # load_fixture()/fixture_names()/list_fixtures()/decode_bytes()
    ├── collect.py        # 可选：重新采集 fixtures（联网，不属于单元测试）
    ├── bbc/              # BBC：列表页 + 2 篇文章 + RSS
    ├── dw/               # DW：列表页 + 2 篇文章
    ├── nhk/              # NHK：列表页 + 1 篇文章（日文）
    ├── lemonde/          # Le Monde：英文版列表页（文章页 402 付费墙，仅用列表测发现）
    ├── sina/             # 新浪：列表页 + 2 篇文章 + 1 份合成 GB18030 编码页
    └── zaobao/           # 联合早报：列表页 + 1 篇文章
```

## fixtures 说明

- **保存原始响应字节**（不解码、不改写），`manifest.json` 记录解码编码；
  GBK/GB18030 等编码页面因此不会在保存时丢失信息。
- 当前共 16 份快照、约 4.6 MB（预算上限 8 MB），采集时间 2026-10-29。
- `sina/sina_article_gbk.html` 是**合成 fixture**：把真实新浪文章按 GB18030
  重新编码并改写 `meta charset`（线上新浪现为 UTF-8），仅用于编码识别/解码回归；
  manifest 的 `notes` 已注明。
- Le Monde 文章页返回 HTTP 402（付费墙），只保留列表页供 `discover` 测试；
  文章抽取用 BBC/DW/NHK/新浪 fixtures 覆盖。
- NHK 线上已跳转至 `news.web.nhk/newsweb`，fixture 保存的是重定向后的最终 HTML。

### 测试里读取 fixtures

```python
from tests.fixtures.loader import load_fixture, fixture_names, fixtures_by_kind

html = load_fixture("bbc_news_list").text      # 解码后的 HTML 文本
raw = load_fixture("sina_article_gbk").bytes   # 原始字节（保留 GB18030 编码）
names = fixture_names(kind="article")          # 某类全部 fixture 名称
```

`load_fixture()` 先按 manifest 的 `encoding_used` 严格解码，失败再依次尝试
UTF-8 / GB18030 / Shift_JIS / EUC-KR / windows-1252 / latin-1。

### 重新采集（可选，需联网）

```powershell
.venv\Scripts\python.exe tests\fixtures\collect.py
```

脚本会删除旧的 `*.html`、重新抓取列表页/文章页并重写 `manifest.json`；
单站点失败自动重试并跳过。采集只抓公开页面，请遵守目标站点服务条款。

## 覆盖范围

| 测试文件 | 被测模块 | 关键点 |
| --- | --- | --- |
| test_urls.py | news_agent.urls | 补协议/去 anchor、非法协议拒绝、IDN、追踪参数、子域同站、正则匹配与坏正则兜底 |
| test_dom.py | news_agent.dom | CSS 子集（属性/组合符/伪类）、XPath 与作用域、`::text`/`::html`/`::attr(name)`、坏选择器返回空、JSON-LD/@graph、meta、container_text 清噪 |
| test_config.py | news_agent.config | 默认值深合并、坏配置回退、.env 解析与系统环境变量优先、DEEPSEEK_* 覆盖、输出目录解析 |
| test_models.py | news_agent.models | to_dict 字段稳定、id 由 canonical URL 生成、SiteProfile 默认合并/域名归一/实例隔离、CrawlConfig 校验与 from_settings、统计上限 |
| test_normalize.py | news_agent.normalize | 表驱动多语言日期（ISO/RFC/英中德法西日俄/相对时间/epoch/纯日期/时区/非法输入）、语言识别、canonical URL、词数、截断 |
| test_profiles.py | news_agent.profiles | validate/save/load、最长域名匹配（含子域）、坏文件告警忽略、模板读取 |
| test_discover.py | news_agent.discover | BBC RSS 解析、JSON-LD ItemList、启发式聚类（BBC 列表 fixture）、画像选择器、翻页、feed 自动发现、feed 条目过滤（根/频道页/redirect.php 跳转端点）、去重、失败降级、probe_structure |
| test_extract.py | news_agent.extract | BBC/DW/NHK/新浪/联合早报 fixtures 的标题/正文/时间/来源/语言；配置选择器优先级；通用算法；trafilatura 缺失/异常回退；URL 日期、item 兜底、中文无乱码 |
| test_output.py | news_agent.output | JSONL schema、CSV BOM/表头/行数、run_meta、模板报告、SVG/PNG 渲染与失败降级、自定义报告、空结果 |
| test_agent_offline.py | news_agent.agent（+全链路） | 本地 fixture HTTP 服务上的确定性流水线 E2E：发现→抽取→输出；GB18030 编码识别；候选 302→首页被最终 URL 校验跳过（含缓存只请求一次断言）；入口失败兜底；socket 护栏保证不联网 |
| test_fixtures.py | tests/fixtures | manifest 字段/sha256/字节数/站点覆盖、loader 解码与错误、中文与 GB18030 无乱码 |

## 测试约定

- **不联网**：离线 E2E 用 `fixture_server.FixtureSiteServer`（127.0.0.1 随机端口），
  并由 `offline_network_guard` 拦截任何非本机连接/DNS 解析。
- **不依赖可选依赖**：`test_extract.py` 默认把 `extract._TRAFILATURA_STATE` 的
  extract 槽位置空（等价未安装 trafilatura），并单独验证缺失/异常/可用三种路径。
- **不写项目 data/**：输出类测试全部使用 `tempfile.TemporaryDirectory()` 与
  `config.out_dir` 绝对路径。
