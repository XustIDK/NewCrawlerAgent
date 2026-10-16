# 小红书调研 Agent

一个用于学习 Agent 基本机制的本地原型。用户输入调研目标后，DeepSeek 模型通过 Function Calling 选择本地 Python 工具；程序执行工具并把结果返回模型，模型可根据采集反馈继续搜索或生成总结。

## 能力

- `search_notes`：使用本机 Chrome 搜索并采集小红书笔记
- `analyze_notes`：统计采集覆盖和高频词线索，辅助判断是否补充搜索
- `save_report`：保存调研报告、结构化 CSV 和本次实际工具调用生成的 Agent Loop 图（PNG 与 SVG）
- 轻量本地网页：查看任务进度、登录提示、调研结果和生成文件

> 页面结构和访问状态可能影响采集结果；词频仅作为线索，不能直接当作语义结论。

## 环境要求

- Python 3.10 或更新版本
- 已安装 Google Chrome
- DeepSeek API Key

## 安装

在项目目录打开终端：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

打开 `.env`，把 `DEEPSEEK_API_KEY` 的占位值替换为自己的密钥。不要把 `.env` 或真实密钥提交到 GitHub。

## 启动网页

```bash
source .venv/bin/activate
python web_app.py
```

浏览器打开：<http://127.0.0.1:8765>

提交调研主题后程序会打开 Chrome。若小红书要求登录，在 Chrome 中完成登录，再回到网页点击“我已登录，继续”。任务产物保存在项目目录。

## 命令行运行

```bash
source .venv/bin/activate
python RedBookFinal.py
```

按提示输入调研主题和最多采集数量。

## Agent Loop 概览

```text
用户目标 → 模型选择工具与参数 → Python 执行工具
       ↑                              ↓
       └──── 工具结果返回模型 ←────────┘
```

Function Calling 是模型给出的结构化工具调用请求；实际执行由本地 Python 程序完成。程序把工具结果交还模型，模型再决定继续调用还是结束回答。
