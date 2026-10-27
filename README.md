# caldav-utils

Check, purge, deduplicate, or make events public in a CalDAV calendar.

Works with standard CalDAV servers — see [Common CalDAV URLs](#common-caldav-urls) below.

## How make-public works

Events with `CLASS:PRIVATE` or `CLASS:CONFIDENTIAL` are set to `CLASS:PUBLIC`. Events without a `CLASS` property (which default to public per RFC 5545) or already set to `PUBLIC` are left unchanged.

If the server rejects the modification with `Forbidden` (e.g. for events owned by another user on a shared calendar), the script offers to delete and re-create each affected event as `PUBLIC` with a new UID. The new event is saved first, and the original is only deleted after the save succeeds, to avoid data loss. Note that re-creation makes you the organizer of the event.

## How dedup works

Two events are considered duplicates when they share the same **(SUMMARY, DTSTART, DTEND)** tuple. From each group of duplicates, one copy is kept and the rest are deleted.

Events that are **skipped** (never considered for dedup):
- Recurring events (have `RRULE`) — different recurrence rules would be lost
- Events without `DTSTART` — malformed per RFC 5545
- Events that fail to parse

## How check works

Check mode is read-only. It reports events with data that violates RFC 5545 (iCalendar) or RFC 4791 (CalDAV). A client such as macOS Calendar downloads these events without complaint, but when it uploads a change, the server can reject the whole event with `400 Bad Request`.

The check reports:
- Values that cannot be parsed, and missing `UID`, `DTSTAMP`, or `DTSTART` properties
- Properties that appear more than once although RFC 5545 allows them once (e.g. a second `DTSTAMP`)
- `DTEND` and `DURATION` in the same event, `DTEND` before `DTSTART`, and `DTSTART` and `DTEND` with different value types (date vs. date-time)
- An `RRULE` with an `UNTIL` value that does not match the value type of `DTSTART` (e.g. a floating `UNTIL` in a series with a time zone, where `UNTIL` must be in UTC)
- Overridden occurrences of a recurring event with a `RECURRENCE-ID` that matches no occurrence of the series, or that has a different value type than the `DTSTART` of the series
- Within one event resource: VEVENTs with different UIDs, more than one VEVENT without `RECURRENCE-ID`, or the same `RECURRENCE-ID` in two VEVENTs
- A UID that two events in the calendar share
- A `TZID` that is neither defined by a `VTIMEZONE` nor an IANA time zone name
- Alarms without `ACTION` or `TRIGGER`

The caldav library repairs some of these problems while loading events, for example by dropping a duplicate `DTSTAMP`. Check mode turns these repairs off, so it reports the data as stored on the server. The script exits with status 1 if it finds a problem.

```
$ python caldav_utils.py --mode check --calendar Personal
Fetching events ...
Found 45 event(s).

Found 1 event(s) with problems:

  Team standup
    URL: https://dav.mailbox.org/caldav/Y2FsOi8vMC8zMQ/8f3b2c1e.ics
    - occurrence 2025-03-10T11:00:00+01:00: RECURRENCE-ID does not match any occurrence of the series
```

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```
python caldav_utils.py [--url URL] [--username USERNAME] [--principal-path PATH] [--calendar CALENDAR] [--mode purge|dedup|make-public|check] [--dry-run] [--yes] [--debug]
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
  3. Make public (set all events to PUBLIC)
  4. Check (report events with invalid data)
Select action [1-4]:
```

Use `--mode` to skip the action prompt:

```sh
python caldav_utils.py --mode dedup --calendar Personal --dry-run
python caldav_utils.py --mode purge --calendar Personal --yes
python caldav_utils.py --mode make-public --calendar Personal --dry-run
python caldav_utils.py --mode check --calendar Personal
```

### Options

| Flag | Description |
|------|-------------|
| `--url` | CalDAV server URL (fallback: `CALDAV_URL` env var, then prompt) |
| `--username` | Username (fallback: `CALDAV_USERNAME` env var, then prompt) |
| `--principal-path` | Principal path to skip auto-discovery (e.g. `/principals/users/8/`) |
| `--calendar` | Calendar name — case-insensitive (fallback: interactive picker) |
| `--mode` | `purge` (delete all), `dedup` (remove duplicates), `make-public` (set CLASS to PUBLIC), or `check` (report invalid events) — fallback: interactive prompt |
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

Preview which events would be made public:

```
$ python caldav_utils.py --mode make-public --calendar Personal --dry-run
Fetching events ...
Found 45 event(s).

Found 2 non-public event(s):

  - [PRIVATE] Dentist appointment
  - [CONFIDENTIAL] Salary review

Dry run — 2 event(s) would be set to PUBLIC.
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

If macOS Calendar reports that "the server responded with 400" for an event, the server rejected a change that Calendar tried to upload. To discard that change, turn off Calendars for the account in System Settings > Internet Accounts and turn it on again, which makes Calendar download all events of the account from the server again. This also discards any other changes in that account that have not been synced yet. If the error comes back, use check mode to find out whether the copy of the event on the server is invalid:

```sh
python caldav_utils.py --mode check --calendar Calendar
```

## Common CalDAV URLs

| Provider | URL |
|----------|-----|
| Mailbox.org | `https://dav.mailbox.org:443` |
| Nextcloud | `https://your-server.com/remote.php/dav` |
| Fastmail | `https://caldav.fastmail.com` |
| iCloud | `https://caldav.icloud.com` |
