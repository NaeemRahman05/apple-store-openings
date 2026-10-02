#!/usr/bin/env python3
"""Watch Apple's job site for openings at specific Long Island Apple Stores.

Pops up an alert when a job opens at Walt Whitman (Huntington Station), Roosevelt
Field (Garden City) or Smith Haven (Lake Grove), and shows a notification when one
closes. Openings are found two ways:

  * Postings tagged to the store or its town (e.g. "US-Genius" at Smith Haven).
  * Nationwide retail roles (e.g. "US - Specialist: Seasonal, Part-time") that
    jobs.apple.com lists as "United States" but hire per store. For these, the
    site's own store picker says which stores currently have an opening.

Other nationwide postings (internships etc.) show up in every city's search and
are ignored.

Each opening is also added to an "Apple Jobs" list in Reminders, which syncs to
iPhone through iCloud (new ones ring an alert there) and can be read by Siri.
After every check, a dashboard page is rewritten with the countdown to the next
check, check history and current openings.

Usage:
  python3 apple_jobs_monitor.py              check once now
  python3 apple_jobs_monitor.py --dashboard  open the dashboard
  python3 apple_jobs_monitor.py --status     show tracked openings and last check
  python3 apple_jobs_monitor.py --install    check every 15 minutes in the background
  python3 apple_jobs_monitor.py --uninstall  stop background checks
  python3 apple_jobs_monitor.py --test-alert show a sample alert
  python3 apple_jobs_monitor.py --test-phone add a test reminder that alerts on iPhone
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# (store label shown in alerts, jobs.apple.com search for the store's town, Apple store id)
LOCATIONS = [
    ("Walt Whitman (Huntington Station)", "huntington-station-HSN", "R068"),
    ("Roosevelt Field (Garden City)", "garden-city-GAR", "R060"),
    ("Smith Haven (Lake Grove)", "lake-grove-C51", "R139"),
]
# Store picker lookups for nationwide roles: all three stores are within this radius.
STORE_SEARCH_ZIP = "11746"
STORE_SEARCH_MILES = 30

CHECK_EVERY_MINUTES = 15  # checks run on the clock (:00, :15, :30, :45), and right after the Mac wakes
FAILURES_BEFORE_WARNING = 8  # ~2 hours of failed checks at the default interval
MISSES_BEFORE_CLOSED = 2  # an opening must be absent this many checks in a row to count as closed
HISTORY_LIMIT = 96  # checks kept for the dashboard (24 hours)
EVENTS_LIMIT = 50  # new/closed events kept for the dashboard

PHONE_ALERTS = True  # mirror openings into Reminders so they reach iPhone and Siri
REMINDERS_LIST = "Apple Jobs"
# iPhone alerts come from the online checker (CLOUD_REPO, via ntfy), which also runs while the
# Mac sleeps, so new reminders are added quietly instead of ringing a second alert.
REMINDER_ALARMS = False
CLOUD_REPO = "NaeemRahman05/apple-store-openings"  # shown on the dashboard; None to hide

BASE_URL = "https://jobs.apple.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HYDRATION_RE = re.compile(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);", re.S)

APP_DIR = Path.home() / "Library" / "Application Support" / "AppleJobsMonitor"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "monitor.log"
DASHBOARD_FILE = APP_DIR / "dashboard.html"
TEMPLATE_NAME = "dashboard_template.html"  # lives next to this script
LAUNCH_LABEL = "local.apple-jobs-monitor"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
LAUNCHER_APP = Path.home() / "Applications" / "Apple Jobs Monitor.app"


class PageFormatError(Exception):
    """Apple's site no longer has the data layout this script expects."""


class IncompleteResults(Exception):
    """A search returned fewer postings than it said it had."""


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- fetching

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT, "Accept-Language": "en-US"}, **(headers or {})))
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
            time.sleep(5 * (attempt + 1))
    raise last_error


def fetch_search_page(slug, page):
    # The site occasionally serves a page with no results; retry before believing it.
    for attempt in range(3):
        search = fetch_search_page_once(slug, page)
        if search["searchResults"]:
            break
        time.sleep(5)
    return search


def fetch_search_page_once(slug, page):
    url = f"{BASE_URL}/en-us/search?location={slug}&page={page}"
    m = HYDRATION_RE.search(http_get(url))
    if not m:
        raise PageFormatError(f"no job data found on {url}")
    try:
        search = json.loads(json.loads(m.group(1)))["loaderData"]["search"]
        search["searchResults"], search["totalRecords"]
    except (ValueError, KeyError, TypeError) as e:
        raise PageFormatError(f"unexpected job data on {url}: {e!r}")
    return search


def fetch_location(slug):
    """Return (location filter info, every posting in this location's search)."""
    first = fetch_search_page(slug, 1)
    total = first["totalRecords"]
    results = list(first["searchResults"])
    page = 1
    while len(results) < total and page < 25:
        page += 1
        batch = fetch_search_page(slug, page)["searchResults"]
        if not batch:
            break
        results.extend(batch)
    if len(results) < total:
        raise IncompleteResults(f"{slug}: got {len(results)} of {total} postings")
    location = (first.get("filters", {}).get("locations") or [{}])[0]
    return location, results


def fetch_open_stores(position_id):
    """Store ids with a current opening for a nationwide role that hires per store."""
    query = urllib.parse.urlencode({
        "jobId": position_id,
        "searchField": "zipCode",
        "fieldValue": STORE_SEARCH_ZIP,
        "milesRange": STORE_SEARCH_MILES,
    })
    url = f"{BASE_URL}/api/v1/storeLocations?{query}"
    try:
        stores = json.loads(http_get(url, {"locale": "en_US"}))["res"]
        return {s["locationId"].replace("postLocation-", "") for s in stores if s.get("currentOpening")}
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise PageFormatError(f"unexpected store data from {url}: {e!r}")


def is_local(job, location, store_id):
    """True if the posting is tagged to this town or its store rather than a broader area.

    A town's search also returns postings tagged with a parent area (the whole US,
    New York State, a metro area). Postings tagged with the town itself or a store
    in it (level 5 = town, level 6 = store) are local.
    """
    ids = {location.get("id"), f"postLocation-{store_id}"} - {None}
    locs = job.get("locations") or []
    if not locs:
        return False
    if any(loc.get("postLocationId") in ids for loc in locs):
        return True
    # Nothing broader than a town is tagged, so Apple matched it through this town or its store.
    return all(loc.get("level", 0) >= 5 for loc in locs)


def job_url(job):
    return f"{BASE_URL}/en-us/details/{job['positionId']}/{job.get('transformedPostingTitle', '')}"


def fetch_local_jobs():
    """Return {opening key: details} for every current opening at the watched stores."""
    jobs = {}

    def add(key, job, label):
        entry = jobs.setdefault(key, {
            "title": job.get("postingTitle", "").strip(),
            "team": (job.get("team") or {}).get("teamName", ""),
            "posted": job.get("postingDate", ""),
            "url": job_url(job),
            "stores": [],
        })
        if label not in entry["stores"]:
            entry["stores"].append(label)

    per_store_roles = {}
    for label, slug, store_id in LOCATIONS:
        location, results = fetch_location(slug)
        local = 0
        for j in results:
            if is_local(j, location, store_id):
                add(j["positionId"], j, label)
                local += 1
            elif j.get("managedPipelineRole"):
                per_store_roles[j["positionId"]] = j
        log(f"{label}: {len(results)} listed, {local} tagged to the store/town")

    for pid, j in per_store_roles.items():
        open_stores = fetch_open_stores(pid)
        here = [label for label, _, store_id in LOCATIONS if store_id in open_stores]
        for label, _, store_id in LOCATIONS:
            if store_id in open_stores:
                add(f"{pid}@{store_id}", j, label)  # one opening per store, so each can close separately
        log(f"Nationwide role '{j.get('postingTitle', '').strip()}': open at {', '.join(here) or 'none of the watched stores'}")
    return jobs


# ---------------------------------------------------------------- alerts

def osascript(lines, args, wait=True):
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    cmd += [str(a) for a in args]
    if wait:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # Detach so the check can finish while the alert waits on screen.
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def banner(title, message):
    osascript([
        "on run argv",
        'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
        "end run",
    ], [title, message])


def merge_by_posting(openings):
    """Collapse per-store openings of the same posting into one entry listing all its stores."""
    merged = {}
    for j in openings:
        entry = merged.setdefault(j["url"], dict(j, stores=[]))
        entry["stores"] += [s for s in j["stores"] if s not in entry["stores"]]
    return list(merged.values())


def alert_new_jobs(new_jobs):
    postings = merge_by_posting(new_jobs)
    count = len(postings)
    title = "New Apple job opening" if count == 1 else f"{count} new Apple job openings"
    body = "\n\n".join(
        f"{j['title']}\n{j['team']} · {', '.join(j['stores'])}\nPosted {j['posted']}" for j in postings
    )
    urls = [j["url"] for j in postings][:5]
    button = "Open Posting" if count == 1 else "Open Postings"
    subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    osascript([
        "on run argv",
        "activate",
        f'set r to display dialog (item 2 of argv) with title (item 1 of argv) buttons {{"Dismiss", "{button}"}} '
        f'default button "{button}" with icon note giving up after 86400',
        f'if button returned of r is "{button}" then',
        "repeat with i from 3 to count of argv",
        "open location (item i of argv)",
        "end repeat",
        "end if",
        "end run",
    ], [title, body] + urls, wait=False)


# ---------------------------------------------------------------- phone (Reminders)

# Arguments: list name, then (operation, reminder name, notes) triples.
# "alarm" adds a reminder that alerts shortly (iCloud carries it to iPhone first),
# "add" adds one quietly, "complete" checks off open reminders with that name.
REMINDERS_SCRIPT = [
    "on run argv",
    "set listName to item 1 of argv",
    'set wasRunning to application "Reminders" is running',
    'tell application "Reminders"',
    "if not (exists list listName) then make new list with properties {name:listName}",
    "set theList to list listName",
    "set i to 2",
    "repeat while i + 2 <= (count of argv)",
    "set op to item i of argv",
    "set theName to item (i + 1) of argv",
    "set theBody to item (i + 2) of argv",
    'if op is "alarm" then',
    "set d to (current date) + 90",
    "tell theList to make new reminder with properties {name:theName, body:theBody, due date:d, remind me date:d}",
    'else if op is "add" then',
    "tell theList to make new reminder with properties {name:theName, body:theBody}",
    'else if op is "complete" then',
    "repeat with r in (every reminder of theList whose name is theName and completed is false)",
    "set completed of r to true",
    "end repeat",
    "end if",
    "set i to i + 3",
    "end repeat",
    "end tell",
    "if not wasRunning then",
    "delay 2",
    'tell application "Reminders" to quit',
    "end if",
    'return "ok"',
    "end run",
]


def run_reminders(ops):
    """Apply (op, name, notes) operations to the Reminders list. Returns an error message or None."""
    args = ["osascript"] + [a for line in REMINDERS_SCRIPT for a in ("-e", line)] + [REMINDERS_LIST]
    for op, name, notes in ops:
        args += [op, name, notes]
    try:
        # macOS asks once for permission to control Reminders; the AppleEvent waits up to 2 min for an answer.
        result = subprocess.run(args, capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        return "Reminders didn't respond (is a permission prompt waiting?)"
    if result.returncode != 0:
        err = result.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            return "not allowed to control Reminders (System Settings > Privacy & Security > Automation)"
        return err or f"osascript exited with {result.returncode}"
    return None


def store_name(label):
    return label.split(" (")[0]


def reminder_name(job):
    return f"{job['title']} · {', '.join(store_name(s) for s in job['stores'])}"


def reminder_notes(job):
    return f"{job['team']} · Posted {job['posted']}\n{job['url']}"


def sync_phone(state, closed_jobs):
    """Mirror tracked openings into Reminders: add missing ones, check off closed ones."""
    pending = []
    for job in state["jobs"].values():
        if not job.get("reminder"):
            op = "alarm" if REMINDER_ALARMS and job.get("phone_alert_pending") else "add"
            pending.append((job, (op, reminder_name(job), reminder_notes(job))))
    closing = [("complete", j["reminder"], "") for j in closed_jobs if j.get("reminder")]
    if not pending and not closing:
        return
    error = run_reminders([op for _, op in pending] + closing)
    if error:
        state["phone"] = {"status": "error", "detail": error}
        log(f"Phone alerts: {error}")
        return
    for job, (_, name, _) in pending:
        job["reminder"] = name
        job.pop("phone_alert_pending", None)
    state["phone"] = {"status": "connected", "detail": None}
    log(f"Phone alerts: {len(pending)} reminder(s) added, {len(closing)} checked off")


# ---------------------------------------------------------------- dashboard

def write_dashboard(state):
    template = (Path(__file__).resolve().parent / TEMPLATE_NAME).read_text()
    stores = []
    for label, _, _ in LOCATIONS:
        name, _, town = label.partition(" (")
        openings = [
            # Per-store openings of nationwide roles have "@store" keys; their posting date is the
            # nationwide listing's, which Apple refreshes constantly, so the page shows first_seen instead.
            dict({k: j.get(k) for k in ("title", "team", "posted", "url", "first_seen")}, per_store="@" in key)
            for key, j in state.get("jobs", {}).items() if label in j.get("stores", [])
        ]
        openings.sort(key=lambda j: j.get("first_seen") or "", reverse=True)
        stores.append({"name": name, "town": town.rstrip(")"), "openings": openings})
    data = {
        "interval_sec": CHECK_EVERY_MINUTES * 60,
        "schedule_anchor": state.get("schedule_anchor") or state.get("last_started"),
        "last_check": state.get("last_check"),
        "last_duration": state.get("last_duration"),
        "consecutive_failures": state.get("consecutive_failures", 0),
        "last_error_reason": state.get("last_error_reason"),
        "stores": stores,
        "history": state.get("history", []),
        "events": state.get("events", []),
        "phone": dict(state.get("phone") or {"status": "pending"}, enabled=PHONE_ALERTS),
        "log_path": str(LOG_FILE),
        "reminders_list": REMINDERS_LIST,
        "cloud_repo": CLOUD_REPO,
    }
    html = template.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DASHBOARD_FILE.with_suffix(".tmp")
    tmp.write_text(html)
    tmp.replace(DASHBOARD_FILE)


# ---------------------------------------------------------------- state

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None


def save_state(state):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def trim_log():
    try:
        if LOG_FILE.stat().st_size > 1_000_000:
            data = LOG_FILE.read_bytes()[-500_000:]
            LOG_FILE.write_bytes(data[data.find(b"\n") + 1:])
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- commands

def record_check(state, started, ok):
    history = state.setdefault("history", [])
    history.append({"t": started, "ok": ok})
    del history[:-HISTORY_LIMIT]


def record_events(state, kind, jobs, when):
    events = state.setdefault("events", [])
    for j in merge_by_posting(jobs):
        events.append({"t": when, "type": kind, "title": j["title"], "stores": [store_name(s) for s in j["stores"]]})
    del events[:-EVENTS_LIMIT]


def refresh_dashboard(state):
    try:
        write_dashboard(state)
    except Exception as e:  # the dashboard is a convenience; never let it break a check
        log(f"Dashboard not updated: {e!r}")


def wait_for_network(host="jobs.apple.com", timeout=90):
    """A check that starts the moment the Mac wakes can beat Wi-Fi reconnecting; give it a moment."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return
        except OSError:
            if time.monotonic() > deadline:
                return  # let the real request fail and be recorded
            time.sleep(5)


def check():
    trim_log()
    wait_for_network()
    started = datetime.now()
    now = started.isoformat(timespec="seconds")
    state = load_state()
    first_run = state is None
    state = state or {"jobs": {}, "consecutive_failures": 0}
    state["last_started"] = now
    if os.environ.get("XPC_SERVICE_NAME") == LAUNCH_LABEL:
        state["schedule_anchor"] = now  # launchd times the next run from this one

    try:
        current = fetch_local_jobs()
    except Exception as e:  # keep the previous snapshot; a partial fetch would look like closures
        reason = "Apple's site layout changed" if isinstance(e, PageFormatError) else "Couldn't reach jobs.apple.com"
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state.update(last_error=f"{now}: {e!r}", last_error_reason=reason)
        log(f"Check failed ({state['consecutive_failures']} in a row): {e!r}")
        if state["consecutive_failures"] == FAILURES_BEFORE_WARNING:
            banner("Apple job monitor needs attention", f"Checks keep failing ({reason}). See {LOG_FILE}")
        record_check(state, now, ok=False)
        save_state(state)
        refresh_dashboard(state)
        return 1

    tracked = state["jobs"]
    new_jobs, closed_jobs = [], []
    for key, job in current.items():
        if key in tracked:
            tracked[key].update(job, missed=0)
        else:
            tracked[key] = dict(job, first_seen=now, missed=0, phone_alert_pending=True)
            new_jobs.append(tracked[key])
    for key in list(tracked):
        if key not in current:
            tracked[key]["missed"] = tracked[key].get("missed", 0) + 1
            if tracked[key]["missed"] >= MISSES_BEFORE_CLOSED:
                closed_jobs.append(tracked.pop(key))

    record_events(state, "new", new_jobs, now)
    record_events(state, "closed", closed_jobs, now)
    state.update(
        last_check=now,
        last_duration=round((datetime.now() - started).total_seconds()),
        consecutive_failures=0,
        last_error=None,
        last_error_reason=None,
    )
    record_check(state, now, ok=True)
    save_state(state)

    if first_run:
        log(f"First run: now tracking {len(current)} opening(s).")
    for j in new_jobs:
        log(f"NEW: {j['title']} ({', '.join(j['stores'])}) {j['url']}")
    for j in closed_jobs:
        log(f"CLOSED: {j['title']} ({', '.join(j['stores'])})")
    if new_jobs:
        alert_new_jobs(new_jobs)
    if closed_jobs:
        names = "; ".join(f"{j['title']} ({', '.join(j['stores'])})" for j in closed_jobs)
        banner("Apple job opening closed", names)
    if not new_jobs and not closed_jobs:
        log(f"No changes ({len(tracked)} opening(s) tracked).")

    if PHONE_ALERTS:
        sync_phone(state, closed_jobs)
        save_state(state)
    refresh_dashboard(state)
    return 0


def status():
    state = load_state()
    if not state:
        print("No checks have run yet.")
        return 0
    print(f"Last successful check: {state.get('last_check') or 'never'}")
    if state.get("consecutive_failures"):
        print(f"Recent failures: {state['consecutive_failures']} (last: {state.get('last_error')})")
    print(f"Background checks: {'installed' if PLIST_PATH.exists() else 'not installed'}")
    phone = state.get("phone") or {}
    if PHONE_ALERTS:
        print(f"Phone alerts: Reminders list '{REMINDERS_LIST}' - {phone.get('status', 'not set up yet')}"
              + (f" ({phone['detail']})" if phone.get("detail") else ""))
    print(f"Watching: {', '.join(label for label, _, _ in LOCATIONS)}")
    postings = merge_by_posting(state.get("jobs", {}).values())
    print(f"\nOpen postings ({len(postings)}):")
    for j in postings:
        print(f"  - {j['title']} [{', '.join(j['stores'])}] posted {j['posted']}\n    {j['url']}")
    if not postings:
        print("  (none right now)")
    print(f"\nDashboard: {DASHBOARD_FILE}\nLog:       {LOG_FILE}")
    return 0


def make_launcher(script):
    """An app in ~/Applications (findable in Spotlight, keepable in the Dock) that opens the dashboard."""
    LAUNCHER_APP.parent.mkdir(exist_ok=True)
    if LAUNCHER_APP.exists():
        shutil.rmtree(LAUNCHER_APP)
    command = f'do shell script quoted form of "{sys.executable}" & " " & quoted form of "{script}" & " --dashboard"'
    subprocess.run(["osacompile", "-o", str(LAUNCHER_APP), "-e", command], check=True)
    icon = script.parent / "AppIcon.icns"
    if icon.exists():
        shutil.copy2(icon, LAUNCHER_APP / "Contents" / "Resources" / "applet.icns")
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(LAUNCHER_APP)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    target = APP_DIR / "apple_jobs_monitor.py"
    if here != APP_DIR.resolve():
        shutil.copy2(__file__, target)
        for extra in (TEMPLATE_NAME, "AppIcon.icns"):
            if (here / extra).exists():
                shutil.copy2(here / extra, APP_DIR / extra)
    make_launcher(target)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump({
            "Label": LAUNCH_LABEL,
            "ProgramArguments": [sys.executable, str(target)],
            # Calendar times (unlike a plain interval) make launchd run a missed check as soon as the Mac wakes.
            "StartCalendarInterval": [{"Minute": m} for m in range(0, 60, CHECK_EVERY_MINUTES)],
            "RunAtLoad": True,
            "AbandonProcessGroup": True,  # let the alert dialog outlive the check
            "StandardOutPath": str(LOG_FILE),
            "StandardErrorPath": str(LOG_FILE),
        }, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST_PATH)], check=True)
    print(f"Installed. Checking every {CHECK_EVERY_MINUTES} minutes (first check running now).")
    print(f"Dashboard app: {LAUNCHER_APP}")
    print(f"Script: {target}\nLog:    {LOG_FILE}")
    return 0


def uninstall():
    if PLIST_PATH.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)], stderr=subprocess.DEVNULL)
        PLIST_PATH.unlink()
        print("Background checks stopped and removed.")
    else:
        print("Background checks were not installed.")
    if LAUNCHER_APP.exists():
        shutil.rmtree(LAUNCHER_APP)
        print("Removed the dashboard app.")
    print(f"Saved data is still in {APP_DIR} (delete that folder to remove it).")
    if PHONE_ALERTS:
        print(f"The '{REMINDERS_LIST}' list in Reminders was left as is.")
    return 0


def open_dashboard():
    write_dashboard(load_state() or {})
    subprocess.run(["open", str(DASHBOARD_FILE)])
    return 0


def test_alert():
    alert_new_jobs([{
        "title": "US-Technical Specialist (TEST ALERT)",
        "team": "Apple Retail",
        "posted": datetime.now().strftime("%b %d, %Y"),
        "stores": [LOCATIONS[0][0]],
        "url": f"{BASE_URL}/en-us/search?location={LOCATIONS[0][1]}",
    }])
    return 0


def test_phone():
    error = run_reminders([(
        "alarm",
        "Apple Jobs Monitor test",
        "If this alerted on your iPhone, phone alerts are working. You can delete this reminder.",
    )])
    state = load_state()
    if state is not None:
        state["phone"] = {"status": "error", "detail": error} if error else {"status": "connected", "detail": None}
        save_state(state)
    if error:
        print(f"Couldn't add the test reminder: {error}")
        return 1
    print(f"Added a test reminder to the '{REMINDERS_LIST}' list. It should alert on your iPhone in about 90 seconds.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Alert on new Apple job openings at specific stores.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dashboard", action="store_true", help="open the dashboard")
    group.add_argument("--status", action="store_true", help="show tracked openings and last check")
    group.add_argument("--install", action="store_true", help="check in the background every few minutes")
    group.add_argument("--uninstall", action="store_true", help="stop background checks")
    group.add_argument("--test-alert", action="store_true", help="show a sample new-job alert")
    group.add_argument("--test-phone", action="store_true", help="add a test reminder that alerts on iPhone")
    args = parser.parse_args()
    if args.dashboard:
        return open_dashboard()
    if args.status:
        return status()
    if args.install:
        return install()
    if args.uninstall:
        return uninstall()
    if args.test_alert:
        return test_alert()
    if args.test_phone:
        return test_phone()
    return check()


if __name__ == "__main__":
    sys.exit(main())
