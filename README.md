# Prolific Study Monitor

A lightweight Python monitor that watches the Prolific participant studies feed,
sends Telegram alerts, and assists with opening and reserving newly available
studies through a locally controlled browser.

## Current version — 18 October 2026

This version includes:

- Polling of Prolific's participant studies endpoint with duplicate suppression.
- Telegram notifications for new studies and reservation results.
- Google Chrome is preferred by default. The desktop launcher explicitly uses
  Chrome with port `9223` and the separate `.browser-profile-chrome` profile.
- Reservation monitoring starts before the first button click, including when
  the button is already visible. Full studies can also be detected before a
  click when the matching study details are visible.
- A reservation attempt reports one terminal result, with duplicate and stale
  browser events suppressed across page reloads.
- Correct reward formatting from Prolific's minor currency units, for example
  `398` is displayed as `£3.98`.
- Automatic Prolific bearer-token refresh from browser network activity.
- Automatic opening of newly detected studies in the browser.
- Study selection by stable Prolific study ID, with tolerant title matching as
  a fallback.
- Interactive-card targeting and retries until the real **Take part** button
  appears.
- A stale study target expires after 20 seconds of ready-page search time.
  Search and result timers pause while the page loads. A separate page-loading
  limit defaults to 120 seconds (`browser.page_load_timeout_seconds`).
- Unclear reservation results and page-loading timeouts produce distinct
  Telegram notices rather than being reported as full studies.
- Automatic retries when Prolific reports that a study is in high demand.
- Detection and Telegram reporting of successful reservations and full studies.
- Structured SQLite diagnostics for each browser reservation attempt, including
  card selection, button state, click, retry, and final result events.
- Automatic classification of missing, disabled, hidden, changed-text, and
  possible cross-origin iframe button failures.
- Automatic deletion and database compaction for diagnostic records older than
  72 hours.
- Cleanup of duplicate Prolific browser tabs.
- A currently enabled manual-only safeguard for **Saudi Sexism Detection** and
  numbered **Sexism Detection - Cross-annotation** studies by `qmul.ac.uk`.
  The monitor reports these studies but skips automatic reservation.
  Set `MANUAL_ONLY_STUDY_RULE_ENABLED = False` to disable this rule.

## Files

- `prolific_monitor.py` — monitor, browser watcher, auto-click guard, and
  Telegram integration.
- `prolific-monitor.json` — polling and browser-request configuration.
- `test_prolific_monitor.py` — reward-formatting and browser-guard tests.
- `test_browser_guard.cjs` — isolated JavaScript scenarios used by the Python
  tests to verify browser behavior without opening a browser or sending alerts.
- `.env.example` — required environment-variable names without secret values.

## Configuration

Copy `.env.example` to `.env` and provide:

```env
PROLIFIC_BEARER_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Never commit `.env`, Telegram bot tokens, bearer tokens, the browser profile, or
runtime state. These paths are excluded by `.gitignore`.

Browser diagnostics are disabled in the supplied configuration. Set `enabled`
to `true` to record reservation diagnostics:

```json
"diagnostics": {
  "enabled": false,
  "retention_hours": 72
}
```

On Windows, the database is stored outside this OneDrive project at
`%LOCALAPPDATA%\ProlificMonitor\diagnostics.sqlite3`. It contains structured
study and button-state events, not complete page HTML or screenshots. Cleanup
runs when the database opens and once per hour while the browser watcher runs.
SQLite secure deletion, WAL truncation, and compaction are used when expired
records are removed. Storage hardware or external backups can still prevent an
absolute physical-erasure guarantee.

## Commands

Test Telegram:

```powershell
py .\prolific_monitor.py test-telegram
```

Run the monitor with automatic browser token refresh:

```powershell
py .\prolific_monitor.py run-with-browser
```

Alternatively, use **Start Prolific Monitor** on the desktop. Sign in to Prolific
in the separate Chrome profile on its first launch. Existing Brave profile data
is preserved, and the monitor uses a separate port to avoid attaching to it.

Show reservation diagnostics from the last three days:

```powershell
py .\prolific_monitor.py diagnostics
```

Use a shorter reporting window when needed:

```powershell
py .\prolific_monitor.py diagnostics --hours 24
```

Run the tests with Python and Node.js installed:

```powershell
py -m unittest -v
```
