"""小红书调研 Agent 的轻量本地网页。"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openai import OpenAI
from playwright.sync_api import sync_playwright

from RedBookFinal import MAX_NOTES, load_local_env, run_agent


HOST = "127.0.0.1"
PORT = 8765
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>小红书调研 Agent</title>
  <style>
    :root { color-scheme: light; --ink:#20221f; --muted:#74776f; --line:#e9e9e4; --green:#334f43; --paper:#f7f7f3; --white:#fff; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--paper); color:var(--ink); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { width:min(760px,calc(100% - 32px)); margin:54px auto 70px; }
    .eyebrow { color:var(--green); font-size:12px; letter-spacing:.12em; font-weight:700; text-transform:uppercase; }
    h1 { margin:8px 0 5px; font-size:30px; letter-spacing:-.04em; }
    .intro { margin:0 0 28px; color:var(--muted); }
    .card { background:var(--white); border:1px solid var(--line); border-radius:14px; padding:24px; box-shadow:0 5px 20px #25291e08; }
    label { display:block; margin-bottom:8px; font-size:13px; font-weight:650; }
    textarea, input { width:100%; border:1px solid #dcded6; border-radius:8px; padding:12px 13px; background:#fff; color:var(--ink); font:inherit; outline:none; }
    textarea { min-height:90px; resize:vertical; }
    textarea:focus,input:focus { border-color:#718878; box-shadow:0 0 0 3px #334f4312; }
    .row { display:flex; align-items:end; gap:14px; margin-top:16px; }
    .count { width:145px; }
    button { border:0; border-radius:8px; padding:11px 17px; background:var(--green); color:#fff; font:inherit; font-weight:650; cursor:pointer; }
    button:disabled { opacity:.5; cursor:wait; }
    button.secondary { background:#ecefe9; color:var(--green); }
    .hint { color:var(--muted); font-size:12px; margin:13px 0 0; }
    .section { margin-top:18px; }
    .statusline { display:flex; justify-content:space-between; gap:12px; align-items:center; }
    #status { font-weight:650; }
    #phase { color:var(--muted); font-size:12px; }
    .logs { margin-top:12px; max-height:190px; overflow:auto; border-radius:8px; background:#f7f8f5; padding:12px 14px; color:#55594f; font-size:13px; white-space:pre-wrap; }
    .actions { margin-top:12px; display:flex; gap:9px; }
    .report { margin-top:20px; }
    .report h2 { margin:0 0 10px; font-size:17px; }
    #report { white-space:pre-wrap; overflow-wrap:anywhere; max-height:520px; overflow:auto; padding:18px; border:1px solid var(--line); border-radius:9px; background:#fff; font:13px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace; }
    .files { color:var(--muted); font-size:12px; margin-top:10px; overflow-wrap:anywhere; }
    [hidden] { display:none !important; }
    @media(max-width:520px) { main{margin-top:30px}.card{padding:18px}.row{align-items:stretch;flex-direction:column}.count{width:100%}.row button{width:100%}h1{font-size:26px} }
  </style>
</head>
<body>
  <main>
    <div class="eyebrow">Research assistant</div>
    <h1>小红书调研 Agent</h1>
    <p class="intro">描述你想了解的问题。Agent 会规划搜索、采集笔记、检查结果并整理报告。</p>
    <section class="card">
      <form id="form">
        <label for="goal">调研主题</label>
        <textarea id="goal" required placeholder="例如：大学生如何用 AI 辅助学习？"></textarea>
        <div class="row">
          <div class="count">
            <label for="target">最多采集笔记</label>
            <input id="target" type="number" value="8" min="1" max="20" required>
          </div>
          <button id="start" type="submit">开始调研</button>
        </div>
        <p class="hint">API key 从本地 .env 文件读取。首次搜索时会打开 Chrome，请登录小红书。</p>
      </form>
      <div class="section" id="progress" hidden>
        <div class="statusline"><span id="status">等待开始</span><span id="phase"></span></div>
        <div class="logs" id="logs"></div>
        <div class="actions" id="loginActions" hidden>
          <button class="secondary" id="loginDone" type="button">我已登录，继续</button>
        </div>
      </div>
      <div class="report" id="reportSection" hidden>
        <h2>调研报告</h2>
        <div id="report"></div>
        <div class="files" id="files"></div>
      </div>
    </section>
  </main>
  <script>
    const form=document.querySelector('#form'), startButton=document.querySelector('#start');
    const progress=document.querySelector('#progress'), statusEl=document.querySelector('#status');
    const phaseEl=document.querySelector('#phase'), logsEl=document.querySelector('#logs');
    const loginActions=document.querySelector('#loginActions'), reportSection=document.querySelector('#reportSection');
    let poller=null, previousLogs=0;
    form.addEventListener('submit',async e=>{
      e.preventDefault();
      startButton.disabled=true; progress.hidden=false; reportSection.hidden=true; previousLogs=0;
      logsEl.textContent=''; statusEl.textContent='正在启动…';
      try {
        const res=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal:document.querySelector('#goal').value,target:Number(document.querySelector('#target').value)})});
        const data=await res.json(); if(!res.ok) throw new Error(data.error||'启动失败');
        if(poller) clearInterval(poller); poller=setInterval(refresh,1000); refresh();
      } catch(err) { statusEl.textContent=err.message; startButton.disabled=false; }
    });
    document.querySelector('#loginDone').addEventListener('click',async()=>{
      await fetch('/api/login-done',{method:'POST'}); loginActions.hidden=true;
    });
    async function refresh(){
      try {
        const res=await fetch('/api/status'); const data=await res.json();
        statusEl.textContent=data.message||'运行中'; phaseEl.textContent=data.phase||'';
        if(data.logs.length>previousLogs){ logsEl.textContent=data.logs.join('\n'); logsEl.scrollTop=logsEl.scrollHeight; previousLogs=data.logs.length; }
        loginActions.hidden=!data.waiting_login;
        if(data.report){ reportSection.hidden=false; document.querySelector('#report').textContent=data.report; }
        if(data.files){ document.querySelector('#files').textContent=`文件已保存：${data.files.report}  ·  ${data.files.csv}  ·  ${data.files.loop}  ·  ${data.files.loop_svg}`; }
        if(!data.running){ startButton.disabled=false; if(poller){clearInterval(poller);poller=null;} }
      } catch(err) { statusEl.textContent='无法连接到本地服务'; }
    }
  </script>
</body>
</html>"""


job_lock = threading.Lock()
job = {
    "running": False,
    "phase": "",
    "message": "填写主题后开始调研。",
    "logs": [],
    "waiting_login": False,
    "login_event": None,
    "report": "",
    "files": None,
}


def push_event(message, phase=None):
    with job_lock:
        job["logs"].append(message)
        job["message"] = message
        if phase:
            job["phase"] = phase
            job["waiting_login"] = phase == "waiting_login"


def run_job(goal, target, login_event):
    try:
        client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
        push_event("正在打开 Chrome…", "starting")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
            page = browser.new_page()
            try:
                answer, result = run_agent(
                    client, goal, target, page,
                    on_event=push_event,
                    login_event=login_event,
                )
            finally:
                browser.close()
        report_path = result.get("report_path")
        report_text = Path(report_path).read_text(encoding="utf-8") if report_path else answer
        with job_lock:
            job["report"] = report_text
            job["files"] = {
                "report": Path(result["report_path"]).name,
                "csv": Path(result["csv_path"]).name,
                "loop": Path(result["diagram_path"]).name,
                "loop_svg": Path(result["diagram_svg_path"]).name,
            } if result.get("report_path") and result.get("csv_path") and result.get("diagram_path") and result.get("diagram_svg_path") else None
            job["message"] = f"调研完成，共采集 {len(result['notes'])} 篇笔记。"
            job["phase"] = "completed"
    except Exception as error:
        push_event(f"运行失败：{type(error).__name__}: {error}", "failed")
        with job_lock:
            job["message"] = f"运行失败：{error}"
    finally:
        with job_lock:
            job["running"] = False
            job["waiting_login"] = False
            job["login_event"] = None


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
        if self.path == "/":
            payload = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/status":
            with job_lock:
                snapshot = {key: value for key, value in job.items() if key != "login_event"}
            return self._json(snapshot)
        return self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/api/start":
            load_local_env()
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
                goal = str(data.get("goal", "")).strip()
                target = int(data.get("target", 8))
            except (ValueError, TypeError, json.JSONDecodeError):
                return self._json({"error": "请求内容无效。"}, 400)
            if not os.environ.get("DEEPSEEK_API_KEY"):
                return self._json({"error": "请先将轮换后的 key 填入项目目录的 .env 文件。"}, 400)
            if not goal:
                return self._json({"error": "请填写调研主题。"}, 400)
            if not 1 <= target <= MAX_NOTES:
                return self._json({"error": f"采集数量需在 1 到 {MAX_NOTES} 之间。"}, 400)
            with job_lock:
                if job["running"]:
                    return self._json({"error": "当前已有调研任务在运行。"}, 409)
                login_event = threading.Event()
                job.update({
                    "running": True, "phase": "starting", "message": "任务已启动。",
                    "logs": [], "waiting_login": False, "login_event": login_event,
                    "report": "", "files": None,
                })
            threading.Thread(target=run_job, args=(goal, target, login_event), daemon=True).start()
            return self._json({"started": True})

        if self.path == "/api/login-done":
            with job_lock:
                event = job.get("login_event")
                if event:
                    event.set()
                    job["waiting_login"] = False
            return self._json({"continued": bool(event)})

        return self._json({"error": "Not found"}, 404)

    def log_message(self, _format, *_args):
        pass


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("提示：尚未配置 .env；网页可以打开，配置 key 后即可开始调研。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"小红书调研 Agent 页面：http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止本地服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
