"""One definition of the entry window and observation freshness."""
import datetime as dt
import math

MAX_OBS_AGE_S = 15
MAX_FUTURE_S = 5
ENTRY_WINDOW_S = 60
MIN_ENTRY_SECONDS_REMAINING = 10
ROUND_SECONDS = 900

def timestamp(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing/invalid timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        if value.replace(".", "", 1).isdigit():
            result = float(value)
        else:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timestamp must include timezone")
            result = parsed.timestamp()
    else:
        raise ValueError("Unsupported timestamp")
    if not math.isfinite(result):
        raise ValueError("Nonfinite timestamp")
    return result

def seconds_to_close(close_time, now):
    return timestamp(close_time) - timestamp(now)

def in_entry_window(close_time, now):
    return 0 < seconds_to_close(close_time, now) <= ENTRY_WINDOW_S

def entry_time_eligible(close_time, now):
    remaining = seconds_to_close(close_time, now)
    return MIN_ENTRY_SECONDS_REMAINING <= remaining <= ENTRY_WINDOW_S

def round_key(close_time):
    return str(math.ceil(timestamp(close_time) / ROUND_SECONDS))

def observation_fresh(observed_at, now, max_age=MAX_OBS_AGE_S):
    try:
        return -MAX_FUTURE_S <= timestamp(now) - timestamp(observed_at) <= max_age
    except (ValueError, TypeError, OverflowError):
        return False
