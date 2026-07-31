# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Flask web application for remotely managing [Timekpr-nExT](https://mjasnik.gitlab.io/timekpr-next/) parental controls across multiple Linux computers. Changes are pushed to remote systems via SSH using `timekpra` CLI commands. Offline computers receive queued changes when they come back online.

## Running the app

**Without Docker (development):**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```
Runs on `http://localhost:5000`. Database at `instance/timekpr.db`. Default login: `admin`/`admin`.

Note: system Python on Ubuntu 24.04+ blocks pip installs outside a venv (PEP 668); always use the venv.

**With Docker:**
```bash
docker-compose up -d
```
Database persists in a named Docker volume (`timekpr_data`). SSH keys are mounted read-only from `./ssh/`.

**SSH key requirement:** Both dev and Docker modes require `ssh/timekpr_ui_key` (RSA private key) to exist before the app can connect to remote systems. Generate with:
```bash
mkdir ssh && ssh-keygen -t rsa -b 4096 -f ./ssh/timekpr_ui_key -N ""
```

## Architecture

```
app.py              — Flask routes and application entry point
src/
  database.py       — SQLAlchemy models and all DB logic
  ssh_helper.py     — SSH connections to remote timekpr systems (via Paramiko)
  task_manager.py   — Background thread that runs every 10s to sync changes
templates/          — Jinja2 HTML templates
```

### Data flow

1. User edits schedules/time in the web UI → stored in SQLite with `is_synced=False`
2. `BackgroundTaskManager` daemon thread (10s cycle) picks up unsynced records and calls `ssh_helper.py` methods
3. `SSHClient` connects as `timekpr-remote` user on the target machine, runs `timekpra` CLI commands
4. On success, records are marked `is_synced=True`; on failure (offline machine), they stay queued
5. Every read cycle (`--userinfo`, every 60s) also re-verifies the intended schedule/hours against
   what the host itself reports (`LIMITS_PER_WEEKDAYS`, `ALLOWED_WEEKDAYS`, `ALLOWED_HOURS_<day>`),
   using data already fetched — this costs no extra SSH. `is_synced` only means "we sent it"; a
   mismatch here sets `ManagedUser.drift_detail`, triggers one rate-limited automatic re-push, and
   turns the dashboard card red. `limits_verified_at` means "the host's own config confirmed it".
   The dashboard's "Time Left Today" is read from the host's reported `TIME_LEFT_DAY`, never
   computed locally — a locally-computed number can't reveal a limit that silently failed to apply.

### Command budget (read before touching ssh_helper.py or task_manager.py)

Every `timekpra` invocation raises a desktop notification on the machine's logged-in user. Writes
in `ssh_helper.py` (`set_weekly_time_limits`, `set_allowed_hours`) are differential: they compare
against the last known host config and skip any command whose effect the host already reports.
Auto-repair on drift (`task_manager._schedule_drift_repush`) fires at most once per drift episode —
never loop-retry a write the host isn't applying. Do not add new periodic host contact (the 60s
read interval is the only recurring SSH traffic); verification reuses data that read already fetched.

### Key models (`src/database.py`)

- `ManagedUser` — a username+IP pair to manage. Has `pending_time_adjustment`/`pending_time_operation` columns for queued one-off time changes.
- `UserTimeUsage` — daily time-spent records (seconds), one row per user per day
- `UserWeeklySchedule` — per-user daily time limits in hours (float), with sync tracking
- `UserDailyTimeInterval` — allowed-hours windows per day (start/end hour:minute), with sync tracking
- `Settings` — key/value store; holds `admin_password_hash` (bcrypt)

### SSH commands used

`SSHClient` wraps these `timekpra` CLI commands:
- `timekpra --userinfo <user>` — fetch current config and usage
- `timekpra --settimeleft <user> +/- <seconds>` — add/remove time
- `timekpra --setalloweddays <user> '<1;2;...>'` — set which days are allowed
- `timekpra --settimelimits <user> '<sec;sec;...>'` — set daily time budgets
- `timekpra --setallowedhours <user> <day_num> '<h;h;...>'` — set allowed hours for a day

Commands are tried without `sudo` first, then retried with `sudo` if the first attempt fails.

### Timezone handling

All datetimes are stored as UTC in the database. The `TZ` env var controls display timezone (default: `UTC`). A `localtime` Jinja2 filter converts UTC datetimes for templates. The `TZ` variable is injected into all templates via the `inject_timezone` context processor.

## Git operations

Never perform write git operations (commit, rebase, push, amend, reset, etc.) without explicitly asking the user for confirmation first.

## Language

All code, comments, variable names, log messages, UI strings, and documentation must be written in **English**.

## Utility scripts

- `reset_db.py` — drops and recreates the database (destructive)
- `migrate_passwords.py` — one-time migration of plain-text passwords to bcrypt hashes
