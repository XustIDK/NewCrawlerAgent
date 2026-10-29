"""路径、.env、config/settings.json 的加载，以及 CrawlConfig 工厂。

优先级：命令行/网页参数 > .env / 环境变量 > config/settings.json > 内置默认值。
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import DEFAULT_USER_AGENT, CrawlConfig

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
SITES_DIR = BASE_DIR / "sites"
DATA_DIR = BASE_DIR / "data"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
ENV_PATH = BASE_DIR / ".env"

DEFAULT_SETTINGS: dict[str, Any] = {
    "user_agent": DEFAULT_USER_AGENT,
    "crawl": {
        "max_articles": 10,
        "max_list_pages": 3,
        "max_candidates": 150,
        "timeout_s": 15,
        "retries": 2,
        "min_delay_s": 0.8,
        "respect_robots": True,
        "browser_mode": "auto",
        "browser_wait_ms": 2500,
        "headless": True,
        "max_body_chars": 20000,
        "default_timezone": "UTC",
        "language_hint": "",
        "include_patterns": [],
        "exclude_patterns": [],
    },
    "llm": {
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "max_rounds": 12,
        "temperature": 0.2,
    },
    "discovery": {
        "feed_suffixes": [
            "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml",
            "/news/feed", "/news/rss",
        ],
        "max_feeds": 5,
        "anchor_min_text": 12,
        "max_scroll": 2,
    },
    "extraction": {
        "min_body_chars": 200,
        "min_confidence": 0.35,
        "max_title_chars": 300,
    },
    "output": {
        "dir": "data",
        "formats": ["jsonl", "csv", "meta", "md"],
    },
}


def load_local_env(path: Path | str | None = None) -> dict[str, str]:
    """从项目目录 .env 读取配置；已存在的系统环境变量优先（setdefault）。"""
    env_path = Path(path) if path else ENV_PATH
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return loaded
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not name:
            continue
        loaded[name] = value
        os.environ.setdefault(name, value)
    return loaded


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_settings(path: Path | str | None = None) -> dict[str, Any]:
    """读取 config/settings.json 并与内置默认值深合并；文件缺失/损坏时用默认值。"""
    settings_path = Path(path) if path else SETTINGS_PATH
    if not settings_path.exists():
        return deepcopy(DEFAULT_SETTINGS)
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return deepcopy(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return deepcopy(DEFAULT_SETTINGS)
    return deep_merge(DEFAULT_SETTINGS, data)


def get_llm_config(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    llm = dict(settings.get("llm") or {})
    return {
        "api_key": os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        "base_url": llm.get("base_url") or "https://api.deepseek.com",
        "model": os.environ.get("DEEPSEEK_MODEL", "").strip()
        or llm.get("model")
        or "deepseek-flash",
        "max_rounds": int(llm.get("max_rounds", 12)),
        "temperature": float(llm.get("temperature", 0.2)),
    }


def resolve_output_dir(settings: dict[str, Any] | None = None, out_dir: str = "") -> Path:
    """输出目录：命令行 --out 优先，其次 settings.output.dir，默认项目 data/。"""
    settings = settings or load_settings()
    raw = out_dir or str((settings.get("output") or {}).get("dir") or "data")
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_config(
    entry_url: str,
    settings: dict[str, Any] | None = None,
    **overrides: Any,
) -> CrawlConfig:
    """CLI / 网页 / Agent 共用的 CrawlConfig 构造函数。"""
    return CrawlConfig.from_settings(entry_url, settings or load_settings(), **overrides)
