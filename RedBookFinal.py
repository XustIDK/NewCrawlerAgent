"""小红书调研 Agent：DeepSeek Function Calling + 本地工具 + Agent Loop。"""

import csv
import json
import os
import random
import re
import time
import textwrap
from html import escape
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from lxml import etree
from openai import OpenAI
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
MAX_NOTES = 20
MAX_SEARCH_CALLS = 5


def load_local_env():
    """从脚本目录读取 .env；已有系统环境变量优先。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


load_local_env()


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "在小红书按关键词搜索并采集笔记。调研时可用不同关键词多次调用；每次最多 8 篇。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "本轮具体搜索词"},
                    "limit": {"type": "integer", "description": "本轮最多采集数量，1 到 8"},
                },
                "required": ["keyword", "limit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_notes",
            "description": "检查当前已经采集到的笔记数量和高频词线索，供你判断是否需要换关键词补充搜索。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_report",
            "description": "保存最终调研报告 Markdown 和笔记 CSV。报告应包含总结、证据/例子、局限，并引用笔记来源链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "report_markdown": {"type": "string", "description": "完整的中文调研报告 Markdown"},
                },
                "required": ["report_markdown"],
            },
        },
    },
]


def xp(tree, expression):
    """取 XPath 文本并拼接。"""
    if tree is None:
        return ""
    return "".join(tree.xpath(expression)).strip()


def xp_first(tree, expression):
    """只取 XPath 第一个匹配节点，避免把页面其他卡片文本拼进当前字段。"""
    if tree is None:
        return ""
    nodes = tree.xpath(expression)
    if not nodes:
        return ""
    node = nodes[0]
    if isinstance(node, str):
        return node.strip()
    return "".join(node.itertext()).strip()


def extract_note_title(tree):
    """只从详情区域取标题；拿不到就留空，不能退回整页搜索结果文本。"""
    candidates = (
        '//*[@id="detail-title"]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " note-content ")]'
        '//*[contains(concat(" ", normalize-space(@class), " "), " title ")]',
        '//meta[@property="og:title"]/@content',
    )
    for expression in candidates:
        title = " ".join(xp_first(tree, expression).split())
        # 小红书标题不会有数百字符；异常时宁可标空，避免把 feed 卡片串进标题列。
        if title and len(title) <= 100 and "小红书" not in title:
            return title
    return ""


def notify(state, message, phase=None):
    if phase:
        state["phase"] = phase
    callback = state.get("on_event")
    if callback:
        callback(message, phase)


def scrape_notes(page, keyword, limit, state):
    """工具实现：搜索一个关键词并采集笔记，结果写入共享 state。"""
    keyword = str(keyword).strip()
    limit = max(1, min(int(limit), 8, MAX_NOTES - len(state["notes"])))
    if not keyword:
        raise ValueError("keyword 不能为空")
    if limit <= 0:
        return {"error": f"已达到本次最多采集 {MAX_NOTES} 篇的限制。"}

    search_url = "https://www.xiaohongshu.com/search_result?keyword=" + quote(keyword)
    page.goto(search_url)
    try:
        page.wait_for_selector("section.note-item", timeout=10000)
    except Exception:
        if state.get("login_event"):
            notify(state, "请在打开的 Chrome 中登录小红书，然后回到页面点击“我已登录，继续”。", "waiting_login")
            if not state["login_event"].wait(timeout=300):
                raise TimeoutError("等待网页确认登录超时。")
        else:
            input("请在 Chrome 中登录小红书，完成后按回车继续……")
        page.goto(search_url)
        page.wait_for_selector("section.note-item", timeout=20000)

    links, seen, no_new = [], set(), 0
    while len(links) < limit and no_new < 4:
        previous_count = len(seen)
        tree = etree.HTML(page.content())
        for section in tree.xpath('//section[contains(@class,"note-item")]'):
            hrefs = section.xpath('.//a[contains(@href,"/search_result/")]/@href')
            if not hrefs:
                continue
            url = hrefs[0].replace("search_result", "explore")
            url = url.split("&xsec_source=")[0] + "&xsec_source=pc_search"
            if url.startswith("/"):
                url = "https://www.xiaohongshu.com" + url
            if url not in seen:
                seen.add(url)
                links.append(url)
        no_new = 0 if len(seen) > previous_count else no_new + 1
        if len(links) < limit:
            page.mouse.wheel(0, 800)
            time.sleep(1.5)

    existing_urls = {note["url"] for note in state["notes"]}
    added = []
    for index, url in enumerate(links[:limit], start=1):
        if url in existing_urls:
            continue
        try:
            page.goto(url)
            page.wait_for_selector(".engage-bar", timeout=10000)
            time.sleep(random.uniform(1.5, 3))
            tree = etree.HTML(page.content())
            note = {
                "keyword": keyword,
                "url": url,
                "title": extract_note_title(tree),
                "author": (
                    xp_first(tree, '//*[@class="author-container"]//*[@class="name"]')
                    or xp_first(tree, '//*[contains(@class,"author")][.//*[@class="tag"]]//*[@class="name"]')
                ),
                "content": xp(tree, '//span[contains(@class,"note-text")][not(ancestor::*[contains(@class,"comment-item")])]//text()'),
                "likes": xp(tree, '//*[contains(@class,"engage-bar")]//*[contains(@class,"like-wrapper")]//*[@class="count"]//text()'),
                "collects": xp(tree, '//*[contains(@class,"engage-bar")]//*[contains(@class,"collect-wrapper")]//*[@class="count"]//text()'),
            }
            state["notes"].append(note)
            added.append(note)
            existing_urls.add(url)
            print(f"  采集到第 {len(state['notes'])} 篇：{note['title'] or '无标题'}")
            notify(state, f"已采集 {len(state['notes'])}/{state['target']} 篇：{note['title'] or '无标题'}", "collecting")
        except Exception as error:
            state["errors"].append(f"{url}: {error}")
            print(f"  这篇笔记采集失败：{error}")
        time.sleep(random.uniform(1, 2))

    state["searches"].append({"keyword": keyword, "requested": limit, "added": len(added)})
    return {
        "keyword": keyword,
        "this_search_added": len(added),
        "total_unique_notes": len(state["notes"]),
        "target_notes": state["target"],
        "evidence": [
            {
                "title": note["title"],
                "content_excerpt": note["content"][:350],
                "likes": note["likes"],
                "url": note["url"],
            }
            for note in added
        ],
        "hint": "请根据结果判断是否换一个更具体/不同角度的关键词继续搜索。",
    }


def analyze_notes(state):
    """工具实现：返回采集覆盖和简单词频证据，不把词频冒充语义结论。"""
    tokens = Counter()
    for note in state["notes"]:
        text = f"{note['title']} {note['content']}".lower()
        # 中文拆成相邻二字片段；英文按词提取，作为模型复核的线索。
        tokens.update(re.findall(r"[a-z][a-z0-9+#.-]*", text))
        chinese = re.findall(r"[\u4e00-\u9fff]", text)
        tokens.update(chinese[i] + chinese[i + 1] for i in range(len(chinese) - 1))
    stop = set("这个 我们 你们 他们 不是 可以 真的 感觉 一个 一种 因为 所以 然后 自己 现在 时候 怎么 什么 进行 使用 通过".split())
    common = [(word, count) for word, count in tokens.most_common(30) if word not in stop and len(word) > 1][:12]
    return {
        "notes_collected": len(state["notes"]),
        "target_notes": state["target"],
        "searches": state["searches"],
        "top_word_clues": [{"term": word, "count": count} for word, count in common],
        "warning": "词频只是线索；报告结论应回看笔记内容，并用链接支持具体例子。",
    }


def output_stem(goal):
    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", goal).strip("_-")[:36]
    return stem or "xiaohongshu_research"


def _svg_lines(text, width, max_lines):
    text = " ".join(str(text).replace("\n", " ").split())
    if not text:
        return ["（无内容）"]
    lines = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max(1, width - 1)] + "…"
    return lines


def _svg_text(x, y, text, width=28, max_lines=3, size=15, color="#38443e", weight="400"):
    lines = _svg_lines(text, width, max_lines)
    spans = []
    for index, line in enumerate(lines):
        dy = "0" if index == 0 else "21"
        spans.append(f'<tspan x="{x}" dy="{dy}">{escape(line)}</tspan>')
    return f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}">{"".join(spans)}</text>'


def _trace_result_lines(call, target):
    name = call["tool"]
    result = call.get("result") or {}
    args = call.get("arguments") or {}
    if result.get("error"):
        return [f"工具返回错误：{result['error']}"]
    if name == "search_notes":
        lines = [
            f"本轮新增 {result.get('this_search_added', 0)} 篇；累计 {result.get('total_unique_notes', 0)}/{target} 篇",
        ]
        evidence = result.get("evidence") or []
        titles = [item.get("title") for item in evidence if item.get("title")]
        if titles:
            lines.append("返回笔记：" + "；".join(titles[:2]))
        elif evidence:
            snippets = [item.get("content_excerpt", "")[:55] for item in evidence[:2] if item.get("content_excerpt")]
            if snippets:
                lines.append("正文线索：" + "；".join(snippets))
        elif not result.get("this_search_added"):
            lines.append("没有采集到新笔记")
        return lines
    if name == "analyze_notes":
        clues = result.get("top_word_clues") or []
        terms = [f"{item['term']}（{item['count']}）" for item in clues[:7]]
        return [
            f"检查 {result.get('notes_collected', 0)}/{result.get('target_notes', target)} 篇笔记",
            "返回关键词线索：" + ("、".join(terms) if terms else "无明显高频词"),
        ]
    if name == "save_report":
        return [
            "报告与 CSV 已写入本目录" if result.get("saved") else "保存未完成",
            Path(result.get("report_path", "报告文件")).name,
            Path(result.get("csv_path", "CSV 文件")).name,
        ]
    return [json.dumps(result, ensure_ascii=False)[:120]]


def save_agent_loop_diagram(state, page=None):
    """按本次真实工具调用轨迹生成 PNG/SVG 流程图，和 CSV/报告保存在一起。"""
    trace = state.get("loop_trace", [])
    goal = state.get("goal", "本次调研")
    target = state.get("target", 0)
    collected = len(state.get("notes", []))
    width, top, row_height, gap = 1240, 270, 190, 45
    rows = max(1, len(trace))
    height = top + rows * row_height + max(0, rows - 1) * gap + 100
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="Arial, sans-serif">',
        '<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="8" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#688477"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="#f5f7f4"/>',
        '<text x="48" y="55" font-size="15" font-weight="700" letter-spacing="1.5" fill="#587264">AGENT RUN TRACE</text>',
        '<text x="48" y="100" font-size="29" font-weight="700" fill="#202a24">本次调研的 Agent Loop</text>',
        _svg_text(48, 133, f"调研目标：{goal}", 68, 2, 16, "#57645c"),
        f'<text x="48" y="183" font-size="14" fill="#718077">目标采集 {target} 篇 · 实际采集 {collected} 篇 · 模型决策轮次 {len(trace)}</text>',
        '<rect x="48" y="207" width="1144" height="45" rx="10" fill="#e8eee9"/>',
        '<text x="66" y="235" font-size="14" fill="#40564a">循环含义：模型选择工具 → Python 执行 → 工具结果回传模型 → 模型据此选择下一步</text>',
    ]

    model_x, model_w = 48, 260
    tool_x, tool_w = 408, 344
    result_x, result_w = 852, 340
    card_h = 166
    if not trace:
        y = top
        parts.extend([
            f'<rect x="48" y="{y}" width="1144" height="{card_h}" rx="14" fill="#fff" stroke="#dce4dd"/>',
            _svg_text(72, y + 45, "本次没有记录到工具调用。若任务在 API 请求阶段失败，请检查运行日志。", 65, 2, 16),
        ])

    for index, turn in enumerate(trace):
        y = top + index * (row_height + gap)
        calls = turn.get("calls") or []
        if not calls:
            calls = [{"tool": "未知工具", "arguments": {}, "result": {}}]

        tool_call_lines = []
        for call in calls:
            tool_name = call.get("tool", "未知工具")
            args = call.get("arguments") or {}
            if tool_name == "search_notes":
                tool_call_lines.append(f"search_notes(keyword={args.get('keyword', '未提供')!r}, limit={args.get('limit', '—')})")
            elif tool_name == "analyze_notes":
                tool_call_lines.append("analyze_notes() · 检查当前笔记")
            elif tool_name == "save_report":
                tool_call_lines.append("save_report() · 写入报告和 CSV")
            else:
                tool_call_lines.append(tool_name)

        return_lines = []
        for call in calls:
            call_name = call.get("tool", "")
            if len(calls) > 1:
                call_args = call.get("arguments") or {}
                if call_name == "search_notes":
                    return_lines.append(f"搜索「{call_args.get('keyword', '')}」：新增 {(call.get('result') or {}).get('this_search_added', 0)} 篇")
                else:
                    return_lines.extend(_trace_result_lines(call, target))
            else:
                return_lines.extend(_trace_result_lines(call, target))
        return_lines = return_lines[:5] or ["工具已执行，结果已回传模型"]

        parts.extend([
            f'<text x="48" y="{y - 14}" font-size="13" font-weight="700" fill="#78877d">LOOP {turn.get("round", index + 1):02d}</text>',
            f'<rect x="{model_x}" y="{y}" width="{model_w}" height="{card_h}" rx="13" fill="#284f40"/>',
            _svg_text(model_x + 18, y + 34, "DeepSeek 模型", 24, 1, 16, "#ffffff", "700"),
            _svg_text(model_x + 18, y + 66, "根据任务/上轮结果选择动作", 28, 2, 13, "#dce9e0"),
            f'<path d="M {model_x + model_w + 8} {y + 56} L {tool_x - 12} {y + 56}" stroke="#688477" stroke-width="2" marker-end="url(#arrow)"/>',
            f'<rect x="{tool_x}" y="{y}" width="{tool_w}" height="{card_h}" rx="13" fill="#ffffff" stroke="#dbe3dc"/>',
            _svg_text(tool_x + 18, y + 30, "Function Calling · 本地执行 Tool", 37, 1, 13, "#668071", "700"),
            _svg_text(tool_x + 18, y + 56, " · ".join(tool_call_lines), 39, 6, 12, "#26342c", "600"),
            f'<path d="M {tool_x + tool_w + 8} {y + 56} L {result_x - 12} {y + 56}" stroke="#688477" stroke-width="2" marker-end="url(#arrow)"/>',
            f'<rect x="{result_x}" y="{y}" width="{result_w}" height="{card_h}" rx="13" fill="#edf3ee" stroke="#d3e0d6"/>',
            _svg_text(result_x + 16, y + 29, "TOOL RESULT", 30, 1, 12, "#557360", "700"),
        ])
        for line_index, line in enumerate(return_lines):
            parts.append(_svg_text(result_x + 16, y + 53 + line_index * 19, line, 39, 1, 12, "#34473b"))

        if index < len(trace) - 1:
            next_y = top + (index + 1) * (row_height + gap)
            # 沿图右侧和左侧返回下一轮模型，清楚标出 loop 的结果回传。
            parts.extend([
                f'<path d="M {result_x + result_w - 14} {y + card_h} L {result_x + result_w - 14} {y + card_h + 17} L 30 {y + card_h + 17} L 30 {next_y + 56} L {model_x - 2} {next_y + 56}" fill="none" stroke="#688477" stroke-width="2" marker-end="url(#arrow)"/>',
                f'<rect x="430" y="{y + card_h + 5}" width="350" height="24" rx="8" fill="#f5f7f4"/>',
                _svg_text(442, y + card_h + 22, "工具结果回传，模型继续判断", 36, 1, 12, "#587264", "600"),
            ])

    footer_y = height - 53
    parts.extend([
        f'<text x="48" y="{footer_y}" font-size="13" fill="#748078">流程记录来自本次实际工具调用；箭头表示数据流，不展示模型隐藏推理。</text>',
        "</svg>",
    ])
    svg_path = BASE_DIR / f"{output_stem(goal)}_agent_loop.svg"
    png_path = BASE_DIR / f"{output_stem(goal)}_agent_loop.png"
    svg_source = "\n".join(parts)
    svg_path.write_text(svg_source, encoding="utf-8")
    if page is not None:
        try:
            page.set_viewport_size({"width": width, "height": height})
            page.set_content(svg_source, wait_until="load")
            page.screenshot(path=str(png_path), full_page=True)
        except Exception as error:
            print(f"PNG 导出失败，已保留 SVG 流程图：{error}")
            png_path = None
    return png_path or svg_path, svg_path


def save_report(report_markdown, state):
    """工具实现：将最终报告和原始笔记保存到脚本目录。"""
    safe_name = output_stem(state["goal"])
    report_path = BASE_DIR / f"{safe_name}_report.md"
    csv_path = BASE_DIR / f"{safe_name}_notes.csv"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["搜索词", "链接", "标题", "作者", "正文", "点赞", "收藏"])
        for note in state["notes"]:
            writer.writerow([
                note["keyword"], note["url"], note["title"], note["author"],
                note["content"], note["likes"], note["collects"],
            ])

    report = str(report_markdown).strip()
    if state["errors"]:
        report += "\n\n## 采集问题\n\n" + "\n".join(f"- {error}" for error in state["errors"][:10])
    report += f"\n\n---\n本次采集 {len(state['notes'])} 篇笔记；原始数据见 `{csv_path.name}`。"
    report_path.write_text(report, encoding="utf-8")
    state["report_path"] = str(report_path)
    state["csv_path"] = str(csv_path)
    return {"saved": True, "report_path": str(report_path), "csv_path": str(csv_path)}


def run_tool(name, arguments, page, state):
    if name == "search_notes":
        if len(state["searches"]) >= MAX_SEARCH_CALLS:
            return {"error": f"已达到最多 {MAX_SEARCH_CALLS} 次搜索调用。请根据现有结果继续总结。"}
        if len(state["notes"]) >= state["target"]:
            return {"error": "已达到用户设定的采集数量，请转入分析和总结。"}
        return scrape_notes(page, arguments.get("keyword", ""), arguments.get("limit", 3), state)
    if name == "analyze_notes":
        return analyze_notes(state)
    if name == "save_report":
        return save_report(arguments.get("report_markdown", ""), state)
    return {"error": f"未知工具：{name}"}


def run_agent(client, goal, target, page, on_event=None, login_event=None):
    """模型决策 -> 本地执行工具 -> 工具结果回传模型，直到模型给出最终答复。"""
    state = {
        "goal": goal,
        "target": target,
        "notes": [],
        "searches": [],
        "errors": [],
        "report_path": None,
        "loop_trace": [],
        "on_event": on_event,
        "login_event": login_event,
    }
    notify(state, "DeepSeek 正在规划调研步骤……", "planning")
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个严谨的小红书调研 Agent。根据用户目标自主规划不同搜索角度；"
                "先至少搜索一次，再调用 analyze_notes 检查覆盖和词频线索；如果样本不足或角度单一，"
                "换关键词补搜。达到用户目标或搜索次数上限后，基于实际采集内容写中文报告，"
                "包含主要发现、证据例子、样本局限，并在具体例子旁引用来源链接。"
                "不要把点赞量当作真实性证明，不要编造笔记内容。最后必须调用 save_report 保存报告。"
                "如果没有采集到笔记，要如实说明，并且仍然保存失败说明。"
            ),
        },
        {"role": "user", "content": f"调研目标：{goal}\n最多采集 {target} 篇不同笔记。请开始规划并执行。"},
    ]

    final_answer = ""
    for step in range(12):
        response = client.chat.completions.create(
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            stream=False,
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            final_answer = message.content or "调研已结束。"
            break

        turn_trace = {"round": step + 1, "calls": []}
        state["loop_trace"].append(turn_trace)
        for call in message.tool_calls:
            tool_name = call.function.name
            arguments = {}
            try:
                arguments = json.loads(call.function.arguments or "{}")
                print(f"\n[Agent Loop {step + 1}] 模型请求调用工具：{tool_name}({json.dumps(arguments, ensure_ascii=False)})")
                notify(state, f"Agent Loop 第 {step + 1} 轮：调用 {tool_name}", "running")
                result = run_tool(tool_name, arguments, page, state)
            except Exception as error:
                result = {"error": f"工具执行失败：{type(error).__name__}: {error}"}
            turn_trace["calls"].append({
                "tool": tool_name,
                "arguments": arguments,
                "result": result,
            })
            print(f"[工具返回] {json.dumps(result, ensure_ascii=False)[:1200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
            if tool_name == "save_report" and state["report_path"]:
                notify(state, "报告和 CSV 已保存。", "saving")

    if not state["report_path"]:
        fallback_report = final_answer or "本次调研未能生成总结。请检查 API、登录状态和采集错误后重试。"
        saved = save_report(fallback_report, state)
        print(f"\n模型未调用 save_report，程序已保存兜底报告：{saved['report_path']}")
        notify(state, "已保存调研报告。", "saving")
    diagram_path, svg_path = save_agent_loop_diagram(state, page)
    state["diagram_path"] = str(diagram_path)
    state["diagram_svg_path"] = str(svg_path)
    if state.get("report_path"):
        with Path(state["report_path"]).open("a", encoding="utf-8") as report_file:
            report_file.write(f"\nAgent Loop 流程图：`{diagram_path.name}`（矢量版：`{svg_path.name}`）\n")
    notify(state, f"Agent Loop 图已保存：{diagram_path.name}", "saving")
    notify(state, "调研完成。", "completed")
    return final_answer, state


def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("没有检测到 DEEPSEEK_API_KEY。请先在终端设置环境变量后再运行。")
        print("新建并轮换 API key 后，在当前终端执行：")
        print("  export DEEPSEEK_API_KEY='你的新 key'")
        return

    goal = input("你想调研什么主题？\n> ").strip()
    if not goal:
        print("主题不能为空。")
        return
    try:
        target = max(1, min(int(input("最多采集多少篇笔记（1-20）？\n> ")), MAX_NOTES))
    except ValueError:
        print("请输入有效的整数。")
        return

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    print("\nAgent 已启动：模型将规划搜索角度、调用工具，并根据工具结果决定下一步。")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        page = browser.new_page()
        try:
            answer, state = run_agent(client, goal, target, page)
        finally:
            browser.close()

    print("\n=== Agent 最终回复 ===\n")
    print(answer)
    print(f"\n采集笔记：{len(state['notes'])} 篇")
    print(f"报告：{state['report_path']}")
    print(f"CSV：{state['csv_path']}")
    print(f"Agent Loop 图：{state['diagram_path']}")
    print(f"Agent Loop 矢量图：{state['diagram_svg_path']}")


if __name__ == "__main__":
    main()
