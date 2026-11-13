# -*- coding: utf-8 -*-
"""Agent Sync Panel — local dashboard at http://127.0.0.1:19528

Pure stdlib. No external dependencies. No network calls beyond localhost.
Shows sync status across WB / DSH / Qoder, session table, one-click sync.

Usage:
  python panel.py          # start panel, open browser
  python panel.py --port 19528
"""
import http.server, json, os, sys, io, time, threading, subprocess, webbrowser
import sqlite3, urllib.parse
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(BASE, "agentctxsync", "mcp")
sys.path.insert(0, REPO)

from adapters.workbuddy import WorkBuddyAdapter
from adapters.dsh import DshAdapter

QODER_DB = os.path.join(os.environ.get("APPDATA", ""), "com.qodercn.app.stable", "main.sqlite")
STATE_FILE = os.path.join(BASE, "sync_state.json")
ID_MAP_FILE = os.path.join(BASE, "qoder_id_map.json")
PORT = 19528

# --- Data layer ---

def bare_id(sid):
    return sid.removeprefix("session-")

def read_qoder():
    if not os.path.isfile(QODER_DB):
        return []
    con = sqlite3.connect(f"file:{QODER_DB}?mode=ro", uri=True, timeout=10)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT session_id, title, cwd, created_at, updated_at "
        "FROM chat_sessions WHERE deleted_at IS NULL AND archived=0").fetchall()
    sessions = []
    cur2 = con.cursor()
    for sid, title, cwd, created, updated in rows:
        cnt = cur2.execute("SELECT count(*) FROM chat_session_messages WHERE session_id=?", (sid,)).fetchone()[0]
        sessions.append({"id": sid, "title": title or "", "cwd": cwd or "",
                         "msgs": cnt, "updated": (updated or 0) / 1000.0})
    con.close()
    return sessions

def gather():
    AUTO_PAT = ("每日自动签到", "每日岗位巡检", "自动签到", "岗位巡检")
    def is_auto(s):
        return any(p in (s.get("title") or "") for p in AUTO_PAT)
    wb = [s for s in WorkBuddyAdapter().read_sessions() if not is_auto(s)]
    ds = [s for s in DshAdapter().read_sessions() if not is_auto(s)]
    qo = [s for s in read_qoder() if not is_auto(s)]
    # Load ID map
    qoder_to_wb = {}
    if os.path.isfile(ID_MAP_FILE):
        m = json.load(open(ID_MAP_FILE, encoding="utf-8"))
        qoder_to_wb = {v: k for k, v in m.items()}
    # Normalize
    wb_map = {bare_id(s["id"]): s for s in wb}
    ds_map = {bare_id(s["id"]): s for s in ds}
    qo_map = {}
    for s in qo:
        canonical = bare_id(qoder_to_wb.get(s["id"], s["id"]))
        qo_map[canonical] = s
    all_ids = sorted(set(wb_map) | set(ds_map) | set(qo_map),
                     key=lambda i: (wb_map.get(i) or ds_map.get(i) or qo_map.get(i, {})).get("last_activity_at")
                                   or (wb_map.get(i) or ds_map.get(i) or qo_map.get(i, {})).get("updated") or 0,
                     reverse=True)
    table = []
    for i in all_ids:
        s = wb_map.get(i) or ds_map.get(i) or qo_map.get(i, {})
        title = s.get("title", "")
        table.append({
            "id": i, "title": title,
            "msgs": len(s.get("messages") or []) if "messages" in s else s.get("msgs", 0),
            "wb": i in wb_map, "dsh": i in ds_map, "qoder": i in qo_map,
            "updated": s.get("last_activity_at") or s.get("updated") or 0,
        })
    state = {}
    if os.path.isfile(STATE_FILE):
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    return {
        "counts": {"wb": len(wb), "dsh": len(ds), "qoder": len(qo), "union": len(all_ids)},
        "state": state,
        "table": table,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# --- HTML ---

HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Agent Sync Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f1117;color:#e1e4e8;padding:24px}
h1{font-size:1.4rem;margin-bottom:16px;color:#58a6ff}
.cards{display:flex;gap:16px;margin-bottom:20px;flex-wrap:wrap}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 24px;min-width:140px}
.card .num{font-size:2rem;font-weight:700;color:#58a6ff}
.card .label{font-size:.8rem;color:#8b949e;margin-top:4px}
.card.ok .num{color:#3fb950}
.meta{font-size:.8rem;color:#8b949e;margin-bottom:16px}
button{background:#238636;color:#fff;border:none;padding:8px 20px;border-radius:6px;cursor:pointer;font-size:.9rem}
button:hover{background:#2ea043}
button:disabled{background:#21262d;color:#484f58;cursor:wait}
table{width:100%;border-collapse:collapse;font-size:.82rem;margin-top:16px}
th{text-align:left;padding:8px;border-bottom:2px solid #30363d;color:#8b949e;position:sticky;top:0;background:#0f1117}
td{padding:6px 8px;border-bottom:1px solid #21262d}
tr:hover td{background:#161b22}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%}
.dot.yes{background:#3fb950}.dot.no{background:#484f58}
.title-cell{max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#status{margin-left:12px;font-size:.8rem;color:#8b949e}
.scroll{max-height:60vh;overflow-y:auto;border:1px solid #30363d;border-radius:8px}
</style></head><body>
<h1>Agent Sync Panel</h1>
<div class="cards" id="cards"></div>
<div class="meta" id="meta"></div>
<button id="syncBtn" onclick="doSync()">立即同步</button>
<span id="status"></span>
<div class="scroll"><table><thead><tr>
<th>会话标题</th><th>消息数</th><th>WB</th><th>DSH</th><th>Qoder</th><th>最后活动</th>
</tr></thead><tbody id="tbody"></tbody></table></div>
<script>
let DATA=null;
async function load(){
  const r=await fetch('/api/data');DATA=await r.json();render();
}
function render(){
  const c=DATA.counts;
  const rows=DATA.table;
  const allSync=rows.length>0&&rows.every(r=>r.wb&&r.dsh&&r.qoder);
  document.getElementById('cards').innerHTML=
    card(c.wb,'WorkBuddy',allSync)+card(c.dsh,'DSH',allSync)+card(c.qoder,'Qoder',allSync)+card(c.union,'总计',allSync);
  const st=DATA.state||{};
  document.getElementById('meta').textContent=
    '上次同步: '+(st.last_run||'从未')+' | 刷新: '+DATA.time+(allSync?' | 三方已齐':' | 存在差异');
  const tb=document.getElementById('tbody');
  tb.innerHTML=rows.map(r=>'<tr><td class="title-cell" title="'+esc(r.id)+'">'+esc(r.title||'(无标题)')+'</td><td>'+r.msgs+'</td>'+
    dot(r.wb)+dot(r.dsh)+dot(r.qoder)+'<td>'+fmtTime(r.updated)+'</td></tr>').join('');
}
function card(n,label,ok){return '<div class="card'+(ok?' ok':'')+'"><div class="num">'+n+'</div><div class="label">'+label+'</div></div>';}
function dot(v){return '<td><span class="dot '+(v?'yes':'no')+'"></span></td>';}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function fmtTime(t){if(!t)return'-';const d=new Date(t*1000);return d.toLocaleDateString('zh-CN')+' '+d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});}
async function doSync(){
  const btn=document.getElementById('syncBtn');btn.disabled=true;
  document.getElementById('status').textContent='同步中...';
  const r=await fetch('/api/sync',{method:'POST'});const j=await r.json();
  document.getElementById('status').textContent=j.ok?'同步完成':'失败: '+(j.error||'');
  btn.disabled=false;await load();
}
load();setInterval(load,30000);
</script></body></html>"""

# --- Server ---

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
        elif self.path == "/api/data":
            data = gather()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/sync":
            try:
                r = subprocess.run(
                    [sys.executable, os.path.join(BASE, "sync.py"), "--apply"],
                    capture_output=True, text=True, timeout=300,
                    encoding="utf-8", errors="replace")
                ok = r.returncode == 0
                result = {"ok": ok, "output": r.stdout[-500:] if r.stdout else "",
                          "error": r.stderr[-300:] if not ok and r.stderr else ""}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


def main():
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Agent Sync Panel running at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
