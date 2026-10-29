"""新闻抓取 Agent 的轻量本地网页。

沿用原项目的“stdlib HTTPServer + 后台线程 + 轮询进度 + 人工介入”结构：
- 输入任意新闻网站 URL，选择抓取篇数与运行模式（LLM Agent / 确定性流水线）；
- 进度、错误、人工处理（验证码/Cloudflare）提示实时展示；
- 完成后展示报告、文章预览，并提供 JSONL/CSV/run_meta/报告/流程图的下载链接。

只依赖标准库 + news_agent 包，不引入 Web 框架。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from news_agent.agent import run_agent, run_deterministic
from news_agent.config import build_config, get_llm_config, load_local_env, load_settings
from news_agent.urls import normalize_entry_url

HOST = "127.0.0.1"
PORT = 8765
MAX_ARTICLES_LIMIT = 50

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>新闻抓取 Agent</title>
  <style>
    :root { color-scheme: light; --ink:#20221f; --muted:#74776f; --line:#e9e9e4; --green:#334f43; --paper:#f7f7f3; --white:#fff; --warn:#8a5a12; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--paper); color:var(--ink); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { width:min(880px,calc(100% - 32px)); margin:46px auto 70px; }
    .eyebrow { color:var(--green); font-size:12px; letter-spacing:.12em; font-weight:700; text-transform:uppercase; }
    h1 { margin:8px 0 6px; font-size:30px; letter-spacing:-.04em; }
    .intro { margin:0 0 26px; color:var(--muted); }
    .card { background:var(--white); border:1px solid var(--line); border-radius:14px; padding:24px; box-shadow:0 5px 20px #25291e08; }
    label { display:block; margin-bottom:8px; font-size:13px; font-weight:650; }
    input[type=url], input[type=number], textarea { width:100%; border:1px solid #dcded6; border-radius:8px; padding:12px 13px; background:#fff; color:var(--ink); font:inherit; outline:none; }
    input:focus, textarea:focus { border-color:#718878; box-shadow:0 0 0 3px #334f4312; }
    .row { display:flex; align-items:end; gap:14px; margin-top:16px; flex-wrap:wrap; }
    .count { width:130px; }
    .options { display:flex; gap:18px; flex-wrap:wrap; margin-top:14px; color:#4a4f47; font-size:13px; }
    .options label { display:flex; align-items:center; gap:7px; margin:0; font-weight:500; }
    button { border:0; border-radius:8px; padding:11px 17px; background:var(--green); color:#fff; font:inherit; font-weight:650; cursor:pointer; }
    button:disabled { opacity:.5; cursor:wait; }
    button.secondary { background:#ecefe9; color:var(--green); }
    .hint { color:var(--muted); font-size:12px; margin:13px 0 0; }
    .section { margin-top:18px; }
    .statusline { display:flex; justify-content:space-between; gap:12px; align-items:center; }
    #status { font-weight:650; }
    #phase { color:var(--muted); font-size:12px; }
    .logs { margin-top:12px; max-height:200px; overflow:auto; border-radius:8px; background:#f7f8f5; padding:12px 14px; color:#55594f; font-size:13px; white-space:pre-wrap; }
    .actions { margin-top:12px; display:flex; gap:9px; }
    .report { margin-top:22px; }
    .report h2 { margin:0 0 10px; font-size:17px; }
    .chip { display:inline-block; margin:0 8px 8px 0; padding:3px 9px; border-radius:999px; background:#eef2ee; color:#3c574a; font-size:12px; }
    .files { margin:10px 0 0; display:flex; flex-wrap:wrap; gap:8px; }
    .files a { padding:6px 11px; border:1px solid var(--line); border-radius:999px; background:#fff; color:var(--green); font-size:12px; text-decoration:none; }
    .files a:hover { border-color:#a9bbae; }
    #report { white-space:pre-wrap; overflow-wrap:anywhere; max-height:460px; overflow:auto; padding:18px; border:1px solid var(--line); border-radius:9px; background:#fff; font:13px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace; }
    .table { width:100%; border-collapse:collapse; margin-top:12px; font-size:13px; }
    .table th, .table td { text-align:left; padding:7px 8px; border-bottom:1px solid #eef0ea; vertical-align:top; }
    .table th { color:var(--muted); font-weight:600; font-size:12px; }
    .table td a { color:var(--green); }
    .error { color:#9a3b2f; }
    [hidden] { display:none !important; }
    @media(max-width:560px) { main{margin-top:30px}.card{padding:18px}.row{align-items:stretch;flex-direction:column}.count{width:100%}.row button{width:100%}h1{font-size:26px} }
  </style>
</head>
<body>
  <main>
    <div class="eyebrow">News crawler agent</div>
    <h1>新闻抓取 Agent</h1>
    <p class="intro">输入任意新闻网站的列表页 / 首页网址，Agent 自动发现新闻、抓取正文并输出结构化数据集（标题、正文、发布时间、来源、作者、语言等）。</p>
    <section class="card">
      <form id="form">
        <label for="url">新闻网站网址</label>
        <input id="url" type="url" required placeholder="例如：https://www.bbc.com/news 或 https://news.sina.com.cn/">
        <div class="row">
          <div class="count">
            <label for="limit">抓取篇数</label>
            <input id="limit" type="number" value="8" min="1" max="50" required>
          </div>
          <button id="start" type="submit">开始抓取</button>
        </div>
        <div class="options">
          <label><input id="useLlm" type="checkbox" checked> 使用 LLM 分析（Agent 模式）</label>
          <label><input id="visible" type="checkbox"> 显示浏览器（处理验证码 / JS 页面）</label>
        </div>
        <p class="hint" id="llmHint">LLM 模式会读取站点结构、自检字段完整率并在必要时生成站点配置；未配置 Key 时可取消勾选，改用确定性流水线。</p>
      </form>

      <div class="section" id="progress" hidden>
        <div class="statusline"><span id="status">等待开始</span><span id="phase"></span></div>
        <div class="logs" id="logs"></div>
        <div class="actions" id="interventionActions" hidden>
          <button class="secondary" id="interventionDone" type="button">我已处理，继续抓取</button>
        </div>
      </div>

      <div class="report" id="reportSection" hidden>
        <h2>抓取结果</h2>
        <div id="summary"></div>
        <div class="files" id="files"></div>
        <table class="table" id="articleTable" hidden>
          <thead><tr><th>标题</th><th>来源</th><th>发布时间</th><th>链接</th></tr></thead>
          <tbody id="articleBody"></tbody>
        </table>
        <h2 style="margin-top:20px">报告</h2>
        <div id="report"></div>
      </div>
    </section>
  </main>
  <script>
    const form = document.querySelector('#form');
    const startButton = document.querySelector('#start');
    const progress = document.querySelector('#progress');
    const statusEl = document.querySelector('#status');
    const phaseEl = document.querySelector('#phase');
    const logsEl = document.querySelector('#logs');
    const interventionActions = document.querySelector('#interventionActions');
    const reportSection = document.querySelector('#reportSection');
    const summaryEl = document.querySelector('#summary');
    const filesEl = document.querySelector('#files');
    const articleTable = document.querySelector('#articleTable');
    const articleBody = document.querySelector('#articleBody');
    const reportEl = document.querySelector('#report');
    const llmHint = document.querySelector('#llmHint');
    let poller = null, previousLogs = 0;

    fetch('/api/config').then(r => r.json()).then(data => {
      if (!data.llm_available) {
        document.querySelector('#useLlm').checked = false;
        llmHint.textContent = '未检测到 DEEPSEEK_API_KEY：默认使用确定性流水线；在 .env 配置 Key 后刷新页面即可启用 Agent 模式。';
      }
    }).catch(() => {});

    form.addEventListener('submit', async event => {
      event.preventDefault();
      startButton.disabled = true;
      progress.hidden = false;
      reportSection.hidden = true;
      previousLogs = 0;
      logsEl.textContent = '';
      statusEl.textContent = '正在启动…';
      try {
        const response = await fetch('/api/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            url: document.querySelector('#url').value,
            limit: Number(document.querySelector('#limit').value),
            use_llm: document.querySelector('#useLlm').checked,
            visible: document.querySelector('#visible').checked
          })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '启动失败');
        if (poller) clearInterval(poller);
        poller = setInterval(refresh, 1000);
        refresh();
      } catch (error) {
        statusEl.textContent = error.message;
        startButton.disabled = false;
      }
    });

    document.querySelector('#interventionDone').addEventListener('click', async () => {
      await fetch('/api/intervention', {method: 'POST'});
      interventionActions.hidden = true;
    });

    function esc(text) {
      const div = document.createElement('div');
      div.textContent = text == null ? '' : String(text);
      return div.innerHTML;
    }

    function renderArticles(articles) {
      articleBody.innerHTML = '';
      (articles || []).forEach(item => {
        const tr = document.createElement('tr');
        const link = item.url ? '<a href="' + esc(item.url) + '" target="_blank" rel="noopener">打开</a>' : '';
        tr.innerHTML = '<td>' + esc(item.title || '（无标题）') + '</td>' +
          '<td>' + esc(item.source || '') + '</td>' +
          '<td>' + esc(item.published_at || '') + '</td>' +
          '<td>' + link + '</td>';
        articleBody.appendChild(tr);
      });
      articleTable.hidden = !(articles && articles.length);
    }

    function renderFiles(files) {
      filesEl.innerHTML = '';
      (files || []).forEach(file => {
        const link = document.createElement('a');
        link.href = '/api/download?name=' + encodeURIComponent(file.name);
        link.textContent = file.label + ' · ' + file.size_label;
        filesEl.appendChild(link);
      });
    }

    async function refresh() {
      try {
        const response = await fetch('/api/status');
        const data = await response.json();
        statusEl.textContent = data.message || '运行中';
        phaseEl.textContent = data.phase || '';
        if (data.logs && data.logs.length > previousLogs) {
          logsEl.textContent = data.logs.join('\n');
          logsEl.scrollTop = logsEl.scrollHeight;
          previousLogs = data.logs.length;
        }
        interventionActions.hidden = !data.waiting_intervention;
        if (data.report) {
          reportSection.hidden = false;
          reportEl.textContent = data.report;
        }
        if (data.summary) {
          reportSection.hidden = false;
          summaryEl.innerHTML = '';
          (data.summary.chips || []).forEach(text => {
            const chip = document.createElement('span');
            chip.className = 'chip';
            chip.textContent = text;
            summaryEl.appendChild(chip);
          });
          renderFiles(data.files || []);
          renderArticles(data.articles || []);
        }
        if (data.error) { statusEl.textContent = data.error; statusEl.className = 'error'; }
        if (!data.running) {
          startButton.disabled = false;
          if (poller) { clearInterval(poller); poller = null; }
        }
      } catch (error) {
        statusEl.textContent = '无法连接到本地服务';
      }
    }
  </script>
</body>
</html>"""

job_lock = threading.Lock()
job = {
    "running": False,
    "phase": "",
    "message": "填写新闻网站网址后开始抓取。",
    "logs": [],
    "waiting_intervention": False,
    "intervention_event": None,
    "report": "",
    "summary": None,
    "files": [],
    "articles": [],
    "error": "",
    "output_dir": "",
}


def push_event(message, phase=None):
    with job_lock:
        job["logs"].append(message)
        job["message"] = message
        if phase:
            job["phase"] = phase
            job["waiting_intervention"] = phase == "waiting_intervention"


def describe_files(output_files):
    labels = {
        "jsonl": "结构化数据 JSONL",
        "csv": "表格 CSV",
        "meta": "运行元数据 JSON",
        "report": "抓取报告 Markdown",
        "diagram_svg": "Agent Loop SVG",
        "diagram_png": "Agent Loop PNG",
    }
    files = []
    for key, label in labels.items():
        raw = (output_files or {}).get(key)
        if not raw:
            continue
        path = Path(raw)
        if not path.exists():
            continue
        size = path.stat().st_size
        size_label = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} B"
        files.append({"name": path.name, "label": label, "size_label": size_label})
    return files


def run_job(entry_url, max_articles, use_llm, visible, intervention_event):
    settings = load_settings()
    config = build_config(
        entry_url,
        settings,
        max_articles=max_articles,
        headless=not visible,
        use_llm=use_llm,
    )
    try:
        push_event("正在启动新闻抓取任务……", "starting")
        if use_llm:
            answer, result = run_agent(
                config,
                settings=settings,
                on_event=push_event,
                intervention_event=intervention_event,
            )
        else:
            result = run_deterministic(
                config,
                settings=settings,
                on_event=push_event,
                intervention_event=intervention_event,
            )
            answer = ""
        output_files = result.output_files or {}
        report_text = ""
        report_path = output_files.get("report")
        if report_path and Path(report_path).exists():
            report_text = Path(report_path).read_text(encoding="utf-8")
        elif answer:
            report_text = answer
        articles = [
            {
                "title": article.title,
                "source": article.source,
                "published_at": article.published_at,
                "url": article.url,
            }
            for article in result.articles[:20]
        ]
        completeness = {}
        if result.articles:
            for field in ("title", "body", "published_at", "author", "source", "language"):
                hit = sum(1 for article in result.articles if getattr(article, field, ""))
                completeness[field] = round(100 * hit / len(result.articles))
        profile_used = result.profile_used or "通用策略（未使用站点配置）"
        chips = [
            f"站点：{result.domain}",
            f"文章：{len(result.articles)} 篇",
            f"列表发现：{result.discovery_method or '未知'}",
            f"站点配置：{profile_used}",
        ]
        chips.extend(
            f"{field} 完整率 {value}%"
            for field, value in completeness.items()
        )
        output_dir = str(Path(output_files["jsonl"]).parent) if output_files.get("jsonl") else ""
        with job_lock:
            job["report"] = report_text
            job["summary"] = {"chips": chips}
            job["files"] = describe_files(output_files)
            job["articles"] = articles
            job["output_dir"] = output_dir
            job["message"] = f"抓取完成，共 {len(result.articles)} 篇文章。"
            job["phase"] = "completed"
    except Exception as error:
        message = f"运行失败：{type(error).__name__}: {error}"
        push_event(message, "failed")
        with job_lock:
            job["message"] = message
            job["error"] = message
    finally:
        with job_lock:
            job["running"] = False
            job["waiting_intervention"] = False
            job["intervention_event"] = None


def serve_file(handler, name):
    with job_lock:
        output_dir = job.get("output_dir") or ""
    if not output_dir or not name:
        handler.send_error(404)
        return
    directory = Path(output_dir).resolve()
    target = (directory / name).resolve()
    if target.parent != directory or not target.is_file():
        handler.send_error(404)
        return
    suffix = target.suffix.lower()
    content_type = {
        ".json": "application/json; charset=utf-8",
        ".jsonl": "application/x-ndjson; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
    }.get(suffix, "application/octet-stream")
    payload = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
    handler.end_headers()
    handler.wfile.write(payload)


class Handler(BaseHTTPRequestHandler):
    def _json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/":
            payload = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parts.path == "/api/status":
            with job_lock:
                snapshot = {key: value for key, value in job.items() if key != "intervention_event"}
            return self._json(snapshot)
        if parts.path == "/api/config":
            llm = get_llm_config()
            return self._json({"llm_available": bool(llm.get("api_key")), "model": llm.get("model")})
        if parts.path == "/api/profiles":
            try:
                from news_agent.profiles import list_profiles
                return self._json({"profiles": list_profiles()})
            except Exception:
                return self._json({"profiles": []})
        if parts.path == "/api/download":
            name = (parse_qs(parts.query).get("name") or [""])[0]
            return serve_file(self, name)
        return self._json({"error": "Not found"}, 404)

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/start":
            load_local_env()
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "请求内容无效。"}, 400)
            try:
                entry_url = normalize_entry_url(str(data.get("url", "")))
            except ValueError as error:
                return self._json({"error": str(error)}, 400)
            try:
                limit = int(data.get("limit", 8))
            except (TypeError, ValueError):
                return self._json({"error": "抓取篇数必须是整数。"}, 400)
            if not 1 <= limit <= MAX_ARTICLES_LIMIT:
                return self._json({"error": f"抓取篇数需在 1 到 {MAX_ARTICLES_LIMIT} 之间。"}, 400)
            use_llm = bool(data.get("use_llm", True))
            visible = bool(data.get("visible", False))
            if use_llm and not get_llm_config().get("api_key"):
                return self._json(
                    {"error": "未检测到 DEEPSEEK_API_KEY：请取消勾选“使用 LLM 分析”，或先在 .env 配置 Key。"},
                    400,
                )
            with job_lock:
                if job["running"]:
                    return self._json({"error": "当前已有抓取任务在运行。"}, 409)
                intervention_event = threading.Event()
                job.update({
                    "running": True,
                    "phase": "starting",
                    "message": "任务已启动。",
                    "logs": [],
                    "waiting_intervention": False,
                    "intervention_event": intervention_event,
                    "report": "",
                    "summary": None,
                    "files": [],
                    "articles": [],
                    "error": "",
                    "output_dir": "",
                })
            threading.Thread(
                target=run_job,
                args=(entry_url, limit, use_llm, visible, intervention_event),
                daemon=True,
            ).start()
            return self._json({"started": True})

        if parts.path == "/api/intervention":
            with job_lock:
                event = job.get("intervention_event")
                if event:
                    event.set()
                    job["waiting_intervention"] = False
            return self._json({"continued": bool(event)})

        return self._json({"error": "Not found"}, 404)

    def log_message(self, _format, *_args):
        pass


def main():
    load_local_env()
    llm = get_llm_config()
    if not llm.get("api_key"):
        print("提示：未配置 DEEPSEEK_API_KEY，网页默认使用确定性流水线（可取消勾选 LLM 模式）。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"新闻抓取 Agent 页面：http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止本地服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
