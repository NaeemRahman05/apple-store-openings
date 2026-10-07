# Apple store openings

Checks jobs.apple.com for openings at three Long Island Apple Stores (Walt Whitman,
Roosevelt Field and Smith Haven) and sends a phone notification through
[ntfy](https://ntfy.sh) when one opens or closes.

This repo is the online backup for a Mac that checks every 15 minutes. GitHub runs the
backup on a 15-minute schedule but in practice only every few hours, which still covers
times the Mac is asleep or off. Both send to the same ntfy topic and skip anything the
other sent in the last 12 hours.

It catches both kinds of public retail posting:

- postings tagged to the store or its town, such as "US-Genius" at Smith Haven, and
- nationwide roles such as "US - Specialist: Seasonal, Part-time", which the site lists as
  "United States" but hires store by store. For these it asks the site's store picker
  which stores currently have an opening.

Internal-only postings on careers.apple.com are not visible to it.

## How it runs

- `.github/workflows/check.yml` runs `cloud.py` on a schedule.
- `cloud.py` runs the checks in `apple_jobs_monitor.py` (the same code as the Mac version)
  and pushes changes to the ntfy topic stored in the `NTFY_TOPIC` repository secret.
- `state.json` holds the openings already seen. The workflow commits it back when it changes.

To send a test notification, run the workflow from the Actions tab with "Send a test
notification" checked.
