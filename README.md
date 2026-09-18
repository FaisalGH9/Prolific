# Prolific Study Monitor

A lightweight Python monitor that watches the Prolific participant studies feed,
sends Telegram alerts, and assists with opening and reserving newly available
studies through a locally controlled browser.

## Current version — 4 September 2026

This version includes:

- Polling of Prolific's participant studies endpoint with duplicate suppression.
- Telegram notifications for new studies and reservation results.
- Correct reward formatting from Prolific's minor currency units, for example
  `398` is displayed as `£3.98`.
- Automatic Prolific bearer-token refresh from browser network activity.
- Automatic opening of newly detected studies in the browser.
- Study selection by stable Prolific study ID, with tolerant title matching as
  a fallback.
- Interactive-card targeting and retries until the real **Take part** button
  appears.
- A stale study target expires after 20 seconds when its card is no longer
  available. Once cleared, the browser guard stops producing repeated
  **Target study card not found** messages and continues watching normally.
- Automatic retries when Prolific reports that a study is in high demand.
- Detection and Telegram reporting of successful reservations and full studies.
- Structured SQLite diagnostics for each browser reservation attempt, including
  card selection, button state, click, retry, and final result events.
- Automatic classification of missing, disabled, hidden, changed-text, and
  possible cross-origin iframe button failures.
- Automatic deletion and database compaction for diagnostic records older than
  72 hours.
- Cleanup of duplicate Prolific browser tabs.
- A currently enabled manual-only safeguard for **Saudi Sexism Detection** by
  `qmul.ac.uk`. The monitor reports this study but skips automatic reservation.
  Set `MANUAL_ONLY_STUDY_RULE_ENABLED = False` to disable this rule.

## Files

- `prolific_monitor.py` — monitor, browser watcher, auto-click guard, and
  Telegram integration.
- `prolific-monitor.json` — polling and browser-request configuration.
- `test_prolific_monitor.py` — reward-formatting and browser-guard tests.
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

Browser diagnostics are enabled by default:

```json
"diagnostics": {
  "enabled": true,
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

Show reservation diagnostics from the last three days:

```powershell
py .\prolific_monitor.py diagnostics
```

Use a shorter reporting window when needed:

```powershell
py .\prolific_monitor.py diagnostics --hours 24
```

Run the tests:

```powershell
py -m unittest -v
```
