# -*- coding: utf-8 -*-
"""Agent Sync Watcher — event-driven sync on process exit.

Watches the three agent apps. When any of them exits, runs sync.py --apply so
the conversations that were held open get propagated into the other stores.

  WorkBuddy.exe        -> must be closed for WB writes
  DeepSeek Harness.exe -> must be closed for DSH writes
  Qoder CN.exe         -> must be closed for Qoder writes; while it runs,
                          sync.py defers the whole Qoder plan (imports and
                          tails) to the next pass, which this watcher triggers
                          on its exit

Launches hidden from the Startup .vbs. ~5 MB RAM, polls the process set every
15 s and re-applies every FULL_PASS_INTERVAL (15 min) as a heartbeat, so drift
is repaired even when an exit event is missed.
No network calls. Pure local file/database operations.
"""
import subprocess, time, os, sys, logging

BASE = os.path.dirname(os.path.abspath(__file__))
SYNC_SCRIPT = os.path.join(BASE, "sync.py")
PYTHON = sys.executable
LOG_FILE = os.path.join(BASE, "watcher.log")
POLL_INTERVAL = 15          # seconds -- how often the process set is sampled
QUIET_AFTER_EXIT = 3        # let file handles release before the first write
FULL_PASS_INTERVAL = 900    # seconds -- heartbeat pass, repairs drift even when
                            # an exit event is missed (watcher was down, app
                            # closed during a reboot, ...)

QODER_EXE = "Qoder CN.exe"
# This process is pythonw.exe (no console), so every child would otherwise get a
# brand-new Windows Terminal window flashed open and shut -- once per poll, i.e.
# every 15 s. CREATE_NO_WINDOW stops the allocation; pipes still work.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WATCHED = {
    "WorkBuddy.exe": "wb",
    "DeepSeek Harness.exe": "dsh",
    QODER_EXE: "qoder",
}

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sync-watcher")


def running_processes():
    """Set of watched image names currently running (tasklist, local only).

    tasklist reports the image name with its own capitalisation ("Notepad.exe"
    for a filter of "notepad.exe"), so the comparison is case-insensitive; a
    missed match here would make an open app look closed.
    """
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL, timeout=15, creationflags=NO_WINDOW
        ).decode("gbk", errors="replace")
        found = {row[0].strip().lower() for row in _csv_rows(out) if row}
        return {name for name in WATCHED if name.lower() in found}
    except Exception as e:
        log.warning("tasklist failed: %s", e)
        return set()


def _csv_rows(text):
    import csv
    return [r for r in csv.reader(text.splitlines()) if r]


def run_sync(reason="exit"):
    """Apply the sync. sync.py itself decides what is safe to write:
    WB/DSH writes are skipped while those apps run, Qoder writes are deferred
    while Qoder runs (this watcher re-runs on every exit, so nothing is lost)."""
    cmd = [PYTHON, SYNC_SCRIPT, "--apply"]
    log.info("trigger (%s): %s", reason, " ".join(cmd[1:]))
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                           encoding="utf-8", errors="replace",
                           creationflags=NO_WINDOW)
        tail = (r.stdout or "").strip().splitlines()[-4:]
        for line in tail:
            log.info("  | %s", line)
        if r.returncode != 0:
            log.error("sync exit=%s stderr: %s", r.returncode, (r.stderr or "")[:400])
        else:
            log.info("sync ok in %.0fs", time.time() - t0)
    except subprocess.TimeoutExpired:
        log.error("sync timed out (900s)")
    except Exception as e:
        log.error("sync failed: %s", e)


def main():
    log.info("=== Agent Sync Watcher started (pid %s) ===", os.getpid())
    log.info("Watching: %s, poll=%ss, heartbeat=%ss",
             sorted(WATCHED), POLL_INTERVAL, FULL_PASS_INTERVAL)
    prev = running_processes()
    log.info("Initial running: %s", sorted(prev) or "none")
    last_full = time.time()

    while True:
        time.sleep(POLL_INTERVAL)
        curr = running_processes()
        exited = prev - curr
        started = curr - prev
        if started:
            log.info("start: %s", sorted(started))
        if exited:
            log.info("exit: %s -> apply", sorted(exited))
            time.sleep(QUIET_AFTER_EXIT)
            # One sync pass covers every store whose app just released its lock.
            run_sync()
            curr = running_processes()
            last_full = time.time()
        elif time.time() - last_full >= FULL_PASS_INTERVAL:
            last_full = time.time()
            run_sync("heartbeat")

        prev = curr


if __name__ == "__main__":
    main()
