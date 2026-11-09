import pytest

from veltrix.solana_meme.paper_sizing import calculate_paper_exit_size

MINT = "So11111111111111111111111111111111111111112"


def test_usd_position_converts_to_exact_raw_token_amount_without_rounding_up():
    result = calculate_paper_exit_size(
        MINT, notional_usd=50, token_price_usd=0.001,
        decimals=6, total_supply_raw=1_000_000_000_000,
    )
    assert result.input_amount_raw == 50_000_000_000
    assert result.supply_fraction == 0.05
    assert result.effective_notional_usd == 50


def test_decimal_rounding_is_downward_and_never_exceeds_requested_notional():
    result = calculate_paper_exit_size(
        MINT, notional_usd=1, token_price_usd=0.003,
        decimals=2, total_supply_raw=100_000,
        min_supply_fraction=0.000001,
    )
    assert result.input_amount_raw == 33_333
    assert result.effective_notional_usd <= 1


def test_raw_amount_must_remain_inside_supply_fraction_bounds():
    with pytest.raises(ValueError, match="supply-fraction"):
        calculate_paper_exit_size(
            MINT, notional_usd=1, token_price_usd=1,
            decimals=6, total_supply_raw=1_000_000_000_000,
        )
    with pytest.raises(ValueError, match="supply-fraction"):
        calculate_paper_exit_size(
            MINT, notional_usd=60, token_price_usd=0.001,
            decimals=6, total_supply_raw=1_000_000_000_000,
        )


@pytest.mark.parametrize("kwargs", [
    {"notional_usd": 0, "token_price_usd": 1, "decimals": 6, "total_supply_raw": 1000},
    {"notional_usd": 1, "token_price_usd": 0, "decimals": 6, "total_supply_raw": 1000},
    {"notional_usd": 1, "token_price_usd": 1, "decimals": True, "total_supply_raw": 1000},
    {"notional_usd": 1, "token_price_usd": 1, "decimals": 6, "total_supply_raw": True},
    {"notional_usd": float("nan"), "token_price_usd": 1, "decimals": 6, "total_supply_raw": 1000},
])
def test_invalid_sizing_inputs_fail_closed(kwargs):
    with pytest.raises(ValueError):
        calculate_paper_exit_size(MINT, **kwargs)


def test_amount_below_one_raw_unit_is_rejected():
    with pytest.raises(ValueError, match="one raw token unit"):
        calculate_paper_exit_size(
            MINT, notional_usd=0.000001, token_price_usd=10,
            decimals=2, total_supply_raw=1_000_000,
        )
