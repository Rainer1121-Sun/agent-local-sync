# -*- coding: utf-8 -*-
"""Three-way conversation sync: WB <-> DSH <-> Qoder (local-only, no server).

Usage:
  python sync.py                  # dry-run: show import plan + drift tails
  python sync.py --apply          # execute: import missing sessions, append tails
  python sync.py --apply --limit 1  # only first N sessions per direction
  python sync.py --apply --live-guard 60  # protect Qoder sessions active in 60 min

Two write semantics, both add-only:
  import  -- a session id missing from the target store is created whole
  tail    -- a session that already exists receives only the messages that are
             (a) strictly newer than the target's newest message AND
             (b) absent from the target by content key (role, sha1(text))
             This is what makes "kept talking in Qoder" reach WB/DSH, and it
             refuses to re-inject the native importer's lossy re-encodings.

Safety:
  - WB/DSH must be CLOSED for writes (process check)
  - Qoder can be RUNNING (WAL mode, busy_timeout=30s), but a session that was
    active inside the live-guard window is never appended into (its tail is
    reported and left for the watcher to apply after Qoder exits)
  - Backs up before write; never deletes; only adds
  - WB is authoritative: on id conflict, WB version wins
  - Qoder stores user/assistant text only; tool traces stay in WB/DSH
"""
import argparse, io, json, os, sys, time, shutil, datetime, subprocess
import sqlite3, uuid, hashlib

# stdout under pythonw.exe (hidden .vbs launch, no pipe) is None, which would
# make every print below raise; give it a sink instead.
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
REPO = os.path.join(BASE, "agentctxsync", "mcp")   # adapters from the agentctxsync repo
sys.path.insert(0, REPO)
from adapters.workbuddy import WorkBuddyAdapter
from adapters.dsh import DshAdapter

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_state.json")
BACKUP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
QODER_DB = os.path.join(os.environ.get("APPDATA", ""), "com.qodercn.app.stable", "main.sqlite")
# Sessions whose title matches are agent automation runs, not human
# conversations: never synced, never shown as drift.
AUTO_PAT = ("每日自动签到", "每日岗位巡检", "自动签到", "岗位巡检")

# Qoder's image name (tasklist spelling); the app is the only thing that can
# hold a session in memory, so its presence defers every Qoder write.
QODER_EXE = "Qoder CN.exe"

# When this file runs under pythonw.exe (the watcher launches sync.py that way),
# the process has no console, so Windows ALLOCATES a fresh one for every child
# it spawns -- and with Windows Terminal as the default terminal app that means
# a visible terminal window flashing open and closed every 15 s. CREATE_NO_WINDOW
# keeps the child console-less; stdout still comes back through the pipe, and the
# flag is simply absent (-> 0) on POSIX.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_auto(s):
    return any(p in (s.get("title") or "") for p in AUTO_PAT)


def process_alive(name):
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            stderr=subprocess.DEVNULL, timeout=10, creationflags=NO_WINDOW
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


def ts_of(m):
    try:
        return float(m.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


def ckey(m):
    """Content identity: role + normalised-text hash. Timestamp-independent,
    so the same turn re-encoded by a different store still matches."""
    t = m.get("content") or ""
    if not isinstance(t, str):
        t = str(t)
    return (m.get("role"), hashlib.sha1(t.strip().lower().encode("utf-8")).hexdigest()[:16])


def for_wb(s):
    """WB keeps the canonical shape (its adapter writes tool events natively)."""
    return s


def for_dsh(s):
    """DSH has no tool role: fold tool results into assistant turns."""
    return fold_tools(s)


def for_qoder(s):
    """Qoder shows user/assistant text bubbles; tool traces are dropped."""
    out = []
    for m in s.get("messages") or []:
        if m.get("role") == "tool":
            continue
        content = m.get("content") or ""
        if not str(content).strip():
            continue
        out.append({"role": m.get("role"), "content": content,
                    "timestamp": ts_of(m)})
    s2 = dict(s)
    s2["messages"] = out
    s2["message_count"] = len(out)
    return s2


TRANSFORMS = {"WB": for_wb, "DSH": for_dsh, "Qoder": for_qoder}

# Roles a target store physically cannot hold, i.e. its writer drops them:
# DshAdapter._merge_messages keeps user/assistant only, so a source's system
# rows can never land there (measured on the live store: 0 system rows in DSH,
# 32 in Qoder, and WB round-tripped one during the scratch append test).
# Skipping them at plan time is what lets drift reach 0 instead of re-planning
# the same message forever; print_plan reports the count so the loss stays
# visible rather than being hidden by the filter.
ROLES_LOST = {"DSH": {"system"}, "WB": set(), "Qoder": set()}


def unholdable(stores, tgt_name):
    """(session, [messages]) the target store cannot represent -- newest-first
    tail candidates only, i.e. the same window compute_tails looks at."""
    lost_roles = ROLES_LOST.get(tgt_name) or set()
    if not lost_roles:
        return []
    out = []
    for sid, tgt in stores[tgt_name].items():
        tmsgs = tgt.get("messages") or []
        if not tmsgs:
            continue
        tmax = max(ts_of(m) for m in tmsgs)
        have = {ckey(m) for m in tmsgs}
        cand, seen = [], set()
        for src_name, src_store in stores.items():
            if src_name == tgt_name:
                continue
            src = src_store.get(sid)
            if not src:
                continue
            for m in TRANSFORMS[tgt_name](src).get("messages") or []:
                if ts_of(m) <= tmax or m.get("role") not in lost_roles:
                    continue
                k = ckey(m)
                if k in have or k in seen:
                    continue
                seen.add(k)
                cand.append(m)
        if cand:
            out.append((sid, tgt, cand))
    return out


def index_by_id(sessions, mapping=None):
    """canonical id -> richest session copy (a store can hold two rows for one
    conversation after a re-import; keep the one with more messages)."""
    out = {}
    for s in sessions:
        sid = bare_id(s["id"])
        if mapping:
            sid = bare_id(mapping.get(sid) or mapping.get(s["id"]) or sid)
        if sid not in out or len(s.get("messages") or []) > len(out[sid].get("messages") or []):
            out[sid] = s
    return out


def compute_tails(stores):
    """stores: {name: {canonical_id: session}} -> {name: [(id, target, [msgs])]}.

    For every session that ALREADY EXISTS in a target store, collect the messages
    the other stores hold that are newer than the target's newest message and
    absent from it by content key. Returns nothing for missing sessions -- those
    go through the whole-session import path.
    """
    names = list(stores)
    plan = {n: [] for n in names}
    for tgt_name in names:
        tf = TRANSFORMS[tgt_name]
        lost = ROLES_LOST.get(tgt_name) or set()
        for sid, tgt in stores[tgt_name].items():
            tmsgs = tgt.get("messages") or []
            if not tmsgs:
                continue
            tmax = max(ts_of(m) for m in tmsgs)
            have = {ckey(m) for m in tmsgs}
            cand, seen = [], set()
            for src_name in names:
                if src_name == tgt_name:
                    continue
                src = stores[src_name].get(sid)
                if not src:
                    continue
                for m in tf(src).get("messages") or []:
                    if ts_of(m) <= tmax:
                        continue
                    if m.get("role") in lost:
                        continue        # target physically cannot hold it
                    k = ckey(m)
                    if k in have or k in seen:
                        continue
                    seen.add(k)
                    cand.append(m)
            if cand:
                cand.sort(key=ts_of)
                plan[tgt_name].append((sid, tgt, cand))
    for lst in plan.values():
        lst.sort(key=lambda t: ts_of(t[2][-1]), reverse=True)
    return plan


def tail_session(tgt, msgs):
    """Synthetic session that carries ONLY the new tail, under the target's own
    id / cwd / title, so the store's merge writer appends into the right record
    instead of re-writing history."""
    s = dict(tgt)
    s["messages"] = msgs
    s["message_count"] = len(msgs)
    s["last_activity_at"] = max(ts_of(m) for m in msgs)
    return s


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
                    continue      # Qoder has no tool trace: user/assistant text only
                if role not in ("user", "assistant"):
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


def id_variants(sid):
    """Qoder has stored session ids both bare and 'session-'-prefixed; accept either."""
    b = bare_id(sid)
    return [x for x in dict.fromkeys([sid, b, f"session-{b}"]) if x]


def append_qoder_messages(items, backup_dir, live_guard_min=0):
    """items: [(real_qoder_session_id, [canonical messages])] -- insert rows only.

    Never creates a session, never rewrites existing rows, never touches a
    session Qoder was still using inside the guard window (its in-memory copy
    would win and our rows would look like ghosts).
    """
    res = {"appended": 0, "sessions": 0, "skipped": []}
    if not items:
        return res
    bk = os.path.join(backup_dir, "qoder-main.append.sqlite")
    if not os.path.isfile(bk):
        os.makedirs(os.path.dirname(bk), exist_ok=True)
        shutil.copy2(QODER_DB, bk)
    cutoff_ms = (time.time() - live_guard_min * 60) * 1000
    con = sqlite3.connect(QODER_DB, timeout=30)
    cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        for sid_req, msgs in items:
            row, sid = [], None
            for cand in id_variants(sid_req):
                row = list(cur.execute(
                    "SELECT updated_at FROM chat_sessions WHERE session_id=?", (cand,)))
                if row:
                    sid = cand
                    break
            if not row:
                res["skipped"].append([sid_req, "session-missing", 0])
                continue
            if (row[0][0] or 0) > cutoff_ms:
                res["skipped"].append([sid, "qoder-live", len(msgs)])
                continue
            seq = cur.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM chat_session_messages WHERE session_id=?",
                (sid,)).fetchone()[0]
            last_ms = 0
            for m in msgs:
                content = m.get("content") or ""
                if not str(content).strip():
                    continue
                role = m.get("role") or "assistant"
                if role == "tool":
                    continue
                if role not in ("user", "assistant"):
                    role = "assistant"
                seq += 1
                mid = str(uuid.uuid4())
                ts = int((ts_of(m) or time.time()) * 1000)
                last_ms = max(last_ms, ts)
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
                res["appended"] += 1
            if last_ms:
                cur.execute("UPDATE chat_sessions SET updated_at=? WHERE session_id=?",
                            (max(row[0][0] or 0, last_ms), sid))
                res["sessions"] += 1
        con.commit()
    except Exception as e:
        con.rollback()
        con.close()
        return {"appended": 0, "sessions": 0, "skipped": [], "error": str(e)}
    con.close()
    return res


def build_stores(verbose=True):
    """Read all three stores, drop automation runs, fold onto canonical ids.

    Returns {"wb", "ds", "stores", "qo_real_id", "lists"} -- reused by both the
    plan pass and the post-write verification pass.
    """
    wb, ds = WorkBuddyAdapter(), DshAdapter()
    if verbose:
        print(f"WB home: {wb.discover()}")
        print(f"DSH home: {ds.discover()}")
        print(f"Qoder DB: {QODER_DB}")
    t0 = time.time()
    raw = {"WB": wb.read_sessions(), "DSH": ds.read_sessions(), "Qoder": read_qoder_sessions()}
    if verbose:
        print("Read: WB={} DSH={} Qoder={} in {:.1f}s".format(
            len(raw["WB"]), len(raw["DSH"]), len(raw["Qoder"]), time.time() - t0))
    for k in raw:
        raw[k] = [s for s in raw[k] if not is_auto(s)]
    if verbose:
        print("After filter (human only): WB={} DSH={} Qoder={}".format(
            len(raw["WB"]), len(raw["DSH"]), len(raw["Qoder"])))

    # Load WB<->Qoder ID mapping from the migration ledger (imported sessions
    # live under a different id inside Qoder, so they must fold onto WB's id).
    id_map_file = os.path.join(BASE, "qoder_id_map.json")
    wb_to_qoder, qoder_to_wb = {}, {}
    if os.path.isfile(id_map_file):
        wb_to_qoder = json.load(open(id_map_file, encoding="utf-8"))
        qoder_to_wb = {v: k for k, v in wb_to_qoder.items()}

    stores = {
        "WB": index_by_id(raw["WB"]),
        "DSH": index_by_id(raw["DSH"]),
        "Qoder": index_by_id(raw["Qoder"], qoder_to_wb),
    }
    return {
        "wb": wb, "ds": ds, "stores": stores,
        # canonical id -> the session_id Qoder actually stores it under
        "qo_real_id": {bare_id(k): v for k, v in wb_to_qoder.items()},
    }


def compute_plan(stores):
    """-> ({store: [whole sessions]}, {store: [(id, target, [tail msgs])]})"""
    names = list(stores)
    all_ids = set()
    for n in names:
        all_ids |= set(stores[n])

    def src(i):
        return stores["WB"].get(i) or stores["DSH"].get(i) or stores["Qoder"].get(i)

    imports = {n: [src(i) for i in all_ids if i not in stores[n]] for n in names}
    for lst in imports.values():
        lst.sort(key=lambda s: s.get("last_activity_at") or 0, reverse=True)
    return imports, compute_tails(stores), all_ids


def print_plan(imports, tails, all_ids, limit=0, stores=None):
    print(f"\nPlan: union={len(all_ids)} sessions")
    hdr = f"  {'store':<7} {'new sessions':>12} {'tail sessions':>14} {'tail msgs':>10}"
    print(hdr)
    for n in ("WB", "DSH", "Qoder"):
        cnt = sum(len(t[2]) for t in tails[n])
        print(f"  {n:<7} {len(imports[n]):>12} {len(tails[n]):>14} {cnt:>10}")
    if stores:
        for n in ("WB", "DSH", "Qoder"):
            lost = unholdable(stores, n)
            if lost:
                total = sum(len(ms) for _, _, ms in lost)
                roles = ", ".join(sorted({m.get("role") for _, _, ms in lost for m in ms}))
                print(f"\n  Physically unwritable into {n}: {total} msg(s) (role: {roles})"
                      f" across {len(lost)} session(s) -- that store has no way to hold them,"
                      f" so they are NOT counted as drift and stay only in the stores that"
                      f" can represent them.")
    for n in ("WB", "DSH", "Qoder"):
        if not tails[n]:
            continue
        print(f"\n  Drift to repair -> {n}:")
        for sid, tgt, msgs in tails[n][:20]:
            srcs = {}
            for m in msgs:
                srcs[m.get("role")] = srcs.get(m.get("role"), 0) + 1
            roles = " ".join(f"{k}:{v}" for k, v in sorted(srcs.items()))
            print("    {:<34} +{:<4} ({})  was {} msgs".format(
                (tgt.get("title") or sid)[:34], len(msgs), roles, len(tgt.get("messages") or [])))
        if len(tails[n]) > 20:
            print(f"    ... +{len(tails[n]) - 20} more sessions")
        if limit:
            print(f"    (--limit {limit}: only the first {limit} sessions will be written)")
    for n in ("WB", "DSH", "Qoder"):
        if imports[n]:
            print(f"\n  Whole sessions missing in {n}: {len(imports[n])}")
            for s in imports[n][:3]:
                print(f"    {s['id'][:16]} msgs={len(s.get('messages') or [])} {(s.get('title') or '')[:35]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--live-guard", type=int, default=0,
                    help="extra safety: even with Qoder closed, hold back sessions whose row "
                         "was written to inside this many minutes (0 = only the app-open check)")
    ap.add_argument("--defer-qoder", action="store_true",
                    help="never write into Qoder in this run (the watcher passes it while Qoder "
                         "is still open); its tails are reported and applied on Qoder's exit")
    a = ap.parse_args()

    ctx = build_stores()
    stores, wb, ds = ctx["stores"], ctx["wb"], ctx["ds"]
    imports, tails, all_ids = compute_plan(stores)
    if a.limit:
        for n in tails:
            tails[n] = tails[n][:a.limit]
            imports[n] = imports[n][:a.limit]

    # Qoder is the store we read while working, so writing into it while the app is
    # open would race its in-memory copy of any tab. All Qoder writes are therefore
    # deferred to the run the watcher triggers when the app exits.
    qoder_open = process_alive(QODER_EXE) or a.defer_qoder
    qoder_plan = (len(imports["Qoder"]), len(tails["Qoder"]),
                  sum(len(t[2]) for t in tails["Qoder"]))
    if qoder_open:
        for lst in (imports["Qoder"], tails["Qoder"]):
            lst.clear()
    print_plan(imports, tails, all_ids, a.limit, stores)

    if qoder_open and qoder_plan[1]:
        why = "--defer-qoder flag" if not process_alive(QODER_EXE) else "tasklist sees the app open"
        print("\n  Qoder is OPEN ({}): its {} tail sessions / {} msgs are NOT written in this"
              " run. Nothing is ever appended into a store the app is using; the watcher"
              " applies them the moment Qoder exits.".format(why, qoder_plan[1], qoder_plan[2]))

    if not a.apply:
        print("\n[DRY-RUN] No writes. Add --apply to execute.")
        return

    def has_work(n):
        return bool(imports[n]) or bool(tails[n])

    drift_before = {n: {"sessions": len(tails[n]),
                        "msgs": sum(len(t[2]) for t in tails[n])} for n in tails}

    state = load_state()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, f"sync-{stamp}")
    os.makedirs(bdir, exist_ok=True)

    running = {"WB": process_alive("WorkBuddy.exe"),
               "DSH": process_alive("DeepSeek Harness.exe")}

    # ---------------- DSH ----------------
    if has_work("DSH"):
        if running["DSH"]:
            print("\n!! DSH is running -> its imports and tails are SKIPPED. "
                  "Close DSH and re-run.")
        else:
            root = ds.discover()
            if os.path.isdir(root):
                shutil.copytree(root, os.path.join(bdir, "dsh-sessions"), dirs_exist_ok=True)
            if imports["DSH"]:
                print(f"\n=== -> DSH import: {len(imports['DSH'])} sessions ===")
                print(f"  {json.dumps(ds.write_sessions([fold_tools(s) for s in imports['DSH']]), ensure_ascii=False)[:300]}")
            if tails["DSH"]:
                print(f"\n=== -> DSH tail: {drift_before['DSH']['msgs']} msgs in {len(tails['DSH'])} sessions ===")
                sess = [tail_session(t, ms) for _, t, ms in tails["DSH"]]
                print(f"  {json.dumps(ds.write_sessions(sess), ensure_ascii=False)[:300]}")

    # ---------------- WB ----------------
    if has_work("WB"):
        if running["WB"]:
            print("\n!! WB is running -> its imports and tails are SKIPPED. "
                  "Close WB and re-run.")
        else:
            db = os.path.join(wb.discover(), "workbuddy.db")
            if os.path.isfile(db):
                shutil.copy2(db, os.path.join(bdir, "workbuddy.db"))
            if imports["WB"]:
                print(f"\n=== -> WB import: {len(imports['WB'])} sessions ===")
                print(f"  {json.dumps(wb.write_sessions(imports['WB']), ensure_ascii=False)[:300]}")
            if tails["WB"]:
                print(f"\n=== -> WB tail: {drift_before['WB']['msgs']} msgs in {len(tails['WB'])} sessions ===")
                sess = [tail_session(t, ms) for _, t, ms in tails["WB"]]
                print(f"  {json.dumps(wb.write_sessions(sess), ensure_ascii=False)[:300]}")

    # ---------------- Qoder ----------------
    if has_work("Qoder"):
        if imports["Qoder"]:
            print(f"\n=== -> Qoder import: {len(imports['Qoder'])} sessions ===")
            print(f"  {json.dumps(write_qoder_sessions(imports['Qoder'], bdir), ensure_ascii=False)[:300]}")
        if tails["Qoder"]:
            items = [(ctx["qo_real_id"].get(sid, sid), ms) for sid, _, ms in tails["Qoder"]]
            print(f"\n=== -> Qoder tail: {drift_before['Qoder']['msgs']} msgs in {len(items)} sessions ===")
            res = append_qoder_messages(items, bdir, a.live_guard)
            print(f"  {json.dumps(res, ensure_ascii=False)[:400]}")
            for sid, why, cnt in res.get("skipped", []):
                print(f"    held back ({why}): {sid[:16]} {cnt} msgs")

    # ---------------- Verification ----------------
    print("\n=== Verification (re-read, re-plan) ===")
    ctx2 = build_stores(verbose=False)
    imports2, tails2, all_ids2 = compute_plan(ctx2["stores"])
    for n in ("WB", "DSH", "Qoder"):
        left = sum(len(t[2]) for t in tails2[n])
        new = len(imports2[n])
        print("  {:<6} new sessions left: {:<3} tail msgs left: {}".format(n, new, left))
    state["counts"] = {"wb": len(ctx2["stores"]["WB"]), "dsh": len(ctx2["stores"]["DSH"]),
                       "qoder": len(ctx2["stores"]["Qoder"]), "union": len(all_ids2)}
    state["drift_before"] = drift_before
    state["drift_after"] = {n: sum(len(t[2]) for t in tails2[n]) for n in tails2}
    save_state(state)
    print(f"\nDone. Backups: {bdir}")


if __name__ == "__main__":
    main()
