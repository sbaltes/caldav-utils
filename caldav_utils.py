#!/usr/bin/env python3
"""Purge or deduplicate events in a CalDAV calendar."""

import argparse
import getpass
import logging
import os
import sys
from collections import defaultdict

import caldav
from caldav.lib.error import AuthorizationError


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


def select_mode(args):
    """Return 'purge' or 'dedup' from args or interactive prompt."""
    if args.mode:
        return args.mode

    print("\nAction:")
    print("  1. Deduplicate (remove duplicate events)")
    print("  2. Purge (delete ALL events)")
    while True:
        choice = input("Select action [1-2]: ").strip()
        if choice == "1":
            return "dedup"
        if choice == "2":
            return "purge"
        print("  Please enter 1 or 2.")


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
            print(f"  Error deleting '{summary}': {e}", file=sys.stderr)
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
                print(f"  Error deleting '{summary}': {e}", file=sys.stderr)
            processed += 1
            if processed % 10 == 0 or processed == total_dupes:
                print(f"  Progress: {processed}/{total_dupes}")

    print(f"\nDone. Deleted: {deleted}, Errors: {errors}")
    if errors:
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
        events = calendar.events()
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
    else:
        run_purge(events, calendar, args)


def main():
    parser = argparse.ArgumentParser(description="Purge or deduplicate events in a CalDAV calendar.")
    parser.add_argument("--url", help="CalDAV server URL (env: CALDAV_URL)")
    parser.add_argument("--username", help="Username (env: CALDAV_USERNAME)")
    parser.add_argument("--principal-path", help="Principal path to skip discovery (e.g. /principals/users/8/)")
    parser.add_argument("--calendar", help="Calendar name (otherwise interactive picker)")
    parser.add_argument("--mode", choices=["purge", "dedup"], help="Action: purge (delete all) or dedup (remove duplicates)")
    parser.add_argument("--dry-run", action="store_true", help="List events without deleting")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt(s)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging (shows HTTP requests)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
