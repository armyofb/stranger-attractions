"""
Stranger Attractions — remote refresh trigger (PythonAnywhere side).

Deployed to /home/armyofb/sa-trigger/sa_blueprint.py and registered by
aa_calc/flask_app.py. Mirrors the AA scraper's arrangement: PythonAnywhere holds
a tiny job flag, and the NUC — which is the only machine that can actually do the
work (logged-in Chrome for Facebook, git credentials for the push) — polls it.

Endpoints
  GET  /sa/            phone-friendly page: big button + last result
  GET  /sa/refresh     queue a refresh          (?key=... )
  GET  /sa/status.json current state as JSON    (?key=... )
  GET  /sa/pending     daemon: is one queued?   (Bearer or ?key=)
  POST /sa/claim       daemon: take the job, clears the flag
  POST /sa/result      daemon: report what happened

The key lives in trigger_key.txt next to this file. It only queues a website
refresh — no destructive capability — so a query-string key is proportionate.
"""
import json
import os
from datetime import datetime, timezone

from flask import Blueprint, abort, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "sa_state.json")
KEY_PATH = os.path.join(BASE, "trigger_key.txt")

bp = Blueprint("sa_trigger", __name__, url_prefix="/sa")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key():
    try:
        with open(KEY_PATH, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"pending": False, "requested_at": None,
                "last_result": None, "last_run_at": None, "runs": 0}


def _save(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def _auth():
    expected = _key()
    if not expected:
        abort(503, description="trigger key not configured")
    header = request.headers.get("Authorization", "")
    provided = header[7:] if header.startswith("Bearer ") else request.args.get("key", "")
    if provided != expected:
        abort(401, description="bad key")


PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stranger Attractions — refresh</title>
<style>
 body{background:#0a0a0c;color:#e8e6e3;font:16px/1.5 system-ui,sans-serif;
      margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
 .wrap{padding:2rem 1.25rem;max-width:32rem;width:100%%;text-align:center}
 h1{font-size:1.1rem;letter-spacing:.18em;text-transform:uppercase;color:#9b98a0;font-weight:600}
 a.btn{display:block;margin:1.5rem 0;padding:1.1rem;background:#d40b1f;color:#fff;
       text-decoration:none;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
       border-radius:2px}
 a.btn:active{background:#ff2438}
 .box{background:#16161b;border:1px solid #26262e;padding:1rem;text-align:left;
      border-radius:2px;font-size:.9rem;white-space:pre-wrap;word-break:break-word}
 .muted{color:#9b98a0;font-size:.8rem;margin-top:1rem}
 .pending{color:#ffd166}
</style>
<div class=wrap>
<h1>Stranger Attractions</h1>
<a class=btn href="/sa/refresh?key=%(key)s">Refresh the site now</a>
%(status)s
<p class=muted>The NUC picks this up within ~2 minutes and pushes any new shows.</p>
</div>"""


@bp.route("/")
def home():
    _auth()
    st = _load()
    if st.get("pending"):
        body = '<div class="box pending">Refresh queued — waiting for the NUC…</div>'
    else:
        last = st.get("last_result") or "no runs recorded yet"
        when = st.get("last_run_at") or "—"
        body = f'<div class=box><b>Last run</b> {when}\n{last}</div>'
    return PAGE % {"key": _key(), "status": body}


@bp.route("/refresh")
def queue_refresh():
    _auth()
    st = _load()
    st["pending"] = True
    st["requested_at"] = _now()
    _save(st)
    return PAGE % {
        "key": _key(),
        "status": '<div class="box pending">Refresh queued. The NUC will pick it up '
                  'within ~2 minutes — reload this page to see the result.</div>',
    }


@bp.route("/status.json")
def status_json():
    _auth()
    return jsonify(_load())


@bp.route("/pending")
def pending():
    _auth()
    st = _load()
    return jsonify({"pending": bool(st.get("pending")), "requested_at": st.get("requested_at")})


@bp.route("/claim", methods=["POST"])
def claim():
    _auth()
    st = _load()
    was = bool(st.get("pending"))
    st["pending"] = False
    _save(st)
    return jsonify({"claimed": was, "requested_at": st.get("requested_at")})


@bp.route("/result", methods=["POST"])
def result():
    _auth()
    data = request.get_json(silent=True) or {}
    st = _load()
    st["last_result"] = str(data.get("summary", ""))[:2000]
    st["last_run_at"] = _now()
    st["runs"] = int(st.get("runs") or 0) + 1
    _save(st)
    return jsonify({"ok": True})
