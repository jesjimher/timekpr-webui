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

## Utility scripts

- `reset_db.py` — drops and recreates the database (destructive)
- `migrate_passwords.py` — one-time migration of plain-text passwords to bcrypt hashes
