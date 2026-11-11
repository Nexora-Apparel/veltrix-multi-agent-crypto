"""Golden-vector and adversarial tests for pinned Raydium tick math."""
import random

import pytest

from veltrix.raydium_clmm_tick_math import (
    Q64,
    RAYDIUM_MAX_TICK,
    RAYDIUM_MAX_SQRT_PRICE_X64,
    RAYDIUM_MIN_TICK,
    RAYDIUM_MIN_SQRT_PRICE_X64,
    RaydiumTickMathError,
    get_sqrt_price_at_tick,
    get_tick_at_sqrt_price,
)


def test_source_pinned_tick_math_boundary_vectors():
    assert get_sqrt_price_at_tick(RAYDIUM_MIN_TICK) == RAYDIUM_MIN_SQRT_PRICE_X64
    assert get_sqrt_price_at_tick(RAYDIUM_MAX_TICK) == RAYDIUM_MAX_SQRT_PRICE_X64
    assert get_sqrt_price_at_tick(0) == Q64
    assert get_sqrt_price_at_tick(-1) == 18_445_821_805_675_395_072
    assert get_sqrt_price_at_tick(1) == 18_447_666_387_855_957_090
    assert get_sqrt_price_at_tick(-28_861) == 4_357_550_247_590_852_500
    assert get_sqrt_price_at_tick(28_861) == 78_090_291_009_052_504_402


@pytest.mark.parametrize("tick", [
    RAYDIUM_MIN_TICK, RAYDIUM_MIN_TICK + 1, -443_000, -100_000,
    -28_861, -1, 0, 1, 28_861, 100_000, 443_000,
    RAYDIUM_MAX_TICK - 1, RAYDIUM_MAX_TICK,
])
def test_tick_price_round_trip_and_floor_boundary(tick):
    price = get_sqrt_price_at_tick(tick)
    if tick < RAYDIUM_MAX_TICK:
        assert get_tick_at_sqrt_price(price) == tick
        assert get_tick_at_sqrt_price(price + 1) == tick
    if tick > RAYDIUM_MIN_TICK:
        assert get_tick_at_sqrt_price(price - 1) == tick - 1


def test_randomized_tick_prices_are_strictly_monotonic_and_invertible():
    rng = random.Random(931)
    samples = sorted({rng.randint(RAYDIUM_MIN_TICK, RAYDIUM_MAX_TICK - 1) for _ in range(300)})
    previous = None
    for tick in samples:
        price = get_sqrt_price_at_tick(tick)
        if previous is not None:
            assert price > previous
        assert get_tick_at_sqrt_price(price) == tick
        previous = price


@pytest.mark.parametrize("tick", [RAYDIUM_MIN_TICK - 1, RAYDIUM_MAX_TICK + 1, True, 1.5, "10"])
def test_rejects_invalid_tick_inputs(tick):
    with pytest.raises(RaydiumTickMathError):
        get_sqrt_price_at_tick(tick)


@pytest.mark.parametrize("price", [
    RAYDIUM_MIN_SQRT_PRICE_X64 - 1,
    RAYDIUM_MAX_SQRT_PRICE_X64,
    True,
    1.5,
    "18446744073709551616",
])
def test_rejects_invalid_sqrt_price_inputs(price):
    with pytest.raises(RaydiumTickMathError):
        get_tick_at_sqrt_price(price)
