# -*- coding: utf-8 -*-
"""Agent Sync Panel — local dashboard at http://127.0.0.1:19528

Pure stdlib. No external dependencies. No network calls beyond localhost.

The metric is CONTENT consistency, not "the session id exists": for every
conversation it shows how many messages each store is still missing (the same
guarded tails sync.py would write), so a green card really means "all three
stores hold the same conversation".

Usage:
  python panel.py                 # start panel, open browser
  python panel.py --port 19528
"""
import http.server, json, os, sys, io, time, threading, subprocess, webbrowser
from datetime import datetime

# same guard as sync.py: stdout is None under a hidden pythonw.exe launch
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
else:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # idempotent: safe to import
    except AttributeError:                                            # frozen / replaced streams
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        except AttributeError:                                        # stream without a .buffer
            pass
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import sync as S          # the sync engine owns reading, id mapping, tail rules

PORT = 19528
CACHE_TTL = 20             # seconds; one gather reads all three stores (~5s)
NAMES = ("WB", "DSH", "Qoder")


class Cache:
    """gather() is slow, so the 30s auto-refresh, the sync button and a manual
    refresh share one result instead of each re-reading three stores."""

    def __init__(self):
        self.data = None
        self.stamp = 0
        self.lock = threading.Lock()

    def get(self, force=False):
        with self.lock:
            if force or self.data is None or time.time() - self.stamp > CACHE_TTL:
                try:
                    self.data = gather()
                    self.stamp = time.time()
                except Exception as e:
                    if self.data is None:
                        self.data = {"error": repr(e)}
                    else:
                        self.data["error"] = repr(e)
            return self.data


CACHE = Cache()


def gather():
    """Read the three stores through sync.py and report per-session tails."""
    ctx = S.build_stores(verbose=False)
    stores = ctx["stores"]
    imports, tails, all_ids = S.compute_plan(stores)

    # Which apps hold their own store open. While an app runs, sync.py will not
    # write into it (the watcher retries on exit), so the card says "waiting"
    # instead of blaming the user for drift nothing can repair right now.
    running = {"WB": S.process_alive("WorkBuddy.exe"),
               "DSH": S.process_alive("DeepSeek Harness.exe"),
               "Qoder": S.process_alive(S.QODER_EXE)}
    pending = {n: {t[0]: len(t[2]) for t in tails[n]} for n in NAMES}
    absent = {n: {S.bare_id(t["id"]) for t in imports[n]} for n in NAMES}

    def newest(i):
        stamp, best = 0, None
        for n in NAMES:
            s = stores[n].get(i)
            if not s:
                continue
            v = s.get("last_activity_at") or 0
            if v > stamp:
                stamp, best = v, s
        return best, stamp

    table = []
    for i in sorted(all_ids, key=lambda x: newest(x)[1], reverse=True):
        s, stamp = newest(i)
        counts = {n: len((stores[n].get(i) or {}).get("messages") or []) for n in NAMES}
        table.append({
            "id": i,
            "title": (s or {}).get("title") or "",
            "counts": counts,
            "present": {n: i in stores[n] for n in NAMES},
            "pending": {n: pending[n].get(i, 0) for n in NAMES},
            "absent": {n: i in absent[n] for n in NAMES},
            "updated": stamp,
        })

    drift = {n: sum(len(t[2]) for t in tails[n]) for n in NAMES}
    # Messages a store physically cannot hold (DSH has no system rows). Not
    # drift -- no sync pass will ever repair it -- so it is shown as its own
    # number instead of being hidden by the filter that keeps drift at 0.
    unhold = {n: sum(len(ms) for _, _, ms in S.unholdable(stores, n)) for n in NAMES}
    # Drift is reported even for a store whose app is open: it is real, the
    # panel just colours it "waiting" because sync cannot repair it meanwhile.
    return {
        "counts": {n: len(stores[n]) for n in NAMES},
        "union": len(all_ids),
        "drift": drift,
        "unhold": unhold,
        "absent": {n: len(absent[n]) for n in NAMES},
        "running": running,
        "table": table,
        "state": S.load_state(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --- HTML ---

HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Agent Sync Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f1117;color:#e1e4e8;padding:24px}
h1{font-size:1.4rem;margin-bottom:6px;color:#58a6ff}
.cards{display:flex;gap:16px;margin:16px 0;flex-wrap:wrap}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 22px;min-width:172px}
.card .num{font-size:1.7rem;font-weight:700;color:#58a6ff}
.card .label{font-size:.8rem;color:#8b949e;margin-top:2px}
.card .sub{font-size:.76rem;margin-top:6px;color:#8b949e}
.card.ok .num{color:#3fb950}.card.ok .sub{color:#3fb950}
.card.bad .num{color:#f85149}.card.bad .sub{color:#f85149}
.card.wait .num{color:#d29922}.card.wait .sub{color:#d29922}
.meta{font-size:.8rem;color:#8b949e;margin-bottom:14px;line-height:1.7}
button{background:#238636;color:#fff;border:none;padding:8px 20px;border-radius:6px;cursor:pointer;font-size:.9rem}
button:hover{background:#2ea043}
button:disabled{background:#21262d;color:#484f58;cursor:wait}
table{width:100%;border-collapse:collapse;font-size:.82rem;margin-top:14px}
th{text-align:left;padding:8px;border-bottom:2px solid #30363d;color:#8b949e;position:sticky;top:0;background:#0f1117}
td{padding:6px 8px;border-bottom:1px solid #21262d}
tr:hover td{background:#161b22}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%}
.dot.full{background:#3fb950}.dot.part{background:#d29922}.dot.none{background:#484f58}
.nc{font-variant-numeric:tabular-nums;color:#8b949e}
.warn{color:#d29922}.err{color:#f85149}.good{color:#3fb950}.dim{color:#8b949e}
.tc{max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#status{margin-left:12px;font-size:.8rem;color:#8b949e}
.scroll{max-height:62vh;overflow-y:auto;border:1px solid #30363d;border-radius:8px}
</style></head><body>
<h1>Agent Sync Panel</h1>
<div class="meta" id="note"></div>
<div class="cards" id="cards"></div>
<div class="meta" id="meta"></div>
<button id="syncBtn" onclick="doSync()">立即同步</button>
<span id="status"></span>
<div class="scroll"><table><thead><tr>
<th>会话</th><th>消息数 W/D/Q</th><th>WB</th><th>DSH</th><th>Qoder</th><th>待补</th><th>最后活动</th>
</tr></thead><tbody id="tbody"></tbody></table></div>
<script>
let DATA=null;
async function load(){const r=await fetch('/api/data');DATA=await r.json();render();}
function card(n,label){
  const miss=DATA.absent[n]||0, pend=DATA.drift[n]||0, open=DATA.running[n];
  let cls='ok', sub='内容一致';
  if(miss||pend){
    if(open){cls='wait';sub='待补 '+pend+' 条 / 缺 '+miss+' 会话 —— '+label+' 开着，关闭后自动写入';}
    else {cls='bad';sub='缺 '+miss+' 会话 / 待补 '+pend+' 条（现在可写）';}
  }
  return '<div class="card '+cls+'"><div class="num">'+(DATA.counts[n]||0)+'</div>'
       +'<div class="label">'+label+'</div><div class="sub">'+sub+'</div></div>';
}
function render(){
  const note=document.getElementById('note');
  if(DATA.error){note.innerHTML='<span class="err">读取失败：'+esc(DATA.error)+'</span>';return;}
  const open=Object.keys(DATA.running).filter(n=>DATA.running[n]);
  note.innerHTML='口径：按内容比对（不只看会话在不在）。'
    +(open.length?'<span class="warn"> 当前开着：'+open.join(' / ')
      +' —— 该端只读，关闭后由 watcher 自动补</span>':'');
  document.getElementById('cards').innerHTML=
    card('WB','WorkBuddy')+card('DSH','DSH')+card('Qoder','Qoder')
    +'<div class="card"><div class="num">'+DATA.union+'</div><div class="label">会话总计</div>'
    +'<div class="sub">人工会话（自动化任务已排除）</div></div>';
  const tot=DATA.drift.WB+DATA.drift.DSH+DATA.drift.Qoder;
  const held=['WB','DSH','Qoder'].filter(n=>DATA.running[n])
    .reduce((a,n)=>a+DATA.drift[n]+(DATA.absent[n]||0)*0,0);
  const unhold=['WB','DSH','Qoder'].reduce((a,n)=>a+((DATA.unhold||{})[n]||0),0);
  document.getElementById('meta').innerHTML=
    '上次同步：'+esc((DATA.state&&DATA.state.last_run)||'从未')+' ｜ 刷新：'+DATA.time
    +' ｜ '+(tot?'<span class="warn">内容有差异：待补 '+tot+' 条</span>'
             :'<span class="good">三端内容一致</span>')
    +(held?'<span class="warn"> ｜ 其中 '+held+' 条所在的应用正开着，关闭后由 watcher 自动补</span>':'')
    +(unhold?'<span class="dim"> ｜ 另有 '+unhold+' 条该端格式装不下（DSH 无 system 角色），'
             +'不计入差异，只保留在能承载的端</span>':'');
  document.getElementById('tbody').innerHTML=DATA.table.map(r=>{
    const cell=(n)=>'<td><span class="dot '+(r.present[n]?(r.pending[n]?'part':'full'):'none')
      +'" title="'+r.counts[n]+' 条 ｜ '
      +(r.present[n]?(r.pending[n]?'待补 '+r.pending[n]+' 条':'内容一致')
        :(r.absent[n]?'本次同步会新建':'这条会话不在该端'))+'"></span></td>';
    const pend=r.pending.WB+r.pending.DSH+r.pending.Qoder;
    return '<tr><td class="tc" title="'+esc(r.id)+'">'+esc(r.title||'(无标题)')+'</td>'
      +'<td class="nc">'+r.counts.WB+' / '+r.counts.DSH+' / '+r.counts.Qoder+'</td>'
      +cell('WB')+cell('DSH')+cell('Qoder')
      +'<td class="nc">'+(pend||'')+'</td><td class="nc">'+fmtTime(r.updated)+'</td></tr>';
  }).join('');
}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function fmtTime(t){if(!t)return'-';const d=new Date(t*1000);
  return d.toLocaleDateString('zh-CN')+' '+d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});}
async function doSync(){
  const btn=document.getElementById('syncBtn');btn.disabled=true;
  document.getElementById('status').textContent='同步中...';
  const r=await fetch('/api/sync',{method:'POST'});const j=await r.json();
  document.getElementById('status').textContent=j.ok?('完成：'+(j.summary||''))
    :('失败：'+(j.error||'见输出'));
  btn.disabled=false;await load();
}
load();setInterval(load,30000);
</script></body></html>"""


# --- Server ---

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
        elif self.path == "/api/data":
            self._json(CACHE.get())
        elif self.path == "/api/refresh":
            self._json(CACHE.get(force=True))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/sync":
            self.send_error(404)
            return
        try:
            r = subprocess.run([sys.executable, os.path.join(BASE, "sync.py"), "--apply"],
                               capture_output=True, text=True, timeout=900,
                               encoding="utf-8", errors="replace",
                               creationflags=S.NO_WINDOW)
            out = (r.stdout or "").strip().splitlines()
            marks = ("  WB ", "  DSH ", "  Qoder ", "!!", "Qoder is OPEN", "Plan:", "Done.")
            summary = " | ".join(l.strip() for l in out if l.startswith(marks))
            self._json({"ok": r.returncode == 0,
                        "summary": (summary[-300:] or "无改动"),
                        "output": "\n".join(out)[-1200:],
                        "error": (r.stderr or "")[-300:]})
            CACHE.get(force=True)
        except Exception as e:
            self._json({"ok": False, "error": repr(e)})


def main():
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Agent Sync Panel running at {url}")
    print("Metric: content consistency (guarded tails), not just session presence.")
    if "--no-browser" not in sys.argv:      # headless start for tests / autostart
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
