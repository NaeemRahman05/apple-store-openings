#!/usr/bin/env python3
"""Run the store-openings check in GitHub Actions, as a backup for when the Mac is off.

Uses the same checking and ntfy push code as the Mac monitor (apple_jobs_monitor.py),
which reads the ntfy topic from NTFY_TOPIC and skips pushes the Mac already sent.
State lives in state.json in this repo, which the workflow commits back when it changes.

Usage:
  NTFY_TOPIC=... python3 cloud.py          check once
  NTFY_TOPIC=... python3 cloud.py --test   send a test notification
"""

import json
import os
import sys
from pathlib import Path

import apple_jobs_monitor as monitor

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
MAC_ONLY_FIELDS = {"reminder", "phone_alert_pending"}


def failure_notice(title, message):
    # check() calls banner() for closures too; those already went out as pushes.
    if "needs attention" in title:
        monitor.push(os.environ["NTFY_TOPIC"], title,
                     "Online checks keep failing. See the Actions tab of the apple-store-openings repo on GitHub.",
                     priority=2)


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
        monitor.push(os.environ["NTFY_TOPIC"], "Apple Jobs Monitor",
                     "The online checker is connected. New openings will show up here.", tags=["white_check_mark"])
        print("Sent a test notification.")
        return 0

    monitor.STATE_FILE = STATE_FILE
    monitor.LOG_FILE = HERE / "monitor.log"  # not written; GitHub keeps each run's output
    monitor.PHONE_ALERTS = False
    monitor.save_state = save_state
    monitor.alert_new_jobs = lambda jobs: None  # the Mac's pop-up; new openings are pushed by check()
    monitor.banner = failure_notice
    monitor.refresh_dashboard = lambda state: None
    monitor.check()
    # A failed check is recorded in state.json and pushes a warning after repeated failures;
    # exiting 0 keeps GitHub from emailing about every network blip.
    return 0


if __name__ == "__main__":
    sys.exit(main())
