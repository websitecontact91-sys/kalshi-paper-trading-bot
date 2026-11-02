"""Review-only specification for a single bounded IOC retry after zero fill.

This file intentionally does not modify production live execution.

Target behavior:
- First IOC remains unchanged.
- A confirmed zero-fill/cancel may trigger exactly one re-quote and one second IOC.
- The refreshed candidate must independently satisfy every existing entry rule.
- Any positive partial fill ends retry eligibility; the canceled remainder is never chased.
- A second zero fill is terminal.
- The retry is part of the same durable round attempt and must not clear attempted_rounds.
- The second POST must use a distinct client_order_id.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryInputs:
    first_ioc_zero_fill: bool = True
    first_ioc_partial_fill: bool = False
    retry_count: int = 0
    refreshed_candidate_valid: bool = True
    cushion_shrank: bool = False
    seconds_remaining: float = 30.0
    same_market_identity: bool = True


def should_retry_zero_fill_ioc(x: RetryInputs) -> bool:
    """Pure review model for the intended bounded retry policy."""
    if not x.first_ioc_zero_fill:
        return False
    if x.first_ioc_partial_fill:
        return False
    if x.retry_count != 0:
        return False
    if not x.refreshed_candidate_valid:
        return False
    if x.cushion_shrank:
        return False
    if x.seconds_remaining < 10:
        return False
    if not x.same_market_identity:
        return False
    return True


def test_one_confirmed_zero_fill_can_retry_once_when_every_rule_still_passes():
    assert should_retry_zero_fill_ioc(RetryInputs())


def test_positive_partial_fill_never_retries_the_remainder():
    assert not should_retry_zero_fill_ioc(RetryInputs(first_ioc_partial_fill=True))


def test_second_zero_fill_cannot_trigger_a_third_post():
    assert not should_retry_zero_fill_ioc(RetryInputs(retry_count=1))


def test_invalid_refreshed_candidate_does_not_retry():
    assert not should_retry_zero_fill_ioc(RetryInputs(refreshed_candidate_valid=False))


def test_retry_is_blocked_if_selected_side_cushion_shrinks():
    assert not should_retry_zero_fill_ioc(RetryInputs(cushion_shrank=True))


def test_retry_is_blocked_below_ten_seconds():
    assert not should_retry_zero_fill_ioc(RetryInputs(seconds_remaining=9.999))


def test_retry_requires_exact_same_market_side_round_and_strike_identity():
    assert not should_retry_zero_fill_ioc(RetryInputs(same_market_identity=False))


def test_non_zero_fill_terminal_state_never_enters_retry_path():
    assert not should_retry_zero_fill_ioc(RetryInputs(first_ioc_zero_fill=False))


def test_retry_policy_is_strictly_bounded_to_two_posts_total():
    first_post_number = 1
    retry_count = 0
    assert should_retry_zero_fill_ioc(RetryInputs(retry_count=retry_count))
    second_post_number = first_post_number + 1
    retry_count += 1
    assert second_post_number == 2
    assert not should_retry_zero_fill_ioc(RetryInputs(retry_count=retry_count))
