#!/usr/bin/env python3
"""Check, purge, deduplicate, or make events public in a CalDAV calendar."""

import argparse
import getpass
import logging
import os
import sys
import zoneinfo
from collections import Counter, defaultdict
from datetime import datetime, time

import uuid

import caldav
from caldav.lib import vcal
from caldav.lib.error import AuthorizationError
from dateutil.rrule import rruleset, rrulestr

# Properties that RFC 5545 allows at most once per VEVENT
SINGLE_PROPERTIES = (
    "UID", "DTSTAMP", "DTSTART", "DTEND", "DURATION", "RECURRENCE-ID", "SUMMARY", "DESCRIPTION",
    "LOCATION", "ORGANIZER", "CLASS", "STATUS", "SEQUENCE", "TRANSP", "CREATED", "LAST-MODIFIED",
    "PRIORITY", "GEO", "URL",
)


def resolve_credentials(args):
    """Resolve URL, username, and password from args, env vars, or prompts."""
    url = args.url or os.environ.get("CALDAV_URL")
    if not url:
        url = input("CalDAV server URL: ").strip()
    if not url:
        print("Error: URL is required.", file=sys.stderr)
        sys.exit(1)

    username = args.username or os.environ.get("CALDAV_USERNAME")
    if not username:
        username = input("Username: ").strip()
    if not username:
        print("Error: Username is required.", file=sys.stderr)
        sys.exit(1)

    password = os.environ.get("CALDAV_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")
    if not password:
        print("Error: Password is required.", file=sys.stderr)
        sys.exit(1)

    return url, username, password


def connect(url, username, password, principal_path=None):
    """Connect to the CalDAV server and return the principal."""
    try:
        client = caldav.DAVClient(url=url, username=username, password=password, timeout=300)
        if principal_path:
            return caldav.Principal(client=client, url=url.rstrip("/") + "/" + principal_path.strip("/") + "/")
        principal = client.principal()
        return principal
    except AuthorizationError:
        print("Error: Authentication failed. Check your username and password.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: Could not connect to {url}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        if not principal_path:
            print("  Try using --principal-path (e.g. --principal-path /principals/users/8/)", file=sys.stderr)
        print("  Check the URL and your network connection.", file=sys.stderr)
        sys.exit(1)


def select_calendar(principal, calendar_name):
    """Select a calendar by name or interactive picker."""
    calendars = principal.calendars()
    if not calendars:
        print("Error: No calendars found for this account.", file=sys.stderr)
        sys.exit(1)

    names = [str(c.name) for c in calendars]

    if calendar_name:
        for i, name in enumerate(names):
            if name.lower() == calendar_name.lower():
                return calendars[i]
        print(f"Error: Calendar '{calendar_name}' not found.", file=sys.stderr)
        print("Available calendars:", file=sys.stderr)
        for name in names:
            print(f"  - {name}", file=sys.stderr)
        sys.exit(1)

    # Interactive picker
    print("Available calendars:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")

    while True:
        choice = input(f"Select calendar [1-{len(calendars)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(calendars):
                return calendars[idx]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(calendars)}.")


def get_event_summary(event):
    """Extract summary from a calendar event."""
    try:
        cal = event.icalendar_instance
        for component in cal.walk():
            if component.name == "VEVENT":
                return str(component.get("SUMMARY", "(no summary)"))
    except Exception:
        pass
    return "(no summary)"


def get_event_key(event):
    """Extract (summary, dtstart_iso, dtend_iso) tuple for dedup grouping.

    Returns None for recurring events, events without DTSTART, or parse errors.
    """
    try:
        cal = event.icalendar_instance
        for component in cal.walk():
            if component.name == "VEVENT":
                # Skip recurring events
                if component.get("RRULE"):
                    return None

                dtstart = component.get("DTSTART")
                if not dtstart:
                    return None
                dtstart_iso = dtstart.dt.isoformat()

                dtend = component.get("DTEND")
                if dtend:
                    dtend_iso = dtend.dt.isoformat()
                else:
                    duration = component.get("DURATION")
                    if duration:
                        dtend_iso = "DUR:" + str(duration.dt)
                    else:
                        dtend_iso = None

                summary = str(component.get("SUMMARY", "(no summary)"))
                return (summary, dtstart_iso, dtend_iso)
    except Exception:
        pass
    return None


def find_duplicates(events):
    """Group events by key and return groups with 2+ members.

    Returns (duplicate_groups, skipped_count) where duplicate_groups is a
    dict mapping keys to lists of events.
    """
    groups = defaultdict(list)
    skipped = 0

    for event in events:
        key = get_event_key(event)
        if key is None:
            skipped += 1
        else:
            groups[key].append(event)

    duplicate_groups = {k: v for k, v in groups.items() if len(v) >= 2}
    return duplicate_groups, skipped


def format_key(key):
    """Format a key tuple for human-readable display."""
    summary, dtstart, dtend = key
    if dtend:
        return f"{summary}  [{dtstart} .. {dtend}]"
    return f"{summary}  [{dtstart}]"


def get_props(component, name):
    """Return all values of a property as a list (icalendar returns a list only for repeated properties)."""
    value = component.get(name)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def get_dt(component, name):
    """Return the date or datetime of a property, or None if it is missing or cannot be parsed."""
    props = get_props(component, name)
    try:
        return props[0].dt if props else None
    except ValueError:  # Broken value, reported via component.errors
        return None


def get_event_uids(event):
    """Return the set of UIDs used by the VEVENTs of a calendar object resource."""
    try:
        return {str(uid) for c in event.icalendar_instance.walk("VEVENT") for uid in get_props(c, "UID")}
    except Exception:
        return set()


def is_known_timezone(tzid):
    """Return whether tzid is an IANA time zone name."""
    try:
        zoneinfo.ZoneInfo(tzid)
        return True
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False


def as_datetime(value):
    """Turn a date into a datetime at midnight, since dateutil.rrule only works with datetimes."""
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time())


def is_occurrence(master, recurrence_id):
    """Return whether recurrence_id is an occurrence of the master's DTSTART, RRULE, and RDATE.

    EXDATE is ignored. Returns None if the occurrences cannot be computed
    (e.g., unparseable RRULE or floating and zoned date-times mixed).
    """
    start = get_dt(master, "DTSTART")
    if start is None:
        return None
    start = as_datetime(start)
    try:
        occurrences = rruleset()
        occurrences.rdate(start)
        for rule in get_props(master, "RRULE"):
            occurrences.rrule(rrulestr(rule.to_ical().decode(), dtstart=start))
        for rdate in get_props(master, "RDATE"):
            for value in rdate.dts:
                dt = value.dt[0] if isinstance(value.dt, tuple) else value.dt  # PERIOD values are (start, end)
                occurrences.rdate(as_datetime(dt))
        return as_datetime(recurrence_id) in occurrences
    except (TypeError, ValueError):
        return None


def check_vevent(component):
    """Return a list of problems in a single VEVENT component."""
    problems = []
    for name in ("UID", "DTSTAMP", "DTSTART"):
        if name not in component:
            problems.append(f"missing required property {name}")
    for name in SINGLE_PROPERTIES:
        count = len(get_props(component, name))
        if count > 1:
            problems.append(f"{name} appears {count} times (allowed once)")

    start = get_dt(component, "DTSTART")
    end = get_dt(component, "DTEND")
    if "DTEND" in component and "DURATION" in component:
        problems.append("has both DTEND and DURATION")
    if start is not None and end is not None:
        if isinstance(start, datetime) != isinstance(end, datetime):
            problems.append("DTSTART and DTEND have different value types (DATE vs. DATE-TIME)")
        else:
            try:
                if end < start:
                    problems.append(f"DTEND {end.isoformat()} is before DTSTART {start.isoformat()}")
            except TypeError:  # Floating and zoned date-times cannot be compared
                pass

    if start is not None:
        for rule in get_props(component, "RRULE"):
            for until in rule.get("UNTIL", []):
                if not isinstance(start, datetime):
                    valid, expected = not isinstance(until, datetime), "a date"
                elif start.tzinfo is not None:
                    valid, expected = isinstance(until, datetime) and until.tzinfo is not None, "a UTC date-time"
                else:
                    valid, expected = isinstance(until, datetime) and until.tzinfo is None, "a floating date-time"
                if not valid:
                    problems.append(f"RRULE UNTIL {until.isoformat()} must be {expected} to match DTSTART {start.isoformat()}")

    for alarm in component.walk("VALARM"):
        for name in ("ACTION", "TRIGGER"):
            if name not in alarm:
                problems.append(f"VALARM is missing required property {name}")
    return problems


def check_event(event):
    """Return a list of problems in a calendar object resource (an event and its overridden occurrences).

    Checks requirements of RFC 5545 (iCalendar) and RFC 4791 (CalDAV) that
    servers may enforce when a client uploads the event.
    """
    problems = []
    try:
        cal = event.icalendar_instance
    except Exception as e:
        problems.append(f"cannot be parsed: {type(e).__name__}: {e}")
        return problems

    for component in cal.walk():
        for name, error in component.errors:
            problems.append(f"cannot parse {name} in {component.name}: {error}")

    vevents = list(cal.walk("VEVENT"))
    if not vevents:
        problems.append("contains no VEVENT")
        return problems

    uids = {str(uid) for c in vevents for uid in get_props(c, "UID")}
    if len(uids) > 1:
        problems.append(f"VEVENTs have different UIDs ({', '.join(sorted(uids))})")

    masters = [c for c in vevents if "RECURRENCE-ID" not in c]
    overrides = [c for c in vevents if "RECURRENCE-ID" in c]
    if len(masters) > 1:
        problems.append(f"contains {len(masters)} VEVENTs without RECURRENCE-ID (at most one allowed)")
    for rid, count in Counter(get_dt(c, "RECURRENCE-ID") for c in overrides).items():
        if rid is not None and count > 1:
            problems.append(f"RECURRENCE-ID {rid.isoformat()} appears in {count} VEVENTs")

    # Every TZID must be defined by a VTIMEZONE (RFC 5545, section 3.2.19);
    # most servers also accept IANA names without one
    defined = {str(tz.get("TZID")) for tz in cal.walk("VTIMEZONE")}
    used = set()
    for component in vevents:
        for value in component.values():
            for prop in value if isinstance(value, list) else [value]:
                tzid = getattr(prop, "params", {}).get("TZID")
                if tzid:
                    used.add(str(tzid))
    for tzid in sorted(used - defined):
        if not is_known_timezone(tzid):
            problems.append(f"TZID '{tzid}' has no VTIMEZONE and is not an IANA time zone")

    for component in masters:
        problems += check_vevent(component)

    master = masters[0] if len(masters) == 1 else None
    master_start = get_dt(master, "DTSTART") if master else None
    for component in overrides:
        rid = get_dt(component, "RECURRENCE-ID")
        found = check_vevent(component)
        if master_start is not None and rid is not None:
            if isinstance(master_start, datetime) != isinstance(rid, datetime):
                found.append("RECURRENCE-ID and DTSTART of the series have different value types (DATE vs. DATE-TIME)")
            elif is_occurrence(master, rid) is False:
                found.append("RECURRENCE-ID does not match any occurrence of the series")
        label = rid.isoformat() if rid is not None else "with unparseable RECURRENCE-ID"
        problems += [f"occurrence {label}: {problem}" for problem in found]

    return problems


def fetch_unmodified_events(calendar):
    """Fetch events without caldav's automatic repair of non-conforming data.

    caldav silently drops duplicate DTSTAMP, DTEND, and DURATION lines and
    adds missing DTSTAMPs, which would hide exactly the problems the check
    looks for.
    """
    fix = vcal.fix
    vcal.fix = lambda data: data
    try:
        return calendar.events()
    finally:
        vcal.fix = fix


def select_mode(args):
    """Return 'purge', 'dedup', 'make-public', or 'check' from args or interactive prompt."""
    if args.mode:
        return args.mode

    print("\nAction:")
    print("  1. Deduplicate (remove duplicate events)")
    print("  2. Purge (delete ALL events)")
    print("  3. Make public (set all events to PUBLIC)")
    print("  4. Check (report events with invalid data)")
    while True:
        choice = input("Select action [1-4]: ").strip()
        if choice == "1":
            return "dedup"
        if choice == "2":
            return "purge"
        if choice == "3":
            return "make-public"
        if choice == "4":
            return "check"
        print("  Please enter 1, 2, 3, or 4.")


def run_purge(events, calendar, args):
    """Delete all events from the calendar (double confirmation)."""
    count = len(events)

    if args.dry_run:
        print("\nDry run — listing events:")
        for event in events:
            print(f"  - {get_event_summary(event)}")
        print(f"\n{count} event(s) would be deleted.")
        return

    if not args.yes:
        print(f"\nThis will permanently delete {count} event(s) from '{calendar.name}'.")
        answer = input('Type "yes" to continue: ').strip()
        if answer != "yes":
            print("Aborted.")
            return
        answer = input('Type "yes" again to confirm: ').strip()
        if answer != "yes":
            print("Aborted.")
            return

    print(f"\nDeleting {count} event(s) ...")
    deleted = 0
    errors = 0
    for i, event in enumerate(events, 1):
        summary = get_event_summary(event)
        try:
            event.delete()
            deleted += 1
        except Exception as e:
            errors += 1
            print(f"  Error deleting '{summary}': {type(e).__name__}: {e}", file=sys.stderr)
            print(f"    URL: {event.url}", file=sys.stderr)
        if i % 10 == 0 or i == count:
            print(f"  Progress: {i}/{count}")

    print(f"\nDone. Deleted: {deleted}, Errors: {errors}")
    if errors:
        sys.exit(1)


def run_dedup(events, calendar, args):
    """Find and remove duplicate events from the calendar."""
    duplicate_groups, skipped = find_duplicates(events)

    if skipped:
        print(f"Skipped {skipped} event(s) (recurring, missing DTSTART, or parse errors).")

    if not duplicate_groups:
        print("No duplicates found.")
        return

    total_dupes = sum(len(v) - 1 for v in duplicate_groups.values())
    print(f"\nFound {len(duplicate_groups)} group(s) of duplicates ({total_dupes} extra copies to remove):\n")

    for key, group in duplicate_groups.items():
        extras = len(group) - 1
        print(f"  x{len(group)} (will delete {extras})  {format_key(key)}")

    if args.dry_run:
        print(f"\nDry run — {total_dupes} event(s) would be deleted.")
        return

    if not args.yes:
        print(f"\nThis will permanently delete {total_dupes} duplicate event(s) from '{calendar.name}'.")
        answer = input('Type "yes" to confirm: ').strip()
        if answer != "yes":
            print("Aborted.")
            return

    print(f"\nDeleting {total_dupes} duplicate(s) ...")
    deleted = 0
    errors = 0
    processed = 0
    for key, group in duplicate_groups.items():
        for event in group[1:]:
            summary = get_event_summary(event)
            try:
                event.delete()
                deleted += 1
            except Exception as e:
                errors += 1
                print(f"  Error deleting '{summary}': {type(e).__name__}: {e}", file=sys.stderr)
                print(f"    URL: {event.url}", file=sys.stderr)
            processed += 1
            if processed % 10 == 0 or processed == total_dupes:
                print(f"  Progress: {processed}/{total_dupes}")

    print(f"\nDone. Deleted: {deleted}, Errors: {errors}")
    if errors:
        sys.exit(1)


def run_make_public(events, calendar, args):
    """Set CLASS to PUBLIC on all non-public events in the calendar."""
    non_public = []
    for event in events:
        try:
            cal = event.icalendar_instance
            for component in cal.walk():
                if component.name == "VEVENT":
                    cls = component.get("CLASS")
                    if cls and str(cls).upper() in ("PRIVATE", "CONFIDENTIAL"):
                        non_public.append((event, component, str(cls)))
                    break
        except Exception:
            pass

    if not non_public:
        print("No non-public events found.")
        return

    print(f"\nFound {len(non_public)} non-public event(s):\n")
    for event, component, cls in non_public:
        summary = str(component.get("SUMMARY", "(no summary)"))
        print(f"  - [{cls}] {summary}")

    if args.dry_run:
        print(f"\nDry run — {len(non_public)} event(s) would be set to PUBLIC.")
        return

    if not args.yes:
        print(f"\nThis will set {len(non_public)} event(s) to PUBLIC in '{calendar.name}'.")
        answer = input("Confirm? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    print(f"\nSetting {len(non_public)} event(s) to PUBLIC ...")
    modified = 0
    recreated = 0
    errors = 0
    forbidden = []
    for i, (event, component, cls) in enumerate(non_public, 1):
        summary = str(component.get("SUMMARY", "(no summary)"))
        try:
            component["CLASS"] = "PUBLIC"
            event.save()
            modified += 1
        except AuthorizationError:
            forbidden.append((event, component, cls, summary))
            print(f"  Forbidden: '{summary}' (will ask about re-creation)")
        except Exception as e:
            errors += 1
            print(f"  Error modifying '{summary}': {type(e).__name__}: {e}", file=sys.stderr)
            print(f"    URL: {event.url}", file=sys.stderr)
            print(f"    CLASS: {cls}", file=sys.stderr)
        if i % 10 == 0 or i == len(non_public):
            print(f"  Progress: {i}/{len(non_public)}")

    if forbidden:
        print(f"\n{len(forbidden)} event(s) could not be modified (Forbidden).")
        print("You can delete and re-create them as PUBLIC (you will become the organizer).\n")
        for event, component, cls, summary in forbidden:
            answer = input(f"  Delete and re-create '{summary}' as PUBLIC? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print(f"    Skipped.")
                errors += 1
                continue
            try:
                # Prepare new iCalendar data with CLASS=PUBLIC and a new UID
                cal = event.icalendar_instance
                for comp in cal.walk():
                    if comp.name == "VEVENT":
                        comp["CLASS"] = "PUBLIC"
                        comp["UID"] = str(uuid.uuid4())
                        # Remove ORGANIZER so the calendar owner becomes the implicit organizer
                        if "ORGANIZER" in comp:
                            del comp["ORGANIZER"]
                new_ical = cal.to_ical().decode("utf-8")
                # Save new version first, then delete original (avoids data loss if save fails)
                calendar.save_event(new_ical)
                event.delete()
                recreated += 1
                print(f"    Re-created as PUBLIC.")
            except Exception as e:
                errors += 1
                print(f"    Error re-creating '{summary}': {type(e).__name__}: {e}", file=sys.stderr)

    print(f"\nDone. Modified: {modified}, Re-created: {recreated}, Errors: {errors}")
    if errors:
        sys.exit(1)


def run_check(events, calendar, args):
    """Report events with data that violates the iCalendar or CalDAV specification."""
    problems = [check_event(event) for event in events]

    # A UID must be unique within a calendar (RFC 4791, section 5.3.2.1)
    by_uid = defaultdict(list)
    for i, event in enumerate(events):
        for uid in get_event_uids(event):
            by_uid[uid].append(i)
    for uid, indices in by_uid.items():
        if len(indices) > 1:
            for i in indices:
                problems[i].append(f"UID {uid} is also used by {len(indices) - 1} other event(s) in this calendar")

    flagged = [(event, found) for event, found in zip(events, problems) if found]
    if not flagged:
        print(f"No problems found in '{calendar.name}'.")
        return

    print(f"\nFound {len(flagged)} event(s) with problems:\n")
    for event, found in flagged:
        print(f"  {get_event_summary(event)}")
        print(f"    URL: {event.url}")
        for problem in found:
            print(f"    - {problem}")
        print()
    sys.exit(1)


def run(args):
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    url, username, password = resolve_credentials(args)

    print(f"Connecting to {url} ...")
    principal = connect(url, username, password, args.principal_path)
    print("Connected.")

    calendar = select_calendar(principal, args.calendar)
    print(f"Selected calendar: {calendar.name}")

    mode = select_mode(args)

    print("Fetching events ...")
    try:
        events = fetch_unmodified_events(calendar) if mode == "check" else calendar.events()
    except Exception as e:
        print(f"Error: Failed to fetch events from '{calendar.name}'.", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    count = len(events)
    print(f"Found {count} event(s).")

    if count == 0:
        print("Nothing to do.")
        return

    if mode == "dedup":
        run_dedup(events, calendar, args)
    elif mode == "make-public":
        run_make_public(events, calendar, args)
    elif mode == "check":
        run_check(events, calendar, args)
    else:
        run_purge(events, calendar, args)


def main():
    parser = argparse.ArgumentParser(description="Check, purge, deduplicate, or make events public in a CalDAV calendar.")
    parser.add_argument("--url", help="CalDAV server URL (env: CALDAV_URL)")
    parser.add_argument("--username", help="Username (env: CALDAV_USERNAME)")
    parser.add_argument("--principal-path", help="Principal path to skip discovery (e.g. /principals/users/8/)")
    parser.add_argument("--calendar", help="Calendar name (otherwise interactive picker)")
    parser.add_argument("--mode", choices=["purge", "dedup", "make-public", "check"], help="Action: purge (delete all), dedup (remove duplicates), make-public (set CLASS to PUBLIC), or check (report invalid events)")
    parser.add_argument("--dry-run", action="store_true", help="List events without deleting")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt(s)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging (shows HTTP requests)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
