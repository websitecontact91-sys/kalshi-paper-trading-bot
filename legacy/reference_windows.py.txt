"""Time-window settlement reference calculations for sampled index observations.

These helpers do not fetch data and do not authorize live or paper entries.
An observation at the beginning of each second is required to claim a
complete 60-second window; missing data fails closed.
"""
import math


def complete_minute_average(observations, start_epoch):
    """Average one observation per second for [start, start+60).

    Observations are (epoch_seconds, index_price) tuples. Duplicate seconds
    are resolved by the latest timestamp in that second. A partial minute
    raises ValueError rather than pretending to be the official benchmark.
    """
    if not math.isfinite(start_epoch):
        raise ValueError('Invalid window start')
    seconds = {}
    for timestamp, value in observations:
        timestamp = float(timestamp)
        value = float(value)
        if not math.isfinite(timestamp) or not math.isfinite(value) or value <= 0:
            continue
        second = math.floor(timestamp - start_epoch)
        if 0 <= second < 60:
            previous = seconds.get(second)
            if previous is None or timestamp > previous[0]:
                seconds[second] = (timestamp, value)
    if len(seconds) != 60:
        raise ValueError('Incomplete 60-second reference window: %d/60 seconds' % len(seconds))
    return sum(seconds[i][1] for i in range(60)) / 60.0


def reference_windows(observations, start_epoch):
    """Compute opening and closing 60-second averages of a 900-second round."""
    return (
        complete_minute_average(observations, start_epoch),
        complete_minute_average(observations, start_epoch + 840),
    )
