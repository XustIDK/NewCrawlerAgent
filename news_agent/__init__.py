"""新闻抓取 Agent。

输入任意新闻网站 URL，自动发现新闻列表、抓取文章并抽取
标题 / 正文 / 发布时间 / 来源 / 作者 / 语言等关键字段，输出结构化数据集。

模块划分（详细契约见仓库根目录 DESIGN.md）：

- models    : 数据模型（NewsItem / Article / SiteProfile / CrawlConfig / CrawlResult）
- urls      : URL 规范化、同站判断等纯函数
- dom       : HTML 解析、CSS(子集)/XPath 选择器、JSON-LD / meta 读取
- config    : .env、config/settings.json、CrawlConfig 工厂
- fetch     : 静态 HTTP 抓取 + Playwright 浏览器回退（含 robots / 限速 / 重试）
- discover  : 新闻列表发现（站点配置 / RSS / JSON-LD / 启发式聚类 / 翻页）
- extract   : 文章字段抽取（配置选择器 / 结构化数据 / 通用正文算法）
- normalize : 多语言日期、语言识别、文本清洗、URL 归一
- profiles  : 站点配置（sites/*.json）读写、校验、选择器解析
- agent     : Agent Loop（DeepSeek Function Calling）与确定性流水线
- tools     : 暴露给模型的工具 schema 与分发
- output    : JSONL / CSV / run_meta / Markdown 报告 / Agent Loop 图
- cli       : 命令行入口（python -m news_agent）
"""

__version__ = "1.0.0"

from .models import (  # noqa: F401
    SCHEMA_VERSION,
    Article,
    CrawlConfig,
    CrawlResult,
    CrawlStats,
    NewsItem,
    SiteProfile,
)

__all__ = [
    "SCHEMA_VERSION",
    "Article",
    "CrawlConfig",
    "CrawlResult",
    "CrawlStats",
    "NewsItem",
    "SiteProfile",
]
