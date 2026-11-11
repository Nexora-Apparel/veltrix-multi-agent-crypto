"""Integer-exact Raydium CLMM tick/sqrt-price math from the pinned source revision.

The inverse uses monotonic integer binary search rather than a floating-point
logarithm. It returns the greatest tick whose encoded sqrt price is <= input,
which is the documented Raydium contract.
"""
from __future__ import annotations

from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_SOURCE_COMMIT


RAYDIUM_MIN_TICK = -443636
RAYDIUM_MAX_TICK = 443636
RAYDIUM_MIN_SQRT_PRICE_X64 = 4_295_048_016
RAYDIUM_MAX_SQRT_PRICE_X64 = 79_226_673_521_066_979_257_578_248_091
Q64 = 1 << 64
U128_MAX = (1 << 128) - 1
SOURCE_COMMIT = RAYDIUM_CLMM_SOURCE_COMMIT

# Exact little-endian U128 low-limb constants from libraries/tick_math.rs.
_TICK_FACTORS = (
    0xFFFCB933BD6FB800,
    0xFFF97272373D4000,
    0xFFF2E50F5F657000,
    0xFFE5CACA7E10F000,
    0xFFCB9843D60F7000,
    0xFF973B41FA98E800,
    0xFF2EA16466C9B000,
    0xFE5DEE046A9A3800,
    0xFCBE86C7900BB000,
    0xF987A7253AC65800,
    0xF3392B0822BB6000,
    0xE7159475A2CAF000,
    0xD097F3BDFD2F2000,
    0xA9F746462D9F8000,
    0x70D869A156F31C00,
    0x31BE135F97ED3200,
    0x09AA508B5B85A500,
    0x005D6AF8DEDC582C,
    0x00002216E584F5FA,
)


class RaydiumTickMathError(ValueError):
    """Raised for an invalid Raydium tick or Q64.64 sqrt price."""


def _integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RaydiumTickMathError(f"{name} must be an integer")
    return value


def get_sqrt_price_at_tick(tick: int) -> int:
    """Return floor-compatible Raydium Q64.64 sqrt(token1/token0) at tick."""
    tick = _integer(tick, "tick")
    if not RAYDIUM_MIN_TICK <= tick <= RAYDIUM_MAX_TICK:
        raise RaydiumTickMathError("tick is outside the pinned Raydium range")
    absolute = abs(tick)
    ratio = _TICK_FACTORS[0] if absolute & 1 else Q64
    for bit_index in range(1, len(_TICK_FACTORS)):
        if absolute & (1 << bit_index):
            ratio = (ratio * _TICK_FACTORS[bit_index]) >> 64
    if tick > 0:
        ratio = U128_MAX // ratio
    if not 0 < ratio <= U128_MAX:
        raise RaydiumTickMathError("Raydium sqrt-price calculation overflowed U128")
    return ratio


def get_tick_at_sqrt_price(sqrt_price_x64: int) -> int:
    """Return the greatest tick whose Raydium sqrt price is <= the input."""
    sqrt_price_x64 = _integer(sqrt_price_x64, "sqrt_price_x64")
    if not RAYDIUM_MIN_SQRT_PRICE_X64 <= sqrt_price_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumTickMathError("sqrt price is outside the pinned Raydium range")
    low = RAYDIUM_MIN_TICK
    high = RAYDIUM_MAX_TICK - 1
    while low < high:
        middle = (low + high + 1) // 2
        if get_sqrt_price_at_tick(middle) <= sqrt_price_x64:
            low = middle
        else:
            high = middle - 1
    return low
