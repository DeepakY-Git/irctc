# IRCTC Current Availability Alert Setup

This project contains a single headless Python worker that renders a tracking page in Chromium, parses the rendered HTML with BeautifulSoup, and sends a high-priority Pushover alert when availability opens up.

## Files

- [irctc_agent_alert.py](irctc_agent_alert.py)
- [streamlit_app.py](streamlit_app.py)
- [requirements.txt](requirements.txt)

## Runtime Prerequisites

Install Python 3.10+ and create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Local UI

Run the dashboard locally with Streamlit:

```bash
streamlit run streamlit_app.py
```

The UI auto-refreshes every 90 seconds while monitoring is enabled. When the parser finds one or more current-availability cards, it sends the configured Pushover alert and writes the local duplicate-suppression file.

## Environment Variables

Set these values before starting the script:

- `TRAIN_NUMBER`: Train number to monitor.
- `SOURCE_STATION`: Source station code.
- `DESTINATION_STATION`: Destination station code.
- `TRAVEL_DATE`: Journey date in `YYYY-MM-DD`.
- `TRAVEL_CLASS`: Travel class, for example `SL`, `3A`, `2A`.
- `QUOTA`: Booking quota, default `GN`.
- `TRACKING_URL`: HTML page URL or URL template to scrape. You can include placeholders like `{train_number}`, `{source}`, `{destination}`, `{travel_date}`, `{travel_class}`, and `{quota}`.
- If you set `TRACKING_URL` to `https://www.confirmtkt.com` or `https://confirmtkt.com`, the script will build the route URL dynamically from `SOURCE_STATION`, `DESTINATION_STATION`, and `TRAVEL_DATE`.
- `USE_BROWSER_RENDERING`: Set to `1` to render the page with Playwright before parsing, which is the recommended mode for client-rendered pages like ConfirmTkt.
- `BROWSER_WAIT_SECONDS`: Extra wait time after the page loads, default `5`.
- `PUSHOVER_USER_KEY`: Pushover user key.
- `PUSHOVER_APP_TOKEN`: Pushover application token.
- `PUSHOVER_DEVICE`: Optional Pushover device target.
- `ALERT_STATE_FILE`: Optional override for the duplicate-suppression file, default `alert_fired.txt`.
- `HTTP_TIMEOUT_SECONDS`: Optional HTTP timeout, default `20`.
- `CHECK_INTERVAL_SECONDS`: Optional polling interval, default `90`.

Example shell exports:

```bash
export TRAIN_NUMBER="12345"
export SOURCE_STATION="NDLS"
export DESTINATION_STATION="BCT"
export TRAVEL_DATE="2026-06-18"
export TRAVEL_CLASS="SL"
export QUOTA="GN"
export TRACKING_URL="https://your-site.example/search?train={train_number}&from={source}&to={destination}&date={travel_date}&class={travel_class}&quota={quota}"
export USE_BROWSER_RENDERING="1"
export PUSHOVER_USER_KEY="your_pushover_user_key"
export PUSHOVER_APP_TOKEN="your_pushover_app_token"
```

If you want the ConfirmTkt home page flow, set `TRACKING_URL` to `https://www.confirmtkt.com` and keep `SOURCE_STATION`, `DESTINATION_STATION`, and `TRAVEL_DATE` filled. The script will construct the same route the site’s search button would navigate to.

## How the Scheduler Works

The script polls the tracking page every 90 seconds by default and stops only when current availability is detected or you interrupt it. Because cron is minute-based, the most reliable way to keep the cadence is to let the script run as a long-lived process.

For ConfirmTkt and similar pages, keep `USE_BROWSER_RENDERING=1` so Chromium executes the client-side JavaScript before BeautifulSoup reads the DOM.

## Run Manually

```bash
source .venv/bin/activate
python irctc_agent_alert.py \
  --train-number "$TRAIN_NUMBER" \
  --source "$SOURCE_STATION" \
  --destination "$DESTINATION_STATION" \
  --travel-date "$TRAVEL_DATE" \
  --travel-class "$TRAVEL_CLASS" \
  --quota "$QUOTA" \
  --tracking-url "$TRACKING_URL"
```

If you are testing ConfirmTkt specifically, point `TRACKING_URL` at the route page and leave browser rendering enabled.

## Cron Option

If you want cron to launch the worker headless, schedule it to start shortly before the active chart window and let the script self-manage the 90-second polling cadence.

Example cron entry:

```cron
0 21 * * * /bin/bash -lc "cd /workspaces/irctc && source .venv/bin/activate && python irctc_agent_alert.py --train-number \"$TRAIN_NUMBER\" --source \"$SOURCE_STATION\" --destination \"$DESTINATION_STATION\" --travel-date \"$TRAVEL_DATE\" --travel-class \"$TRAVEL_CLASS\" --quota \"$QUOTA\" --tracking-url \"$TRACKING_URL\" >> irctc_alert.log 2>&1"
```

Adjust the schedule so the process starts near your preferred polling window. If you want a cleaner deployment, point cron at a small wrapper script that exports the environment and then launches the worker.

## systemd Timer Option

For a cleaner headless deployment, use a systemd service and timer that starts the script when needed.

### Service

```ini
[Unit]
Description=IRCTC availability alert worker

[Service]
Type=simple
WorkingDirectory=/workspaces/irctc
EnvironmentFile=/workspaces/irctc/.env
ExecStart=/workspaces/irctc/.venv/bin/python /workspaces/irctc/irctc_agent_alert.py
Restart=no
```

### Timer

```ini
[Unit]
Description=Start IRCTC alert worker near chart window

[Timer]
OnCalendar=*-*-* 21:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable both with `systemctl --user enable --now <name>.timer` after adjusting the schedule for your route.

## Parsing Notes

- The page must expose availability in the HTML response for BeautifulSoup to read it.
- If the site is rendered entirely by client-side JavaScript, keep browser rendering enabled so Playwright can materialize the DOM before parsing.

## Behavior Notes

- The script writes `alert_fired.txt` so the same opening event is not notified twice.
- Only Pushover is implemented for delivery, as requested.
- The endpoint schema is configurable so you can plug in a RapidAPI provider or a mock JSON service for testing.