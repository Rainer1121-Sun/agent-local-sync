# -*- coding: utf-8 -*-
"""Agent Sync Watcher — event-driven sync on process exit.

Monitors WorkBuddy.exe and DeepSeek Harness.exe. When either exits,
triggers sync.py to propagate new conversations to the other stores.
Also syncs Qoder (read-only from main.sqlite -> WB -> DSH).

Launch hidden via startup .vbs. ~5MB RAM, polls every 15s.
No network calls. No scheduled tasks. Pure local file operations.
"""
import subprocess, time, os, sys, json, datetime, logging

SYNC_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.py")
PYTHON = sys.executable
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher.log")
POLL_INTERVAL = 15  # seconds

WATCHED = {
    "WorkBuddy.exe": "wb",
    "DeepSeek Harness.exe": "dsh",
}

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sync-watcher")


def get_running():
    """Return set of watched process names currently running."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL, timeout=10
        ).decode("gbk", errors="replace")
        running = set()
        for line in out.strip().splitlines():
            parts = line.split('","')
            if parts:
                name = parts[0].strip('"')
                if name in WATCHED:
                    running.add(name)
        return running
    except Exception as e:
        log.warning(f"tasklist failed: {e}")
        return set()


def run_sync(direction="both"):
    """Run sync.py --apply."""
    cmd = [PYTHON, SYNC_SCRIPT, "--apply", "--direction", direction]
    log.info(f"Triggering sync: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace"
        )
        log.info(f"sync exit={result.returncode}")
        if result.stdout:
            for line in result.stdout.strip().splitlines()[-5:]:
                log.info(f"  {line}")
        if result.returncode != 0 and result.stderr:
            log.error(f"  stderr: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        log.error("sync timed out (300s)")
    except Exception as e:
        log.error(f"sync failed: {e}")


def main():
    log.info("=== Agent Sync Watcher started ===")
    log.info(f"Watching: {list(WATCHED.keys())}, poll={POLL_INTERVAL}s")
    prev = get_running()
    log.info(f"Initial running: {prev or 'none'}")

    while True:
        time.sleep(POLL_INTERVAL)
        curr = get_running()

        # Detect exits (was running, now not)
        exited = prev - curr
        if exited:
            names = [WATCHED[p] for p in exited]
            log.info(f"Detected exit: {exited} -> triggering sync")
            # Small delay to let file handles release
            time.sleep(3)
            run_sync("both")

        # Detect starts (for logging only)
        started = curr - prev
        if started:
            log.info(f"Detected start: {started}")

        prev = curr


if __name__ == "__main__":
    main()
