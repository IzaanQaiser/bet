"""Real Google Calendar reads for dispatcher-svc — capacity-engine.md §1's
inputs. Read-only in practice (this module only ever calls GET), even
though the OAuth token itself carries the broader `calendar.events`
scope: dispatcher-svc and committer-svc share one refresh token per user
(there's no separate read-only grant — see infrastructure.md §4's note),
so the "read only" boundary in infrastructure.md §2.1's IAM matrix is
enforced by this module never calling anything but GET, not by a
narrower OAuth scope.

fetch_events_for_range() does one Calendar API call (paginated only if a
user genuinely has 2500+ events in the queried span) for an entire date
range, not one call per day — matches infrastructure.md §4's stated
quota assumption ("two reads per user per dispatcher run": one for the
14-day trailing window, one for the 7-day forward window).
"""

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from google.cloud import secretmanager
from google.oauth2.credentials import Credentials

from dispatcher_svc.capacity_engine import Event

CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _secret_client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def user_credentials(refresh_token_ref: str) -> Credentials:
    refresh_token = (
        _secret_client().access_secret_version(name=refresh_token_ref).payload.data.decode()
    )
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=[CALENDAR_SCOPE],
    )


def _local_time_clamped(dt: datetime, day: date, tz: ZoneInfo) -> time:
    """A timed event's start/end, expressed as a time-of-day on `day`. An
    event that spans a midnight boundary into an adjacent day clamps to
    that day's edge rather than producing a start/end outside
    [00:00, 23:59] that capacity_engine's clip() isn't designed to
    handle — also how a multi-day event gets split across each day it
    overlaps, one Event per day."""
    local = dt.astimezone(tz)
    if local.date() < day:
        return time(0, 0)
    if local.date() > day:
        return time(23, 59)
    return local.time()


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _add_event_to_days(
    raw: dict,
    events_by_day: dict[date, list[Event]],
    tz: ZoneInfo,
    range_start: date,
    range_end: date,
) -> None:
    start_raw, end_raw = raw["start"], raw["end"]

    if "date" in start_raw:  # all-day event(s) — Calendar's end date is exclusive
        first_day = date.fromisoformat(start_raw["date"])
        last_day = date.fromisoformat(end_raw["date"]) - timedelta(days=1)
        for d in _date_range(max(first_day, range_start), min(last_day, range_end)):
            events_by_day[d].append(Event(start=time(0, 0), end=time(23, 59), all_day=True))
        return

    start_dt = datetime.fromisoformat(start_raw["dateTime"])
    end_dt = datetime.fromisoformat(end_raw["dateTime"])
    declined = any(
        a.get("self") and a.get("responseStatus") == "declined" for a in raw.get("attendees", [])
    )
    transparency = raw.get("transparency", "opaque")

    first_day = start_dt.astimezone(tz).date()
    last_day = end_dt.astimezone(tz).date()
    for d in _date_range(max(first_day, range_start), min(last_day, range_end)):
        events_by_day[d].append(
            Event(
                start=_local_time_clamped(start_dt, d, tz),
                end=_local_time_clamped(end_dt, d, tz),
                all_day=False,
                declined=declined,
                transparency=transparency,
            )
        )


def fetch_events_for_range(
    session: AuthorizedSession, start: date, end: date, tz_name: str
) -> dict[date, list[Event]]:
    tz = ZoneInfo(tz_name)
    range_start = datetime.combine(start, time(0, 0), tzinfo=tz)
    range_end = datetime.combine(end, time(23, 59, 59), tzinfo=tz)
    events_by_day: dict[date, list[Event]] = {d: [] for d in _date_range(start, end)}

    page_token = None
    while True:
        params = {
            "timeMin": range_start.isoformat(),
            "timeMax": range_end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 2500,
        }
        if page_token:
            params["pageToken"] = page_token
        response = session.get(CALENDAR_EVENTS_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        for raw in payload.get("items", []):
            _add_event_to_days(raw, events_by_day, tz, start, end)
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return events_by_day
