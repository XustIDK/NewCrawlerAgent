"""统一的异常类型，便于 Agent/CLI/网页区分“可恢复”与“不可恢复”错误。"""

from __future__ import annotations


class NewsAgentError(Exception):
    """本项目所有自定义异常的基类。"""


class FetchError(NewsAgentError):
    """网络抓取失败（超时、状态码、TLS、浏览器异常等）。"""


class RobotsDenied(FetchError):
    """robots.txt 禁止访问；尊重 robots 时作为可恢复错误记录。"""


class ExtractError(NewsAgentError):
    """文章字段抽取失败（页面结构无法识别、正文过短等）。"""


class ProfileError(NewsAgentError):
    """站点配置非法（JSON 结构、必填字段、选择器语法）。"""
