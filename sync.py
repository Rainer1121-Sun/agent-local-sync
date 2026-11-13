# -*- coding: utf-8 -*-
"""Three-way conversation sync: WB <-> DSH <-> Qoder (local-only, no server).

Usage:
  python sync.py                  # dry-run: show plan
  python sync.py --apply          # execute full three-way sync
  python sync.py --apply --limit 1  # only first N sessions per direction

Safety:
  - WB/DSH must be CLOSED for writes (process check)
  - Qoder can be RUNNING (WAL mode, busy_timeout=30s)
  - Backs up before write; never deletes; only adds
  - WB is authoritative: on id conflict, WB version wins
"""
import argparse, io, json, os, sys, time, shutil, datetime, subprocess
import sqlite3, uuid, hashlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(BASE, "agentctxsync", "mcp")   # adapters from the agentctxsync repo
sys.path.insert(0, REPO)
from adapters.workbuddy import WorkBuddyAdapter
from adapters.dsh import DshAdapter

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_state.json")
BACKUP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
QODER_DB = os.path.join(os.environ.get("APPDATA", ""), "com.qodercn.app.stable", "main.sqlite")


def process_alive(name):
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            stderr=subprocess.DEVNULL, timeout=10
        ).decode("gbk", errors="replace")
        return name.lower() in out.lower()
    except Exception:
        return False


def load_state():
    if os.path.isfile(STATE_FILE):
        return json.load(open(STATE_FILE, encoding="utf-8"))
    return {"last_run": None, "counts": {}}


def save_state(state):
    state["last_run"] = datetime.datetime.now().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fold_tools(s):
    out, seen = [], set()
    for m in s.get("messages") or []:
        if m.get("role") == "tool":
            nm = m.get("tool_name") or "tool"
            m2 = {k: v for k, v in m.items() if k not in ("tool_call_id", "tool_name")}
            m2["role"] = "assistant"
            m2["content"] = f"[Tool: {nm}]\n\n{m.get('content') or ''}"
            key = (m2["role"], m2.get("timestamp"))
            while key in seen:
                m2["timestamp"] = float(m2["timestamp"] or 0) + 0.001
                key = (m2["role"], m2["timestamp"])
            seen.add(key)
            out.append(m2)
        else:
            seen.add((m.get("role"), m.get("timestamp")))
            out.append(m)
    s2 = dict(s)
    s2["messages"] = out
    s2["message_count"] = len(out)
    return s2


def bare_id(sid):
    return sid.removeprefix("session-")


def read_qoder_sessions():
    if not os.path.isfile(QODER_DB):
        return []
    con = sqlite3.connect(f"file:{QODER_DB}?mode=ro", uri=True, timeout=10)
    cur = con.cursor()
    sessions = []
    rows = cur.execute(
        "SELECT session_id, title, cwd, created_at, updated_at "
        "FROM chat_sessions WHERE deleted_at IS NULL AND archived=0"
    ).fetchall()
    cur2 = con.cursor()
    for row in rows:
        sid, title, cwd, created, updated = row
        msgs = []
        for mrow in cur2.execute(
            "SELECT message_id, role, payload_json, created_at, sequence "
            "FROM chat_session_messages WHERE session_id=? ORDER BY sequence", (sid,)
        ):
            mid, role, payload, ts, seq = mrow
            text = ""
            try:
                parts = json.loads(payload).get("parts", [])
                text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
            except Exception:
                text = payload or ""
            if text:
                msgs.append({"role": role or "assistant", "content": text,
                             "timestamp": (ts or 0) / 1000.0})
        sessions.append({
            "id": sid, "title": title or "", "cwd": cwd or "",
            "started_at": (created or 0) / 1000.0,
            "last_activity_at": (updated or 0) / 1000.0,
            "messages": msgs, "message_count": len(msgs), "_source": "qoder",
        })
    con.close()
    return sessions


def write_qoder_sessions(sessions, backup_dir):
    if not sessions:
        return {"imported": 0}
    bk = os.path.join(backup_dir, "qoder-main.sqlite")
    os.makedirs(os.path.dirname(bk), exist_ok=True)
    shutil.copy2(QODER_DB, bk)
    con = sqlite3.connect(QODER_DB, timeout=30)
    cur = con.cursor()
    imported = 0
    try:
        cur.execute("BEGIN IMMEDIATE")
        for s in sessions:
            sid = s["id"]
            if list(cur.execute("SELECT 1 FROM chat_sessions WHERE session_id=?", (sid,))):
                continue
            created_ms = int((s.get("started_at") or time.time()) * 1000)
            updated_ms = int((s.get("last_activity_at") or time.time()) * 1000)
            cwd = s.get("cwd") or ""
            ws = list(cur.execute(
                "SELECT workspace_id FROM workspaces WHERE root_paths_json LIKE ? LIMIT 1",
                (f"%{os.path.basename(cwd)}%",))) if cwd else []
            workspace_id = ws[0][0] if ws else None
            extra = json.dumps({"source": "workspace", "importedFrom": "workbuddy-sync",
                                "syncOrigin": s.get("_source", "wb")}, ensure_ascii=False)
            exec_target = json.dumps({"mode": "local", "host": {"id": "local", "kind": "local", "label": "Local"}})
            cur.execute(
                "INSERT INTO chat_sessions (session_id, session_kind, conversation_mode, product_mode,"
                " title, cwd, execution_kind, workspace_id, model, permission_mode,"
                " additional_direcotries, extra_json, created_at, updated_at,"
                " execution_target_json, archived, unread, message_envelope_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "standard", "normal", "general", s.get("title") or "Synced session",
                 cwd, "local", workspace_id, "qfmodel", "bypassPermissions", "{}", extra,
                 created_ms, updated_ms, exec_target, 0, 0, 1))
            seq = 0
            for m in s.get("messages") or []:
                content = m.get("content") or ""
                if not content:
                    continue
                role = m.get("role", "assistant")
                if role == "tool":
                    role = "assistant"
                seq += 1
                mid = str(uuid.uuid4())
                ts = int((m.get("timestamp") or time.time()) * 1000)
                payload = json.dumps({"parts": [{"id": f"{mid}:text:0", "type": "text",
                                                 "text": content, "parentToolUseId": None}]},
                                     ensure_ascii=False)
                pb = len(payload.encode("utf-8"))
                ph = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                cur.execute(
                    "INSERT INTO chat_session_messages (session_id, message_id, turn_id, sequence,"
                    " payload_json, status, source, created_at, updated_at, role,"
                    " has_visible_content, payload_bytes, payload_hash, payload_schema_version, feedback_revision)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, mid, mid, seq, payload, "completed", "cli-import", ts, ts, role, 1, pb, ph, 2, 0))
            imported += 1
        con.commit()
    except Exception as e:
        con.rollback()
        con.close()
        return {"imported": 0, "error": str(e)}
    con.close()
    return {"imported": imported}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    if a.apply:
        if process_alive("DeepSeek Harness.exe"):
            print("ERROR: DSH is running. Close it first.")
            return
        if process_alive("WorkBuddy.exe"):
            print("ERROR: WB is running. Close it first.")
            return

    wb = WorkBuddyAdapter()
    ds = DshAdapter()
    print(f"WB home: {wb.discover()}")
    print(f"DSH home: {ds.discover()}")
    print(f"Qoder DB: {QODER_DB}")

    t0 = time.time()
    wb_sessions = wb.read_sessions()
    ds_sessions = ds.read_sessions()
    qo_sessions = read_qoder_sessions()
    print(f"Read: WB={len(wb_sessions)} DSH={len(ds_sessions)} Qoder={len(qo_sessions)} in {time.time()-t0:.1f}s")

    # Filter out automation sessions (only sync human conversations)
    AUTO_PAT = ("每日自动签到", "每日岗位巡检", "自动签到", "岗位巡检")
    def is_auto(s):
        t = s.get("title") or ""
        return any(p in t for p in AUTO_PAT)
    wb_sessions = [s for s in wb_sessions if not is_auto(s)]
    ds_sessions = [s for s in ds_sessions if not is_auto(s)]
    qo_sessions = [s for s in qo_sessions if not is_auto(s)]
    print(f"After filter (human only): WB={len(wb_sessions)} DSH={len(ds_sessions)} Qoder={len(qo_sessions)}")

    wb_map = {bare_id(s["id"]): s for s in wb_sessions}
    ds_map = {bare_id(s["id"]): s for s in ds_sessions}
    qo_map = {bare_id(s["id"]): s for s in qo_sessions}

    # Load WB->Qoder ID mapping from migration ledger (dedup imported sessions)
    id_map_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qoder_id_map.json")
    qoder_to_wb = {}
    if os.path.isfile(id_map_file):
        wb_to_qoder = json.load(open(id_map_file, encoding="utf-8"))
        qoder_to_wb = {v: k for k, v in wb_to_qoder.items()}
    # Re-key qo_map: if a Qoder session was imported from WB, use WB's id as key
    qo_map_normalized = {}
    for qid, s in qo_map.items():
        canonical = qoder_to_wb.get(qid, qid)
        qo_map_normalized[canonical] = s
    qo_map = qo_map_normalized

    all_ids = set(wb_map) | set(ds_map) | set(qo_map)

    to_dsh = [wb_map.get(i) or ds_map.get(i) or qo_map[i] for i in all_ids if i not in ds_map]
    to_wb = [wb_map.get(i) or ds_map.get(i) or qo_map[i] for i in all_ids if i not in wb_map]
    to_qo = [wb_map.get(i) or ds_map.get(i) or qo_map[i] for i in all_ids if i not in qo_map]

    for lst in (to_dsh, to_wb, to_qo):
        lst.sort(key=lambda s: s.get("last_activity_at") or 0, reverse=True)
    if a.limit:
        to_dsh, to_wb, to_qo = to_dsh[:a.limit], to_wb[:a.limit], to_qo[:a.limit]

    print(f"\nPlan: ->DSH {len(to_dsh)}, ->WB {len(to_wb)}, ->Qoder {len(to_qo)} (union={len(all_ids)})")
    for label, lst in [("DSH", to_dsh), ("WB", to_wb), ("Qoder", to_qo)]:
        for s in lst[:3]:
            print(f"  ->{label} {s['id'][:16]} msgs={len(s.get('messages') or [])} {s.get('title','')[:35]}")
        if len(lst) > 3:
            print(f"  ... +{len(lst)-3} more")

    if not a.apply:
        print("\n[DRY-RUN] No writes. Add --apply to execute.")
        return

    state = load_state()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, f"sync-{ts}")
    os.makedirs(bdir, exist_ok=True)

    if to_dsh:
        print(f"\n=== -> DSH: {len(to_dsh)} sessions ===")
        ds_root = ds.discover()
        if os.path.isdir(ds_root):
            shutil.copytree(ds_root, os.path.join(bdir, "dsh-sessions"), dirs_exist_ok=True)
        res = ds.write_sessions([fold_tools(s) for s in to_dsh])
        print(f"  {json.dumps(res, ensure_ascii=False)[:300]}")

    if to_wb:
        print(f"\n=== -> WB: {len(to_wb)} sessions ===")
        wb_home = wb.discover()
        bk = os.path.join(bdir, "wb-db")
        os.makedirs(bk, exist_ok=True)
        db_path = os.path.join(wb_home, "workbuddy.db")
        if os.path.isfile(db_path):
            shutil.copy2(db_path, os.path.join(bk, "workbuddy.db"))
        res = wb.write_sessions(to_wb)
        print(f"  {json.dumps(res, ensure_ascii=False)[:300]}")

    if to_qo:
        print(f"\n=== -> Qoder: {len(to_qo)} sessions ===")
        res = write_qoder_sessions(to_qo, bdir)
        print(f"  {json.dumps(res, ensure_ascii=False)[:300]}")

    state["counts"] = {"wb": len(wb_map), "dsh": len(ds_map), "qoder": len(qo_map), "union": len(all_ids)}
    save_state(state)
    print(f"\nDone. Backups: {bdir}")

    print("\n=== Verification ===")
    ds_after = DshAdapter().read_sessions()
    wb_after = WorkBuddyAdapter().read_sessions()
    qo_after = read_qoder_sessions()
    print(f"WB: {len(wb_sessions)}->{len(wb_after)}  DSH: {len(ds_sessions)}->{len(ds_after)}  Qoder: {len(qo_sessions)}->{len(qo_after)}")
    print(f"Target union: {len(all_ids)}")


if __name__ == "__main__":
    main()
