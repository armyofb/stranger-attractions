"""
Facebook side of the Stranger Attractions event refresh.

Skeletix (see refresh_events.py) covers every show with a presale. It does NOT
cover door-only shows, which the promoter announces on Facebook alone — sometimes
as a proper FB event, sometimes as nothing but a post with a flyer.

Facebook has no usable public API here and blocks plain HTTP, so this drives a
real logged-in Chrome via Patchright — the same approach as the AA scraper
(see american-airlines-scraper/WHY_LOCAL_DAEMON_NOT_CLAUDE_ROUTINE.md). It runs
on the NUC, from a residential connection, against a persistent profile.

Output: tools/fb_candidates.json, which refresh_events.py merges. This script
never touches events.js or git itself.

    python tools/fb_scan.py --login     # one-time: log the profile into Facebook
    python tools/fb_scan.py --dry-run   # scan and print, write nothing
    python tools/fb_scan.py             # scan and write fb_candidates.json

IMPORTANT LIMITATION: Facebook will not paginate the post feed under automation.
Only the newest few posts are ever reachable. That is acceptable because a
door-only show is *announced* as the top post — we catch it while it is news. A
post that scrolls away before a scan runs is gone for good, so the scan is
scheduled to run twice a day rather than weekly.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(REPO, "tools", "fb_profile")
OUT_FILE = os.path.join(REPO, "tools", "fb_candidates.json")
LOG_FILE = os.path.join(REPO, "tools", "refresh.log")

PAGE = "https://www.facebook.com/StrangerAttractions"
EVENTS_URL = PAGE + "/upcoming_hosted_events"

# Venues he books. Matching is how we decide a post is actually a show.
VENUES = {
    "state street pub": ("State Street Pub", "243 N State Ave, Indianapolis, IN 46201"),
    "black circle": ("Black Circle", "2201 E 46th St, Indianapolis, IN 46205"),
    "holy ground": ("Holy Ground Studio", "3317 E 10th St, Indianapolis, IN 46201"),
}
TWENTYONE_PLUS_VENUES = ("State Street Pub",)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")

LOG_LINES = []


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] fb: {msg}"
    LOG_LINES.append(line)
    print(line)


def flush_log():
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("\n".join(LOG_LINES) + "\n")
    except OSError:
        pass


# --- browser ---------------------------------------------------------------


def _clean_profile_locks():
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(PROFILE, name))
        except OSError:
            pass


def launch(pw, headless):
    """Persistent real-Chrome context. Real Chrome (channel='chrome') matters:
    Facebook fingerprints bundled Chromium and serves a degraded page."""
    last = None
    for attempt in range(3):
        _clean_profile_locks()
        try:
            return pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE,
                headless=headless,
                channel="chrome",
                viewport={"width": 1400, "height": 1000},
                no_viewport=False,
            )
        except Exception as exc:
            last = exc
            log(f"chrome launch attempt {attempt + 1}/3 failed: {str(exc)[:160]}")
            time.sleep(4)
    raise last


def logged_in(page):
    try:
        return page.evaluate(
            "() => !document.querySelector('input[name=\"pass\"]')"
            " && !/login|checkpoint/.test(location.pathname)"
        )
    except Exception:
        return False


# --- parsing helpers -------------------------------------------------------


def clean(text):
    return ZERO_WIDTH.sub("", text or "").replace(" ", " ")


def find_venue(text):
    low = text.lower()
    for needle, (name, address) in VENUES.items():
        if needle in low:
            return name, address
    return None, None


def parse_date(text, today):
    """Pull a show date out of promoter prose. Returns ISO date or None.

    Handles 'Sunday, September 13th', 'Sept 13', 'September 13th', '9/13'.
    A month/day already past is read as next year.
    """
    text = clean(text)
    m = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        text, re.I,
    )
    month = day = None
    if m:
        month = MONTHS.get(m.group(1).lower())
        day = int(m.group(2))
    else:
        m2 = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
        if m2:
            month, day = int(m2.group(1)), int(m2.group(2))
    if not month or not day or day > 31:
        return None
    year = today.year
    try:
        when = dt.date(year, month, day)
    except ValueError:
        return None
    if when < today:
        try:
            when = dt.date(year + 1, month, day)
        except ValueError:
            return None
    # Sanity: promoters don't announce more than ~18 months out.
    if (when - today).days > 550:
        return None
    return when.isoformat()


def parse_price(text):
    m = re.search(r"\$\s?(\d{1,3})(?:\.\d{2})?", clean(text))
    return f"${m.group(1)}" if m else ""


def parse_age(text, venue):
    if venue in TWENTYONE_PLUS_VENUES:
        return "21 & Over"
    low = clean(text).lower()
    if "21+" in low or "21 and over" in low or "21 & over" in low:
        return "21 & Over"
    if "18+" in low:
        return "18 & Over"
    if "all ages" in low:
        return "All Ages"
    return ""


def parse_time(text):
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b", clean(text), re.I)
    if not m:
        return ""
    hour = int(m.group(1))
    minute = m.group(2) or "00"
    return f"{hour}:{minute} {m.group(3).upper()}"


def parse_lineup(text):
    """Bands named after 'with'. Returns (headliner, support[])."""
    text = clean(text)
    m = re.search(r"\bwith\s+(.+)", text, re.I)
    if not m:
        return None, []
    tail = re.split(r"[!\n]", m.group(1))[0]
    tail = re.split(r"\s+\(", tail)[0]
    parts = [p.strip(" .,:;-–—") for p in re.split(r"\s*(?:,|&|\band\b)\s*", tail)]
    parts = [p for p in parts if p and len(p) <= 40
             and p.lower() not in ("more", "and more", "special guests", "guests", "tba")]
    if not parts:
        return None, []
    return parts[0].upper(), parts[1:]


def lineup_from_links(links, venue):
    """Prefer the post's tagged band pages over prose. Drops the venue, the
    promoter's own page, and anything that reads like UI chrome."""
    junk = re.compile(
        r"^(stranger attractions|state street pub|black circle|holy ground"
        r"|indianapolis|see more|all reactions|most relevant)", re.I)
    bands = []
    for raw in links:
        name = clean(raw).strip(" .,:;-–—!")
        if not name or junk.match(name):
            continue
        if venue and name.lower() in venue.lower():
            continue
        if re.match(r"^[\d\s:apm/&+·•\-]+$", name, re.I):  # times, counts, "21+"
            continue
        if name not in bands:
            bands.append(name)
    if not bands:
        return None, []
    return bands[0].upper(), bands[1:]


def full_size(url):
    """FB serves feed images downscaled via a ctp= param; cstp= carries the
    source dimensions. Swapping one for the other yields the original flyer."""
    if not url:
        return url
    m = re.search(r"cstp=mx(\d+x\d+)", url)
    if not m:
        return url
    return re.sub(r"ctp=s\d+x\d+", "ctp=s" + m.group(1), url)


# --- scanners --------------------------------------------------------------

EVENTS_JS = """
() => {
  const out = {};
  document.querySelectorAll('a[href*="/events/"]').forEach(a => {
    const m = a.href.match(/\\/events\\/(\\d+)/);
    if (!m) return;
    let el = a;
    for (let i = 0; i < 7 && el && el.parentElement; i++) el = el.parentElement;
    const txt = (el ? el.innerText : '') || a.innerText || '';
    if (!out[m[1]] || txt.length > out[m[1]].length) out[m[1]] = txt;
  });
  return out;
}
"""

# Band names in a post are links to their pages, so innerText runs them
# together ("GRAVERIPPER Raider Huntsmen and Nequient"). Return the anchor
# texts separately -- a far more reliable lineup than the prose.
POSTS_JS = """
() => {
  const arts = [...document.querySelectorAll('div[role="article"]')];
  return arts.slice(0, 6).map(a => {
    const imgs = [...a.querySelectorAll('img')].filter(i => i.naturalWidth >= 300);
    imgs.sort((x, y) => y.naturalWidth - x.naturalWidth);
    const links = [...a.querySelectorAll('a')]
      .map(x => (x.innerText || '').trim())
      .filter(t => t && t.length <= 40 && !/^(see more|like|comment|share|\\d+)$/i.test(t));
    return {
      text: (a.innerText || '').slice(0, 2000),
      img: imgs.length ? imgs[0].src : null,
      links: [...new Set(links)].slice(0, 15)
    };
  });
}
"""


def scan_events(page):
    log("scanning FB events page")
    page.goto(EVENTS_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    try:
        raw = page.evaluate(EVENTS_JS)
    except Exception as exc:
        log(f"! events scrape failed: {str(exc)[:140]}")
        return []
    today = dt.date.today()
    out = []
    for eid, text in (raw or {}).items():
        text = clean(text)
        if "Stranger Attractions" not in text and "stranger attractions" not in text.lower():
            continue
        date = parse_date(text, today)
        venue, address = find_venue(text)
        if not date or not venue:
            continue
        title = ""
        for line in text.split("\n"):
            line = line.strip()
            if "presents" in line.lower() or (len(line) > 20 and line.upper() == line):
                title = line
                break
        headliner, support = parse_lineup(title or text)
        if not headliner:
            body = re.sub(r".*?presents\s*", "", title, flags=re.I).strip()
            headliner = re.split(r"\s+(?:w/|with)\s+", body, flags=re.I)[0].strip(" !.,") or None
        if not headliner:
            continue
        out.append({
            "source": "fb-event",
            "fb_id": eid,
            "facebook": f"https://www.facebook.com/events/{eid}/",
            "headliner": headliner.upper(),
            "support": support,
            "date": date,
            "venue": venue,
            "address": address,
            "doors": parse_time(text),
            "show": "",
            "price": parse_price(text),
            "age": parse_age(text, venue),
            "image": None,
        })
    log(f"found {len(out)} parseable events")
    return out


def scan_posts(page):
    log("scanning FB top posts")
    page.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    # expand truncated post bodies
    try:
        page.evaluate(
            "() => { document.querySelectorAll('div[role=\"button\"],span').forEach(b => {"
            " if (/^see more$/i.test((b.innerText||'').trim())) b.click(); }); }"
        )
        page.wait_for_timeout(2500)
    except Exception:
        pass
    try:
        raw = page.evaluate(POSTS_JS)
    except Exception as exc:
        log(f"! posts scrape failed: {str(exc)[:140]}")
        return []
    today = dt.date.today()
    out = []
    for post in raw or []:
        text = clean(post.get("text") or "")
        if len(text) < 40:
            continue
        venue, address = find_venue(text)
        date = parse_date(text, today)
        if not venue or not date:
            continue
        headliner, support = lineup_from_links(post.get("links") or [], venue)
        if not headliner:
            headliner, support = parse_lineup(text)
        if not headliner:
            continue
        out.append({
            "source": "fb-post",
            "fb_id": None,
            "facebook": "",
            "headliner": headliner,
            "support": support,
            "date": date,
            "venue": venue,
            "address": address,
            "doors": parse_time(text),
            "show": "",
            "price": parse_price(text),
            "age": parse_age(text, venue),
            "image": full_size(post.get("img")),
            "text": text[:400],
        })
    log(f"found {len(out)} parseable show posts")
    return out


# --- main ------------------------------------------------------------------


def do_login():
    from patchright.sync_api import sync_playwright

    os.makedirs(PROFILE, exist_ok=True)
    print("Opening Chrome. Log in to Facebook, then close the browser window.")
    with sync_playwright() as pw:
        ctx = launch(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
        while True:
            time.sleep(3)
            try:
                if not ctx.pages:
                    break
            except Exception:
                break
    print("Profile saved. Run `python tools/fb_scan.py --dry-run` to test.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="one-time interactive Facebook login")
    ap.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args()

    if args.login:
        return do_login()

    if not os.path.isdir(PROFILE):
        log("no Facebook profile yet — run: python tools/fb_scan.py --login")
        flush_log()
        return 1

    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        log("patchright not installed (pip install patchright && patchright install chrome)")
        flush_log()
        return 1

    candidates = []
    try:
        with sync_playwright() as pw:
            ctx = launch(pw, headless=not args.headed)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                if not logged_in(page):
                    log("! profile is not logged in to Facebook — run --login")
                    flush_log()
                    return 1
                candidates.extend(scan_events(page))
                candidates.extend(scan_posts(page))
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as exc:
        log(f"! scan failed: {str(exc)[:200]}")
        flush_log()
        return 1

    # de-dupe: same headliner + date
    seen, uniq = set(), []
    for c in candidates:
        key = (c["headliner"].upper(), c["date"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    for c in uniq:
        log(f"  candidate [{c['source']}] {c['headliner']} — {c['date']} @ {c['venue']} "
            f"({c['age'] or '?'}, {c['price'] or '?'})")

    if args.dry_run:
        print(json.dumps(uniq, indent=2)[:4000])
        flush_log()
        return 0

    payload = {"scanned_at": dt.datetime.now().astimezone().isoformat(), "candidates": uniq}
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    log(f"wrote {len(uniq)} candidates to {os.path.basename(OUT_FILE)}")
    flush_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
