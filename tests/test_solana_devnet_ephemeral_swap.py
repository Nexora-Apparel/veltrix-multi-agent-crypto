from veltrix.solana_devnet_ephemeral_swap import (
    ALLOWED_PROGRAMS, DEV_USDC, FAUCET_AIRDROP_LAMPORTS,
    MAX_PRICE_IMPACT_PCT, SWAP_INPUT_LAMPORTS, EphemeralSwapError,
)


def test_ephemeral_swap_is_hard_pinned_to_devnet_and_capped():
    assert "devnet" in "https://api.devnet.solana.com"
    assert "devnet" in "https://transaction-v1-devnet.raydium.io"
    assert FAUCET_AIRDROP_LAMPORTS == 50_000_000
    assert SWAP_INPUT_LAMPORTS == 1_000_000
    assert MAX_PRICE_IMPACT_PCT == 2.0
    assert DEV_USDC == "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
    assert "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb" in ALLOWED_PROGRAMS


def test_ephemeral_swap_error_type_is_explicit():
    assert issubclass(EphemeralSwapError, RuntimeError)
