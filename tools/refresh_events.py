"""
Stranger Attractions event refresher — plain deterministic script, no AI in the loop.

Pulls the promoter's shows straight from Skeletix's Algolia search index (the same
JSON API their own site's search uses), adds any new ones to events.js, downloads
the hi-res poster, prunes long-past shows, and pushes to GitHub (Cloudflare Pages
auto-deploys).

Run by Windows Task Scheduler via refresh_events.bat. Safe to run repeatedly:
it only writes/commits when something actually changed.

    python tools/refresh_events.py [--dry-run] [--verbose]

Notes / gotchas baked in:
  * Only events matching the promoter query are auto-added. Shows he books whose
    Skeletix title omits his name (e.g. band-hosted record release shows) are
    reported in the log under "POSSIBLY MISSING" for a human to confirm — never
    auto-added, because other promoters use the same venues.
  * Future events already in events.js are never deleted, even if they vanish from
    the index; they're only flagged. Pruning is limited to shows >30 days past.
  * State Street Pub is a 21+ room. Skeletix listings sometimes say "ALL AGES"
    anyway, so the venue rule overrides the feed.
  * `tag` (genre/origin flavor text) is left empty for new shows — it's editorial,
    add it by hand in events.js if wanted.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

# --- config ---------------------------------------------------------------

ALGOLIA_APP = "4P6121I2PI"
ALGOLIA_KEY = "44655db9d9e29e1bace9f6aa3e68153e"  # public search-only key from their site
ALGOLIA_INDEX = "event-prod"

PROMOTER_QUERY = "stranger attractions"
HIS_VENUES = ("Black Circle", "State Street Pub", "Holy Ground Studio")
TWENTYONE_PLUS_VENUES = ("State Street Pub",)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_JS = os.path.join(REPO, "events.js")
POSTER_DIR = os.path.join(REPO, "assets", "posters")
LOG_FILE = os.path.join(REPO, "tools", "refresh.log")

PRUNE_AFTER_DAYS = 30
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StrangerAttractionsRefresher/1.0"

LOG_LINES = []


def log(msg):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    LOG_LINES.append(line)
    print(line)


def flush_log():
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("\n".join(LOG_LINES) + "\n")
    except OSError as exc:
        print(f"(could not write log: {exc})")


# --- skeletix / algolia ---------------------------------------------------


def algolia(query, hits=200):
    url = f"https://{ALGOLIA_APP}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"
    payload = json.dumps({"params": f"query={urllib.parse.quote(query)}&hitsPerPage={hits}"}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "X-Algolia-Application-Id": ALGOLIA_APP,
            "X-Algolia-API-Key": ALGOLIA_KEY,
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def event_date(hit):
    times = hit.get("event_times") or []
    if times and times[0].get("date"):
        return times[0]["date"]
    return None


def derive_headliner(hit):
    """Skeletix titles are unreliable (sometimes just 'Stranger Attractions Presents'),
    so prefer the description, which reads 'X Presents HEADLINER with ACTS at VENUE'."""
    for source in (hit.get("description") or "", hit.get("title") or ""):
        text = source.strip()
        if not text:
            continue
        m = re.search(r"stranger\s+attractions[^:]{0,40}?presents\s*:?\s*", text, re.I)
        if m:
            text = text[m.end():]
        # NB: \b after "w/" never matches (slash is not a word char), so use an
        # explicit whitespace lookahead or headliners keep their support acts.
        text = re.split(r"\s+(?:with|w/|featuring|feat\.?|ft\.?)(?=\s)", text, flags=re.I)[0]
        text = re.split(r"\s+at\s+", text, flags=re.I)[0]
        text = re.sub(r"\(.*?\)", "", text)
        text = text.strip(" !?.,:;-–—")
        if text:
            return text.upper()
    return None


def acts_from_text(text, headliner):
    """Fest-style listings name a marquee act only in the title/description
    ('HELL IS REAL FEST w/ PROFANATICA & more'), so pull those out too."""
    if not text:
        return []
    m = re.search(r"\b(?:w/|with|featuring|feat\.?|ft\.?)\s+(.+)", text, re.I)
    if not m:
        return []
    tail = m.group(1)
    tail = re.split(r"\s+at\s+", tail, flags=re.I)[0]
    tail = re.sub(r"[!?.]+$", "", tail).strip()
    out = []
    for piece in re.split(r"\s*(?:,|&|\band\b|/)\s*", tail):
        name = piece.strip(" !?.,:;-–—")
        if not name or len(name) > 40:
            continue
        if name.lower() in ("more", "and more", "special guests", "guests", "many more", "tba"):
            continue
        if headliner and name.upper() == headliner.upper():
            continue
        # listings shout act names; match the file's house style, but leave
        # short strings alone so acronyms survive (VOID, TRHA, ...)
        if name.isupper() and len(name) > 4:
            name = name.title()
        out.append(name)
    return out


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "show"


def age_for(hit):
    venue = (hit.get("venue") or {}).get("name") or ""
    if any(v.lower() in venue.lower() for v in TWENTYONE_PLUS_VENUES):
        return "21 & Over"
    raw = (hit.get("age_restriction") or "").strip()
    low = raw.lower()
    if "21" in low:
        return "21 & Over"
    if "18" in low:
        return "18 & Over"
    if "all" in low:
        return "All Ages"
    return raw


def price_for(hit):
    tickets = hit.get("tickets") or []
    costs = []
    for t in tickets:
        try:
            costs.append(float(t.get("cost")))
        except (TypeError, ValueError):
            continue
    if not costs:
        return ""
    low = min(costs)
    return f"${low:.2f}".replace(".00", "")


def address_for(hit):
    v = hit.get("venue") or {}
    bits = [(v.get("street_1") or "").strip()]
    city = (v.get("city") or "").strip()
    state = (v.get("state") or "").strip()
    zipc = (v.get("zip") or "").strip()
    tail = " ".join(x for x in [state, zipc] if x)
    for part in (city, tail):
        if part:
            bits.append(part)
    return ", ".join(b for b in bits if b)


def poster_url(hit):
    img = hit.get("primary_image") or {}
    return img.get("zoom_url") or hit.get("image_url") or ""


def download_poster(url, dest, dry_run=False):
    if not url:
        return False
    if dry_run:
        log(f"    [dry-run] would download poster -> {os.path.basename(dest)}")
        return True
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if len(data) < 5000:
        log(f"    ! poster looked too small ({len(data)} bytes), skipping: {url}")
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    log(f"    poster saved: {os.path.basename(dest)} ({len(data):,} bytes)")
    return True


# --- events.js read / write ----------------------------------------------


def read_events_js():
    with open(EVENTS_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def split_entries(text):
    """Return (prefix, [entry_text, ...], suffix) for the EVENTS array."""
    m = re.search(r"const\s+EVENTS\s*=\s*\[", text)
    if not m:
        raise SystemExit("could not find `const EVENTS = [` in events.js")
    start = m.end()
    depth = 0
    entries = []
    entry_start = None
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "[" or ch == "{":
            if ch == "{" and depth == 0:
                entry_start = i
            depth += 1
        elif ch == "}" or ch == "]":
            if ch == "]" and depth == 0:
                return text[:start], entries, text[i:]
            depth -= 1
            if ch == "}" and depth == 0 and entry_start is not None:
                end = i + 1
                if end < len(text) and text[end] == ",":
                    end += 1
                entries.append(text[entry_start:end])
                entry_start = None
        i += 1
    raise SystemExit("events.js: unterminated EVENTS array")


def entry_field(entry, field):
    m = re.search(rf'{field}\s*:\s*"([^"]*)"', entry)
    return m.group(1) if m else ""


def entry_event_id(entry):
    m = re.search(r"skeletix\.com/(\d+)-", entry)
    return m.group(1) if m else None


def js_str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_entry(ev):
    support = ", ".join(js_str(a) for a in ev["support"])
    return (
        "  {\n"
        f"    headliner: {js_str(ev['headliner'])},\n"
        f"    support: [{support}],\n"
        f"    date: {js_str(ev['date'])},\n"
        f"    venue: {js_str(ev['venue'])},\n"
        f"    address: {js_str(ev['address'])},\n"
        f"    doors: {js_str(ev['doors'])},\n"
        f"    show: {js_str(ev['show'])},\n"
        f"    price: {js_str(ev['price'])},\n"
        f"    age: {js_str(ev['age'])},\n"
        f"    tickets: {js_str(ev['tickets'])},\n"
        f"    facebook: {js_str(ev.get('facebook') or '')},\n"
        f"    poster: {js_str(ev['poster'])},\n"
        f"    tag: \"\"\n"
        "  },"
    )


def hit_to_event(hit):
    headliner = derive_headliner(hit)
    date = event_date(hit)
    if not headliner or not date:
        return None
    acts = [a.strip() for a in (hit.get("acts") or []) if a and a.strip()]
    acts = [a for a in acts if a.upper() != headliner.upper()]
    # marquee acts named only in the title (fest listings) go first
    extra = acts_from_text(hit.get("title") or "", headliner)
    known = {a.upper() for a in acts}
    acts = [e for e in extra if e.upper() not in known] + acts
    venue = (hit.get("venue") or {}).get("name") or ""
    return {
        "headliner": headliner,
        "support": acts,
        "date": date,
        "venue": venue.strip(),
        "address": address_for(hit),
        "doors": (hit.get("doors_open") or "").strip(),
        "show": (hit.get("show_starts") or "").strip(),
        "price": price_for(hit),
        "age": age_for(hit),
        "tickets": (hit.get("ticket_url") or "").strip(),
        "poster": f"assets/posters/{slugify(headliner)}.jpg",
        "poster_url": poster_url(hit),
        "event_id": str(hit.get("event_id") or ""),
    }


# --- git ------------------------------------------------------------------


FB_CANDIDATES = os.path.join(REPO, "tools", "fb_candidates.json")
FB_MAX_AGE_HOURS = 18
# A Facebook post is prose, not a database row. Only publish one automatically
# when every field we need parsed cleanly; anything short of that gets logged
# for a human instead of guessed at, because a wrong date on a promoter's site
# sends people to the venue on the wrong night.
FB_REQUIRED = ("headliner", "date", "venue", "price", "age")


def norm_name(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def merge_facebook(entries, skeletix_added, today, args):
    """Fold fb_scan.py's candidates in. Returns (auto_added, needs_review)."""
    if not os.path.exists(FB_CANDIDATES):
        return [], []
    try:
        with open(FB_CANDIDATES, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"! could not read fb candidates: {exc}")
        return [], []

    scanned = payload.get("scanned_at") or ""
    try:
        age = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(scanned)).total_seconds() / 3600
    except ValueError:
        age = 999
    if age > FB_MAX_AGE_HOURS:
        log(f"! fb candidates are {age:.0f}h old — ignoring (is fb_scan.py running?)")
        return [], []

    known = set()
    for entry in entries + [render_entry(e) for e in skeletix_added]:
        known.add((norm_name(entry_field(entry, "headliner")), entry_field(entry, "date")))

    auto, review = [], []
    for cand in payload.get("candidates") or []:
        key = (norm_name(cand.get("headliner")), cand.get("date") or "")
        if key in known:
            continue
        try:
            if dt.date.fromisoformat(cand["date"]) < today:
                continue
        except (KeyError, ValueError):
            continue
        missing = [f for f in FB_REQUIRED if not cand.get(f)]
        if missing:
            review.append((cand, missing))
            continue
        slug = slugify(cand["headliner"])
        poster_rel = f"assets/posters/{slug}.jpg"
        got_poster = False
        if cand.get("image"):
            try:
                got_poster = download_poster(
                    cand["image"], os.path.join(REPO, poster_rel.replace("/", os.sep)), args.dry_run)
            except Exception as exc:
                log(f"    ! poster download failed: {str(exc)[:120]}")
        ev = {
            "headliner": cand["headliner"],
            "support": cand.get("support") or [],
            "date": cand["date"],
            "venue": cand["venue"],
            "address": cand.get("address") or "",
            "doors": cand.get("doors") or "",
            "show": cand.get("show") or "",
            "price": cand["price"],
            "age": cand["age"],
            "tickets": "",  # door-only by definition; Skeletix shows come in via Part A
            "facebook": cand.get("facebook") or "",
            "poster": poster_rel if got_poster else "",
            "event_id": cand.get("fb_id") or f"fb-{slug}-{cand['date']}",
            "poster_url": cand.get("image"),
        }
        log(f"  + NEW (facebook/{cand['source']}): {ev['headliner']} — {ev['date']} @ "
            f"{ev['venue']} ({ev['age']}, {ev['price']})")
        auto.append(ev)
        known.add(key)

    for cand, missing in review:
        log(f"  NEEDS REVIEW (facebook): {cand.get('headliner')} {cand.get('date')} "
            f"@ {cand.get('venue')} — missing {', '.join(missing)}")
    return auto, review


def git(*args, check=True):
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


# --- main -----------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-git", action="store_true", help="write events.js but do not commit/push (testing)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    log("=== refresh start" + (" (dry run)" if args.dry_run else "") + " ===")

    text = read_events_js()
    prefix, entries, suffix = split_entries(text)
    known_ids = {entry_event_id(e) for e in entries if entry_event_id(e)}
    log(f"events.js currently has {len(entries)} shows")

    try:
        result = algolia(PROMOTER_QUERY)
    except Exception as exc:  # network/API trouble: fail quietly, try again next run
        log(f"! Skeletix query failed: {exc}")
        flush_log()
        return 1

    hits = result.get("hits") or []
    log(f"Skeletix index returned {len(hits)} events for '{PROMOTER_QUERY}'")

    today = dt.date.today()
    added = []
    for hit in hits:
        eid = str(hit.get("event_id") or "")
        date = event_date(hit)
        if not date or eid in known_ids:
            continue
        try:
            when = dt.date.fromisoformat(date)
        except ValueError:
            continue
        if when < today:
            continue
        ev = hit_to_event(hit)
        if not ev:
            log(f"  ? could not parse event {eid}: {hit.get('title','')[:60]}")
            continue
        log(f"  + NEW: {ev['headliner']} — {ev['date']} @ {ev['venue']} ({ev['age']}, {ev['price']})")
        download_poster(ev["poster_url"], os.path.join(REPO, ev["poster"].replace("/", os.sep)), args.dry_run)
        added.append(ev)

    # prune long-past shows
    cutoff = today - dt.timedelta(days=PRUNE_AFTER_DAYS)
    kept, pruned = [], []
    for entry in entries:
        d = entry_field(entry, "date")
        try:
            when = dt.date.fromisoformat(d)
        except ValueError:
            kept.append(entry)
            continue
        if when < cutoff:
            pruned.append(entry)
        else:
            kept.append(entry)
    for entry in pruned:
        log(f"  - pruned past show: {entry_field(entry,'headliner')} ({entry_field(entry,'date')})")
        poster = entry_field(entry, "poster")
        path = os.path.join(REPO, poster.replace("/", os.sep))
        if poster and os.path.exists(path) and not args.dry_run:
            try:
                os.remove(path)
            except OSError:
                pass

    # Facebook-only shows (door-only, no Skeletix listing) picked up by fb_scan.py
    fb_added, fb_review = merge_facebook(entries, added, today, args)
    added.extend(fb_added)

    # shows at his venues that our query didn't match — humans decide
    seen_ids = known_ids | {e["event_id"] for e in added}
    try:
        everything = algolia("", hits=400).get("hits") or []
    except Exception:
        everything = []
    maybe = []
    for hit in everything:
        venue = (hit.get("venue") or {}).get("name") or ""
        eid = str(hit.get("event_id") or "")
        date = event_date(hit)
        if venue in HIS_VENUES and eid not in seen_ids and date and date >= today.isoformat():
            maybe.append((date, venue, hit.get("title", "")[:60], eid))
    if maybe:
        log(f"  POSSIBLY MISSING ({len(maybe)} shows at his venues booked by someone, or titled without his name):")
        for date, venue, title, eid in sorted(maybe):
            log(f"      {date} | {venue} | {title} [{eid}]")

    changed = bool(added or pruned)
    if not changed:
        log("no show changes")

    if args.dry_run:
        log(f"[dry-run] would add {len(added)}, prune {len(pruned)}")
        log("=== refresh done ===")
        flush_log()
        return 0

    # rebuild events.js, sorted by date for readability
    blocks = kept + [render_entry(e) for e in added]

    def sort_key(block):
        return entry_field(block, "date") or "9999-99-99"

    blocks.sort(key=sort_key)
    # entries are captured from their opening brace, so re-add the file's indent
    blocks = ["  " + b.strip().rstrip(",") for b in blocks if b.strip()]
    if not blocks:
        log("! refusing to write an empty EVENTS array")
        flush_log()
        return 1
    body = "\n".join(b + "," for b in blocks)  # trailing comma is valid JS
    new_text = prefix + "\n" + body + "\n" + suffix

    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    stamp = stamp[:-2] + ":" + stamp[-2:]
    # LAST_CHECKED moves every run, so the footer proves the refresher is alive;
    # LAST_UPDATED moves only when the listings actually changed.
    checked_line = f'const LAST_CHECKED = "{stamp}";'
    if re.search(r'const LAST_CHECKED = "[^"]*";', new_text):
        new_text = re.sub(r'const LAST_CHECKED = "[^"]*";', checked_line, new_text)
    else:  # constant missing (hand-edited file) — add it next to LAST_UPDATED
        new_text = re.sub(
            r'(const LAST_UPDATED = "[^"]*";)', r"\1\n" + checked_line, new_text, count=1
        )
        log("added missing LAST_CHECKED constant to events.js")
    if changed:
        new_text = re.sub(r'const LAST_UPDATED = "[^"]*";', f'const LAST_UPDATED = "{stamp}";', new_text)

    if new_text == text:
        log("events.js byte-identical, nothing to commit")
        log("=== refresh done ===")
        flush_log()
        return 0

    with open(EVENTS_JS, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    log(f"events.js rewritten: {len(blocks)} shows, LAST_CHECKED={stamp}" + (f", LAST_UPDATED={stamp}" if changed else ""))

    if args.no_git:
        log("--no-git: skipping commit/push")
        log("=== refresh done ===")
        flush_log()
        return 0

    if added:
        subject = "Auto-update shows: " + ", ".join(f"{e['headliner']} ({e['date']})" for e in added)
        if len(subject) > 100:
            subject = f"Auto-update shows: added {len(added)}, pruned {len(pruned)}"
    elif pruned:
        subject = f"Auto-update: prune {len(pruned)} past show(s)"
    else:
        subject = "Auto-check: no show changes"
    # Stage ONLY what this script owns. `git add -A` would sweep up whatever else
    # happens to be in the working tree and commit it under an automated message.
    owned = ["events.js", "assets/posters"]
    try:
        git("add", *owned)
        if not git("status", "--porcelain", "--", *owned):
            log("nothing staged, skipping commit")
        else:
            git("commit", "-m", subject, "-m", "Automated by tools/refresh_events.py")
            git("push")
            log(f"pushed: {subject}")
    except RuntimeError as exc:
        log(f"! git step failed: {exc}")
        flush_log()
        return 1

    log("=== refresh done ===")
    flush_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
