"""
NUC side of the remote refresh trigger.

PythonAnywhere holds a flag (see pa_sa_blueprint.py); this polls it and, when a
refresh has been requested from Bryan's phone, runs the same scan the scheduled
task runs and reports the outcome back so the phone page can show what happened.

The work has to happen here, not on PA: Facebook needs the logged-in Chrome
profile and the push needs the git credentials — neither exists in the cloud.
Same division of labour as the AA scraper.

Run by Task Scheduler every couple of minutes:
    python tools/trigger_poll.py

    --once      poll a single time (default)
    --force     run a refresh regardless of the flag (local testing)
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_FILE = os.path.join(REPO, "tools", "pa_trigger_key.txt")
LOG_FILE = os.path.join(REPO, "tools", "refresh.log")
ROOT = "https://armyofb.pythonanywhere.com/sa"
PYTHON = sys.executable or "python"

INTERESTING = ("+ NEW", "NEEDS REVIEW", "pruned", "pushed", "no show changes",
               "! ", "possibly missing")


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] trigger: {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def key():
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{ROOT}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {key()}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode() or "{}")


def run_refresh():
    """Facebook first, then Skeletix — same order as refresh_events.bat.

    The summary is read back out of refresh.log rather than the child's stdout:
    both scripts write that file as explicit utf-8, whereas a piped stdout under
    pythonw (no console) comes back in the Windows codepage and mangles
    em-dashes and band names in the phone-facing summary.
    """
    try:
        start = os.path.getsize(LOG_FILE)
    except OSError:
        start = 0

    failures = []
    for script in ("fb_scan.py", "refresh_events.py"):
        path = os.path.join(REPO, "tools", script)
        try:
            subprocess.run([PYTHON, path], cwd=REPO, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=600)
        except subprocess.TimeoutExpired:
            failures.append(f"! {script} timed out")
        except Exception as exc:
            failures.append(f"! {script} failed to start: {exc}")

    lines = []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start)
            for line in fh.read().splitlines():
                if any(tok.lower() in line.lower() for tok in INTERESTING):
                    lines.append(line.split("] ", 1)[-1])
    except OSError as exc:
        failures.append(f"! could not read log: {exc}")
    return lines + failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not key():
        log("no trigger key file — nothing to poll")
        return 1

    if not args.force:
        try:
            state = call("/pending")
        except (urllib.error.URLError, ValueError) as exc:
            # PA asleep or offline: not worth logging loudly every 2 minutes
            print(f"poll failed: {str(exc)[:120]}")
            return 0
        if not state.get("pending"):
            return 0
        try:
            claimed = call("/claim", method="POST", payload={})
        except Exception as exc:
            log(f"! claim failed: {str(exc)[:120]}")
            return 1
        if not claimed.get("claimed"):
            return 0

    log("refresh requested remotely — running")
    lines = run_refresh()
    summary = "\n".join(lines[-14:]) or "ran, nothing notable in the log"
    log("remote refresh done: " + (lines[-1] if lines else "no output"))

    try:
        call("/result", method="POST", payload={"summary": summary})
    except Exception as exc:
        log(f"! could not report result: {str(exc)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
