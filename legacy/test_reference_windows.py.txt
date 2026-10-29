"""Offline checks for reference window arithmetic and incomplete feeds."""
import pytest
from reference_windows import complete_minute_average, reference_windows


def test_exact_sixty_seconds():
    points = [(1000 + i, 100 + i) for i in range(60)]
    assert complete_minute_average(points, 1000) == 129.5


def test_missing_second_fails_closed():
    points = [(1000 + i, 100.0) for i in range(60) if i != 20]
    with pytest.raises(ValueError, match='Incomplete'):
        complete_minute_average(points, 1000)


def test_duplicate_second_does_not_double_count():
    points = [(1000 + i, 100.0) for i in range(60)]
    points.append((1000.5, 160.0))
    assert complete_minute_average(points, 1000) == 101.0


def test_start_and_end_minutes_do_not_overlap():
    points = [(1000 + i, 100.0) for i in range(60)]
    points += [(1000 + 840 + i, 110.0) for i in range(60)]
    assert reference_windows(points, 1000) == (100.0, 110.0)


def test_last_second_is_exclusive():
    points = [(1000 + i, 100.0) for i in range(59)]
    points.append((1060.0, 200.0))
    with pytest.raises(ValueError):
        complete_minute_average(points, 1000)
