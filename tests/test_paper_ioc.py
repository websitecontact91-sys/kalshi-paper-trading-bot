from decimal import Decimal as D

import pytest

from paper_ioc import simulate_ioc


def test_ioc_full_fill_is_terminal_and_never_rests():
    result = simulate_ioc(
        requested_quantity="5.00", available_quantity="5.00",
        price="0.87", fee_multiplier="1", balance_precision="0.01", budget="5.00")
    assert result["status"] == "FILLED"
    assert D(result["filled_quantity"]) == D("5.00")
    assert D(result["cancelled_quantity"]) == 0
    assert D(result["resting_quantity"]) == 0
    assert D(result["debit"]) <= D("5.00")


def test_ioc_partial_fill_accepts_immediate_quantity_and_cancels_remainder():
    result = simulate_ioc(
        requested_quantity="5.00", available_quantity="2.00",
        price="0.87", fee_multiplier="1", balance_precision="0.01", budget="5.00")
    assert result["status"] == "PARTIALLY_FILLED"
    assert D(result["filled_quantity"]) == D("2.00")
    assert D(result["cancelled_quantity"]) == D("3.00")
    assert D(result["resting_quantity"]) == 0
    assert D(result["payout"]) == D("2.00")
    assert D(result["principal"]) + D(result["fees"]) == D(result["debit"])


def test_ioc_zero_fill_cancels_entire_request_without_position():
    result = simulate_ioc(
        requested_quantity="5.00", available_quantity="0",
        price="0.87", fee_multiplier="1", balance_precision="0.01", budget="5.00")
    assert result["status"] == "UNFILLED"
    assert D(result["filled_quantity"]) == 0
    assert D(result["cancelled_quantity"]) == D("5.00")
    assert D(result["resting_quantity"]) == 0
    assert D(result["debit"]) == 0


def test_ioc_available_depth_is_rounded_down_to_contract_increment():
    result = simulate_ioc(
        requested_quantity="5.00", available_quantity="1.237",
        price="0.87", fee_multiplier="1", balance_precision="0.01", budget="5.00")
    assert D(result["filled_quantity"]) == D("1.23")
    assert D(result["cancelled_quantity"]) == D("3.77")
    assert D(result["resting_quantity"]) == 0


@pytest.mark.parametrize("requested", ["0", "-1", "1.001"])
def test_ioc_rejects_invalid_requested_quantity(requested):
    with pytest.raises(ValueError):
        simulate_ioc(
            requested_quantity=requested, available_quantity="1",
            price="0.87", fee_multiplier="1", balance_precision="0.01", budget="5.00")
