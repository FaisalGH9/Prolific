# Prolific Study Monitor

A lightweight Python monitor that watches the Prolific participant studies feed,
sends Telegram alerts, and assists with opening and reserving newly available
studies through a locally controlled browser.

## Current version — 25 July 2026

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
- Automatic retries when Prolific reports that a study is in high demand.
- Detection and Telegram reporting of successful reservations and full studies.
- Cleanup of duplicate Prolific browser tabs.
- A currently enabled manual-only safeguard for **Saudi Sexism Detection** by
  `qmul.ac.uk`. The monitor reports this study but skips automatic reservation.
  Set `MANUAL_ONLY_STUDY_RULE_ENABLED = False` to disable this rule.

## Files

- `prolific_monitor.py` — monitor, browser watcher, auto-click guard, and
  Telegram integration.
- `prolific-monitor.json` — polling and browser-request configuration.
- `test_prolific_monitor.py` — reward-formatting tests.
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

## Commands

Test Telegram:

```powershell
py .\prolific_monitor.py test-telegram
```

Run the monitor with automatic browser token refresh:

```powershell
py .\prolific_monitor.py run-with-browser
```

Run the tests:

```powershell
py -m unittest -v
```
