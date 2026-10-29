# 站点配置（sites/）

每个文件描述一个新闻站点的抓取规则，文件名即归一化域名（去掉前导 `www.`），例如：

- `sites/bbc.com.json` → 匹配 `bbc.com`、`www.bbc.com`、`m.bbc.com`
- `sites/lemonde.fr.json` → 匹配 `lemonde.fr`、`www.lemonde.fr`

**没有配置也能抓**：程序默认走“通用策略”（RSS/JSON-LD/链接聚类/正文算法/多语言日期），
只有站点结构特殊、或需要更高抽取质量时才写配置。

## 选择器语法

- CSS 子集：`article h2`、`a.headline[href]`、`#main .item`，支持 `>`、`+`、`~` 与逗号分组
- XPath：`xpath://article//h1`
- 取值后缀：`::text`（默认）、`::attr(href)`、`::html`

## 字段说明

| 分组 | 字段 | 说明 |
| --- | --- | --- |
| `fetch` | `mode` | `auto`（静态失败自动开浏览器）、`always`、`never` |
| | `encoding` | `auto` 或强制编码（如 `gbk`、`shift_jis`） |
| | `wait_selector` | 浏览器模式下等待出现的元素 |
| `discovery` | `feeds` | 手动指定 RSS/Atom 地址 |
| | `use_jsonld` / `use_heuristics` | 是否使用结构化数据 / 通用启发式 |
| | `max_scroll` | 列表页自动滚动的次数（懒加载站点） |
| `list` | `url` | 列表页地址（留空则用用户输入的网址） |
| | `item` | 列表项链接选择器（通常带 `::attr(href)`） |
| | `item_title` / `item_date` / `item_section` / `item_image` | 列表项附加字段 |
| | `pagination.next` / `url_template` / `max_pages` | 翻页规则 |
| `article` | `title` / `body` / `date` / `author` / `source` / `summary` / `section` | 文章字段选择器 |
| | `exclude` | 正文容器内需要删除的噪声选择器 |
| 顶层 | `include` / `exclude` | 文章 URL 的正则白名单/黑名单 |
| | `note` / `learned_by` | 备注与来源（human / agent） |

## 由 Agent 自动生成

当通用抽取质量不佳时，LLM Agent 会先调用 `inspect_site` 查看页面结构，再调用
`save_site_profile` 把推断出的选择器写入本目录并复抓。自动生成的配置会带
`"learned_by": "agent"`，可人工复核后保留或删除。
