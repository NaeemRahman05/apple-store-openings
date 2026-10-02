#!/usr/bin/env python3
"""Run the store-openings check in GitHub Actions and push changes to a phone through ntfy.

Uses the same checking logic as the Mac monitor (apple_jobs_monitor.py); only the
notifications and where state is kept differ. State lives in state.json in this
repo, which the workflow commits back after each run.

Usage:
  NTFY_TOPIC=... python3 cloud.py          check once
  NTFY_TOPIC=... python3 cloud.py --test   send a test notification
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import apple_jobs_monitor as monitor

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
NTFY_URL = "https://ntfy.sh/"
MAC_ONLY_FIELDS = {"reminder", "phone_alert_pending"}


def push(title, message, click=None, priority=3, tags=()):
    body = {
        "topic": os.environ["NTFY_TOPIC"],
        "title": title,
        "message": message,
        "priority": priority,
        "tags": list(tags),
    }
    if click:
        body["click"] = click
        body["actions"] = [{"action": "view", "label": "Open posting", "url": click}]
    req = urllib.request.Request(NTFY_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return
        except OSError:
            if attempt == 2:
                raise  # fail the run so GitHub emails about it rather than silently dropping an alert
            time.sleep(5 * (attempt + 1))


def push_new(new_jobs):
    for j in monitor.merge_by_posting(new_jobs):
        stores = ", ".join(monitor.store_name(s) for s in j["stores"])
        push(f"New opening · {stores}", f"{j['title']}\n{j['team']}", click=j["url"], priority=4, tags=["briefcase"])


def push_notice(title, message):
    if "needs attention" in title:
        message = "Online checks keep failing. See the Actions tab of the apple-store-openings repo on GitHub."
    push(title, message, priority=2)


def save_state(state):
    # Only what's needed to spot changes, so the repo gets a commit when openings change, not every run.
    keep = {
        "consecutive_failures": state.get("consecutive_failures", 0),
        "jobs": {k: {f: v for f, v in j.items() if f not in MAC_ONLY_FIELDS} for k, j in state["jobs"].items()},
    }
    STATE_FILE.write_text(json.dumps(keep, indent=2, sort_keys=True) + "\n")


def main():
    if not os.environ.get("NTFY_TOPIC"):
        sys.exit("NTFY_TOPIC is not set")
    if "--test" in sys.argv:
        push("Apple Jobs Monitor", "The online checker is connected. New openings will show up here.", tags=["white_check_mark"])
        print("Sent a test notification.")
        return 0

    monitor.STATE_FILE = STATE_FILE
    monitor.LOG_FILE = HERE / "monitor.log"  # not written; GitHub keeps each run's output
    monitor.PHONE_ALERTS = False
    monitor.save_state = save_state
    monitor.alert_new_jobs = push_new
    monitor.banner = push_notice
    monitor.refresh_dashboard = lambda state: None
    monitor.check()
    # A failed check is recorded in state.json and pushes a warning after repeated failures;
    # exiting 0 keeps GitHub from emailing about every network blip.
    return 0


if __name__ == "__main__":
    sys.exit(main())
