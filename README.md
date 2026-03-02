# caldav-utils

Purge or deduplicate events in a CalDAV calendar.

Works with any CalDAV server (Mailbox.org, Nextcloud, Fastmail, iCloud, etc.).

## How dedup works

Two events are considered duplicates when they share the same **(SUMMARY, DTSTART, DTEND)** tuple. From each group of duplicates, one copy is kept and the rest are deleted.

Events that are **skipped** (never considered for dedup):
- Recurring events (have `RRULE`) — different recurrence rules would be lost
- Events without `DTSTART` — malformed per RFC 5545
- Events that fail to parse

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```
python caldav_utils.py [--url URL] [--username USERNAME] [--principal-path PATH] [--calendar CALENDAR] [--mode purge|dedup] [--dry-run] [--yes] [--debug]
```

Any missing credentials are prompted interactively. After connecting and selecting a calendar, the script asks which action to perform:

```
$ python caldav_utils.py
CalDAV server URL: https://dav.mailbox.org:443
Username: user@mailbox.org
Password:
Connecting to https://dav.mailbox.org:443 ...
Connected.
Available calendars:
  1. Personal
  2. Work
Select calendar [1-2]: 1
Selected calendar: Personal

Action:
  1. Deduplicate (remove duplicate events)
  2. Purge (delete ALL events)
Select action [1-2]:
```

Use `--mode` to skip the action prompt:

```sh
python caldav_utils.py --mode dedup --calendar Personal --dry-run
python caldav_utils.py --mode purge --calendar Personal --yes
```

### Options

| Flag | Description |
|------|-------------|
| `--url` | CalDAV server URL (fallback: `CALDAV_URL` env var, then prompt) |
| `--username` | Username (fallback: `CALDAV_USERNAME` env var, then prompt) |
| `--principal-path` | Principal path to skip auto-discovery (e.g. `/principals/users/8/`) |
| `--calendar` | Calendar name — case-insensitive (fallback: interactive picker) |
| `--mode` | `purge` (delete all) or `dedup` (remove duplicates) — fallback: interactive prompt |
| `--dry-run` | List events without deleting |
| `--yes`, `-y` | Skip confirmation prompt(s) |
| `--debug` | Enable debug logging (shows HTTP requests) |

The password is read from the `CALDAV_PASSWORD` env var or prompted via `getpass` (never a CLI argument).

### Environment variables

For non-interactive use (scripts, cron):

```sh
export CALDAV_URL=https://dav.mailbox.org:443
export CALDAV_USERNAME=user@mailbox.org
export CALDAV_PASSWORD=secret
python caldav_utils.py --mode dedup --calendar Personal --yes
```

### Dry run

Preview which duplicates would be removed:

```
$ python caldav_utils.py --mode dedup --calendar Personal --dry-run
Fetching events ...
Found 45 event(s).

Found 3 group(s) of duplicates (4 extra copies to remove):

  x3 (will delete 2)  Team standup  [2025-01-15T10:00:00 .. 2025-01-15T10:30:00]
  x2 (will delete 1)  Dentist appointment  [2025-02-01T14:00:00 .. 2025-02-01T15:00:00]
  x2 (will delete 1)  Flight to Berlin  [2025-03-10T08:00:00 .. 2025-03-10T11:00:00]

Dry run — 4 event(s) would be deleted.
```

Preview which events would be purged:

```
$ python caldav_utils.py --mode purge --calendar Personal --dry-run
Fetching events ...
Found 3 event(s).

Dry run — listing events:
  - Team standup
  - Dentist appointment
  - Flight to Berlin

3 event(s) would be deleted.
```

### Troubleshooting

If the script hangs at "Connecting to …", the server may not support principal auto-discovery on the root URL. Use `--principal-path` to skip discovery:

```sh
python caldav_utils.py --url https://dav.mailbox.org:443 --username user@mailbox.org --principal-path /principals/users/8/ --dry-run
```

You can find your principal path in your CalDAV client settings or by checking the server's `.well-known/caldav` response.

To inspect the HTTP requests being made, use `--debug`:

```sh
python caldav_utils.py --url https://dav.mailbox.org:443 --username user@mailbox.org --debug --dry-run
```

## Common CalDAV URLs

| Provider | URL |
|----------|-----|
| Mailbox.org | `https://dav.mailbox.org:443` |
| Nextcloud | `https://your-server.com/remote.php/dav` |
| Fastmail | `https://caldav.fastmail.com` |
| iCloud | `https://caldav.icloud.com` |
