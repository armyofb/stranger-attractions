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
import subprocess
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
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}
MONTH_RE = "jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec"

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


def _kill_stray_chrome():
    """Kill Chrome processes still holding OUR profile directory.

    A crashed or interrupted run leaves chrome.exe alive on fb_profile; every
    later launch then dies with "Opening in existing browser session", which
    looks like a Facebook problem but isn't. Matching on the profile path means
    the user's normal Chrome is never touched.
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*fb_profile*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=60)
    except Exception:
        pass


def launch(pw, headless):
    """Persistent real-Chrome context. Real Chrome (channel='chrome') matters:
    Facebook fingerprints bundled Chromium and serves a degraded page."""
    last = None
    for attempt in range(3):
        _kill_stray_chrome()
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
    """True when the profile holds an authenticated Facebook session.

    Tested via the c_user cookie, not the URL: after a successful login Facebook
    often parks on /login/save-device or a similar interstitial, and a path-based
    check would report 'not logged in' forever while the user sat there logged in.
    """
    try:
        for cookie in page.context.cookies("https://www.facebook.com"):
            if cookie.get("name") == "c_user" and cookie.get("value"):
                return True
    except Exception:
        pass
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
    """Pull a show date out of Facebook text. Returns ISO date or None.

    Covers both the promoter's prose ('Sunday, September 13th', 'Sept 13',
    '9/13') and the formats Facebook's own event list uses, which are easy to
    miss: '14 Aug' puts the day FIRST, and near-term events are shown purely
    relatively — 'Today at 19:00', 'Tomorrow at 18:30', 'This Friday at 18:30'.
    A month/day already past is read as next year.
    """
    text = clean(text)
    low = text.lower()

    if re.search(r"\btoday\b", low):
        return today.isoformat()
    if re.search(r"\btomorrow\b", low):
        return (today + dt.timedelta(days=1)).isoformat()
    m = re.search(r"\b(this|next)\s+(" + "|".join(WEEKDAYS) + r")\b", low)
    if m:
        delta = (WEEKDAYS[m.group(2)] - today.weekday()) % 7
        if delta == 0 or m.group(1) == "next":
            delta = delta or 7
        return (today + dt.timedelta(days=delta)).isoformat()

    month = day = None
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + MONTH_RE + r")[a-z]*\.?", low)
    if m:  # "14 Aug" / "4 Dec"
        day, month = int(m.group(1)), MONTHS.get(m.group(2))
    else:
        m = re.search(r"\b(" + MONTH_RE + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
        if m:  # "Sept 13" / "September 13th"
            month, day = MONTHS.get(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", low)
            if m:
                month, day = int(m.group(1)), int(m.group(2))

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

# Climb only while the ancestor still describes ONE event. A fixed number of
# hops overshoots into the shared list container, which hands every event the
# same blob of text (and therefore the first event's date).
EVENTS_JS = """
() => {
  const idOf = el => ((el.href || '').match(/\\/events\\/(\\d+)/) || [])[1];
  const out = {};
  document.querySelectorAll('a[href*="/events/"]').forEach(a => {
    const id = idOf(a);
    if (!id) return;
    let el = a, card = a;
    for (let i = 0; i < 8 && el && el.parentElement; i++) {
      el = el.parentElement;
      const ids = new Set([...el.querySelectorAll('a[href*="/events/"]')]
        .map(idOf).filter(Boolean));
      if (ids.size > 1) break;
      card = el;
    }
    const txt = card.innerText || '';
    if (!out[id] || txt.length > out[id].length) out[id] = txt;
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
    """Read announcement posts off the page body.

    Deliberately NOT scoped to div[role="article"]: on this page those are empty
    shells and the real post text sits elsewhere in the DOM. Posts also render
    collapsed, so "See more" has to be clicked before the body carries the date
    and price at all.

    Post-derived candidates carry no image — the flyer cannot be tied to its post
    reliably from flat body text, and a poster on the wrong show is worse than a
    placeholder. refresh_events.py falls back to the band-name placeholder.
    """
    log("scanning FB top posts")
    page.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)

    for _ in range(4):
        try:
            page.mouse.wheel(0, 1000)
        except Exception:
            break
        page.wait_for_timeout(2000)
        # Real clicks via locators: a synthetic el.click() does not expand these,
        # and without expansion the post body has no date or price to read.
        try:
            more = page.get_by_text("See more", exact=True)
            for i in range(min(more.count(), 6)):
                try:
                    more.nth(i).click(timeout=3000)
                    page.wait_for_timeout(700)
                except Exception:
                    pass
        except Exception:
            pass
        page.wait_for_timeout(1000)

    try:
        body = clean(page.evaluate("() => document.body.innerText || ''"))
    except Exception as exc:
        log(f"! posts scrape failed: {str(exc)[:140]}")
        return []

    today = dt.date.today()
    out = []
    seen = set()
    for match in re.finditer("|".join(re.escape(v) for v in
                                      ("State Street Pub", "Black Circle", "Holy Ground")), body):
        window = body[max(0, match.start() - 250): match.start() + 450]
        venue, address = find_venue(window)
        date = parse_date(window, today)
        price = parse_price(window)
        if not venue or not date or not price:
            continue          # not an announcement, just a mention
        headliner, support = parse_lineup(window)
        if not headliner:
            continue
        key = (headliner.upper(), date)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source": "fb-post",
            "fb_id": None,
            "facebook": "",
            "headliner": headliner,
            "support": support,
            "date": date,
            "venue": venue,
            "address": address,
            "doors": parse_time(window),
            "show": "",
            "price": price,
            "age": parse_age(window, venue),
            "image": None,
            "text": window[:300],
        })
    log(f"found {len(out)} parseable show posts")
    return out


# --- main ------------------------------------------------------------------


def do_login(timeout_s=900):
    """Open a visible browser and wait until the session is actually logged in.

    Detects success and closes itself; earlier versions waited for the user to
    close the window, which left orphaned chrome.exe processes holding the
    profile and made every later launch fail.
    """
    from patchright.sync_api import sync_playwright

    os.makedirs(PROFILE, exist_ok=True)
    print("Opening Chrome — log in to Facebook in that window.")
    print("This closes itself as soon as login is detected. Ctrl+C to abort.\n")
    ok = False
    with sync_playwright() as pw:
        ctx = launch(pw, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + timeout_s
            announced = False
            while time.time() < deadline:
                time.sleep(3)
                try:
                    if not ctx.pages:          # window closed by hand
                        break
                    if logged_in(page):
                        ok = True
                        break
                    if not announced:
                        announced = True
                        print("waiting for login… (page: %s)" % page.url[:70])
                except Exception:
                    break
            if ok:
                print("Login detected — letting cookies settle…")
                time.sleep(4)
        finally:
            try:
                ctx.close()                    # release the profile cleanly
            except Exception:
                pass
    _kill_stray_chrome()
    if ok:
        print("\nProfile saved. Verify with: python tools/fb_scan.py --dry-run")
        return 0
    print("\nDid not detect a logged-in session. Nothing saved; rerun --login.")
    return 1


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
