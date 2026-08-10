"""
AAdvantage Goals Calculator — Flask backend for PythonAnywhere.

The calculator UI runs in the browser. This server:
  * Serves the React-in-HTML app and the price-data JSON files.
  * Mediates between the browser and a local Windows scraper daemon via a
    small job queue. PA cannot scrape AA directly (datacenter IPs are blocked)
    so the daemon polls these endpoints over the public web.

Auth model:
  * Browser-facing endpoints are open (single-user public app, low stakes).
  * Daemon-facing endpoints require a shared bearer token configured as the
    SCRAPE_TOKEN environment variable. Set it via the Web tab on PA and via
    `setx SCRAPE_TOKEN ...` on Windows.
"""
import json
import os
import threading
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from flask import Flask, abort, jsonify, request, send_from_directory

app = Flask(__name__)

from japanese_routes import bp as japanese_bp
app.register_blueprint(japanese_bp)

# --- phone-chat blueprint -------------------------------------------------
import sys as _sys
_PHONE_CHAT_PATH = "/home/armyofb/phone-chat"
if _PHONE_CHAT_PATH not in _sys.path:
    _sys.path.insert(0, _PHONE_CHAT_PATH)
try:
    from chat_blueprint import bp as _chat_bp, init_app as _init_chat
    app.register_blueprint(_chat_bp)
    _init_chat(app)
except Exception as _e:
    import logging as _lg
    _lg.getLogger(__name__).exception("phone-chat blueprint failed: %s", _e)
# --- /phone-chat blueprint ------------------------------------------------

# --- img-gen blueprint ----------------------------------------------------
# URL-based image generator: GET /img/<name>.png?prompt=... triggers Atlas
# Cloud gen, caches to /home/armyofb/img-gen/images/, serves bytes back.
# Auth piggybacks on the chat session cookie.
_IMG_GEN_PATH = "/home/armyofb/img-gen"
if _IMG_GEN_PATH not in _sys.path:
    _sys.path.insert(0, _IMG_GEN_PATH)
try:
    from img_blueprint import bp as _img_bp, init_app as _init_img
    app.register_blueprint(_img_bp)
    _init_img(app)
except Exception as _e:
    import logging as _lg
    _lg.getLogger(__name__).exception("img-gen blueprint failed: %s", _e)
# --- /img-gen blueprint ---------------------------------------------------

# --- swing-dashboard blueprint -------------------------------------------
import sys as _sys, logging as _lg
_SWING_PATH = "/home/armyofb/swing-dashboard"
if _SWING_PATH not in _sys.path:
    _sys.path.insert(0, _SWING_PATH)
try:
    from swing_blueprint import bp as _swing_bp
    app.register_blueprint(_swing_bp)
except Exception as _e:
    _lg.getLogger(__name__).exception("swing dashboard blueprint failed: %s", _e)
# --- /swing-dashboard blueprint ------------------------------------------

# -------- paths -----------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
QUEUE_PATH = os.path.join(BASE_DIR, "scrape_queue.json")
ROUTE_PRICES_PATH = os.path.join(TEMPLATES_DIR, "route_prices.json")

# How many completed jobs to keep in the queue file (rolling window).
KEEP_COMPLETED = 50

# Auto-fail any job stuck in "running"/"claimed" longer than this. The daemon
# may have crashed or hit a bot block — we don't want zombie jobs showing as
# "scraping" in the UI forever.
STALE_AFTER_SECONDS = 600  # 10 minutes

_queue_lock = threading.Lock()


# -------- queue helpers ---------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_queue() -> dict:
    if not os.path.exists(QUEUE_PATH):
        return {"jobs": []}
    try:
        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"jobs": []}


def _save_queue(data: dict) -> None:
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, QUEUE_PATH)


def _prune(jobs: list) -> list:
    """Keep all non-terminal jobs and the most recent N terminal ones."""
    terminal = {"done", "failed"}
    active = [j for j in jobs if j.get("status") not in terminal]
    completed = sorted(
        (j for j in jobs if j.get("status") in terminal),
        key=lambda j: j.get("completed_at") or j.get("created_at") or "",
        reverse=True,
    )
    return active + completed[:KEEP_COMPLETED]


def _expire_stale(jobs: list) -> bool:
    """Mark jobs stuck in claimed/running past the timeout as failed.
    Returns True if anything changed."""
    cutoff = datetime.now(timezone.utc).timestamp() - STALE_AFTER_SECONDS
    changed = False
    for j in jobs:
        if j.get("status") not in ("claimed", "running"):
            continue
        ts_str = j.get("claimed_at") or j.get("created_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            j["status"] = "failed"
            j["completed_at"] = _now()
            j["error"] = j.get("error") or "Daemon timed out / crashed before posting result."
            changed = True
    return changed


# -------- auth ------------------------------------------------------------
def _expected_token() -> str:
    return os.environ.get("SCRAPE_TOKEN", "")


def daemon_required(fn):
    """Decorator: require a valid bearer token for daemon-only endpoints."""
    @wraps(fn)
    def wrapper(*a, **kw):
        token = _expected_token()
        if not token:
            # If SCRAPE_TOKEN is unset on the server, daemon mode is disabled.
            abort(503, description="Scrape daemon not configured (SCRAPE_TOKEN missing).")
        header = request.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        if provided != token:
            abort(401, description="Invalid scrape token.")
        return fn(*a, **kw)
    return wrapper


# -------- static-ish routes ----------------------------------------------
@app.route("/")
def index():
    return send_from_directory(TEMPLATES_DIR, "index.html")


@app.route("/award_prices.json")
def award_prices():
    """General off-peak/peak grid the calculator loads on startup."""
    path = os.path.join(TEMPLATES_DIR, "award_prices.json")
    if not os.path.exists(path):
        return jsonify({"error": "award_prices.json not found"}), 404
    resp = send_from_directory(TEMPLATES_DIR, "award_prices.json")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/route_prices.json")
def route_prices():
    """Date-keyed precise scrapes for Plan-Trip / per-goal queries."""
    if not os.path.exists(ROUTE_PRICES_PATH):
        # Empty body, not 404 — the UI treats absence as "no precise data yet"
        return jsonify({"_meta": {"last_updated": None}, "entries": {}})
    resp = send_from_directory(TEMPLATES_DIR, "route_prices.json")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "version": "4.0-scrape-queue",
        "price_file_present": os.path.exists(os.path.join(TEMPLATES_DIR, "award_prices.json")),
        "route_prices_present": os.path.exists(ROUTE_PRICES_PATH),
        "queue_configured": bool(_expected_token()),
    })


# -------- browser-facing scrape endpoints ---------------------------------
@app.route("/api/scrape/request", methods=["POST"])
def enqueue_scrape():
    """
    Body:
        {"type": "all"}
        {"type": "goals", "routes": ["pdx","lhr"]}                  // default dates
        {"type": "goals", "route_dates": [{"route":"pdx","depart":"2026-12-15","return":"2026-12-22"}, ...]}
        {"type": "trip",  "route": "pdx", "depart": "...", "return": "..."}
    """
    if not _expected_token():
        return jsonify({"error": "Scrape daemon not configured on server."}), 503

    payload = request.get_json(force=True, silent=True) or {}
    jtype = payload.get("type")
    if jtype not in ("all", "goals", "trip"):
        return jsonify({"error": "type must be one of: all, goals, trip"}), 400

    spec = {}
    if jtype == "goals":
        if "route_dates" in payload:
            rd = payload["route_dates"]
            if not isinstance(rd, list) or not rd:
                return jsonify({"error": "route_dates must be a non-empty list"}), 400
            spec["route_dates"] = rd
        elif "routes" in payload:
            r = payload["routes"]
            if not isinstance(r, list) or not r:
                return jsonify({"error": "routes must be a non-empty list"}), 400
            spec["routes"] = r
        else:
            return jsonify({"error": "goals requires routes or route_dates"}), 400
    elif jtype == "trip":
        for k in ("route", "depart"):
            if k not in payload:
                return jsonify({"error": f"trip requires {k}"}), 400
        spec["route"] = payload["route"]
        spec["depart"] = payload["depart"]
        if payload.get("return"):
            spec["return"] = payload["return"]

    job = {
        "id": uuid4().hex,
        "type": jtype,
        "spec": spec,
        "status": "queued",
        "created_at": _now(),
        "claimed_at": None,
        "completed_at": None,
        "error": None,
    }
    with _queue_lock:
        q = _load_queue()
        # Coalesce: if an identical job is already queued, return its id instead.
        for j in q["jobs"]:
            if j["status"] == "queued" and j["type"] == jtype and j["spec"] == spec:
                return jsonify({"job_id": j["id"], "coalesced": True})
        q["jobs"].append(job)
        _save_queue(q)
    return jsonify({"job_id": job["id"], "coalesced": False})


@app.route("/api/scrape/status")
def scrape_status():
    """Snapshot of queue for UI display. Public.
    Lazily expires jobs stuck in running > STALE_AFTER_SECONDS so a crashed
    daemon doesn't leave the UI showing 'scraping' forever."""
    with _queue_lock:
        q = _load_queue()
        if _expire_stale(q.get("jobs", [])):
            _save_queue(q)
        jobs = q.get("jobs", [])
    return jsonify({
        "queued": [j for j in jobs if j["status"] == "queued"],
        "running": [j for j in jobs if j["status"] in ("claimed", "running")],
        "recent": [j for j in jobs if j["status"] in ("done", "failed")][:10],
    })


@app.route("/api/scrape/clear", methods=["POST"])
def scrape_clear():
    """Mark all queued/claimed/running jobs as failed. Public; same trust
    posture as enqueue. Useful when the daemon crashed leaving zombies."""
    with _queue_lock:
        q = _load_queue()
        affected = 0
        for j in q.get("jobs", []):
            if j["status"] in ("queued", "claimed", "running"):
                j["status"] = "failed"
                j["completed_at"] = _now()
                j["error"] = j.get("error") or "Cleared by user."
                affected += 1
        q["jobs"] = _prune(q["jobs"])
        _save_queue(q)
    return jsonify({"cleared": affected})


@app.route("/api/scrape/job/<job_id>")
def get_job(job_id):
    q = _load_queue()
    for j in q.get("jobs", []):
        if j["id"] == job_id:
            return jsonify(j)
    return jsonify({"error": "not found"}), 404


# -------- daemon-facing endpoints ----------------------------------------
@app.route("/api/scrape/queue")
@daemon_required
def get_queue():
    """Local daemon polls this. Returns up to N queued jobs."""
    q = _load_queue()
    queued = [j for j in q.get("jobs", []) if j["status"] == "queued"]
    return jsonify({"jobs": queued[:5]})


@app.route("/api/scrape/claim/<job_id>", methods=["POST"])
@daemon_required
def claim_job(job_id):
    with _queue_lock:
        q = _load_queue()
        for j in q.get("jobs", []):
            if j["id"] == job_id:
                if j["status"] != "queued":
                    return jsonify({"error": f"job status is {j['status']}, cannot claim"}), 409
                j["status"] = "running"
                j["claimed_at"] = _now()
                _save_queue(q)
                return jsonify(j)
    return jsonify({"error": "not found"}), 404


@app.route("/api/scrape/result/<job_id>", methods=["POST"])
@daemon_required
def post_result(job_id):
    """
    Body: {"success": bool, "error": "...", "data": {...}}

    `data` shape depends on job type:
      * type=all          -> {"award_prices": {...}}   (full file)
      * type=goals/routes -> {"award_prices": {...}}   (full updated file)
      * type=goals/dates  -> {"route_prices": {...}}   (entries to merge)
      * type=trip         -> {"route_prices": {...}}   (single entry to merge)
    """
    payload = request.get_json(force=True, silent=True) or {}
    success = bool(payload.get("success"))
    data = payload.get("data") or {}

    with _queue_lock:
        q = _load_queue()
        target = None
        for j in q.get("jobs", []):
            if j["id"] == job_id:
                target = j
                break
        if not target:
            return jsonify({"error": "not found"}), 404

        target["status"] = "done" if success else "failed"
        target["completed_at"] = _now()
        target["error"] = payload.get("error") if not success else None

        # Apply data to the right file
        if success:
            try:
                if "award_prices" in data:
                    out = os.path.join(TEMPLATES_DIR, "award_prices.json")
                    tmp = out + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data["award_prices"], f, indent=2, ensure_ascii=False)
                    os.replace(tmp, out)
                if "route_prices" in data:
                    existing = {"_meta": {"last_updated": None}, "entries": {}}
                    if os.path.exists(ROUTE_PRICES_PATH):
                        try:
                            with open(ROUTE_PRICES_PATH, "r", encoding="utf-8") as f:
                                existing = json.load(f)
                        except Exception:
                            pass
                    incoming = data["route_prices"]
                    if isinstance(incoming, dict):
                        existing.setdefault("entries", {}).update(incoming.get("entries", incoming))
                        existing["_meta"] = {"last_updated": _now()}
                    tmp = ROUTE_PRICES_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(existing, f, indent=2, ensure_ascii=False)
                    os.replace(tmp, ROUTE_PRICES_PATH)
            except Exception as e:
                target["status"] = "failed"
                target["error"] = f"Failed to write result file: {e}"

        q["jobs"] = _prune(q["jobs"])
        _save_queue(q)
        return jsonify(target)


# Local dev only — PythonAnywhere uses WSGI
if __name__ == "__main__":
    app.run(debug=True, port=5000)
