# Apple store openings

Checks jobs.apple.com every 15 minutes for openings at three Long Island Apple Stores
(Walt Whitman, Roosevelt Field and Smith Haven) and sends a phone notification through
[ntfy](https://ntfy.sh) when one opens or closes.

It catches both kinds of retail posting:

- postings tagged to the store or its town, such as "US-Genius" at Smith Haven, and
- nationwide roles such as "US - Specialist: Seasonal, Part-time", which the site lists as
  "United States" but hires store by store. For these it asks the site's store picker
  which stores currently have an opening.

## How it runs

- `.github/workflows/check.yml` runs `cloud.py` on a schedule.
- `cloud.py` runs the checks in `apple_jobs_monitor.py` (the same code as the Mac version)
  and pushes changes to the ntfy topic stored in the `NTFY_TOPIC` repository secret.
- `state.json` holds the openings already seen. The workflow commits it back when it changes.

To send a test notification, run the workflow from the Actions tab with "Send a test
notification" checked.
