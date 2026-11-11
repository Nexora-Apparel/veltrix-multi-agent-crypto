from solders.pubkey import Pubkey

from veltrix.config import DEFAULT_JUPITER_TAKER_PUBLIC_KEY, settings


def test_configured_jupiter_taker_is_the_supplied_canonical_public_key():
    configured = settings()
    assert configured.jupiter_taker_public_key == "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"
    assert configured.jupiter_taker_public_key == DEFAULT_JUPITER_TAKER_PUBLIC_KEY
    assert str(Pubkey.from_string(configured.jupiter_taker_public_key)) == configured.jupiter_taker_public_key


def test_jupiter_taker_configuration_does_not_enable_live_trading():
    configured = settings()
    assert configured.jupiter_taker_public_key
    assert configured.paper_trading_enabled is False


def test_order_adapter_uses_configured_taker_without_reimplementing_order_intake(monkeypatch):
    from types import SimpleNamespace

    from solders.pubkey import Pubkey

    from veltrix import config
    from veltrix.jupiter_order import JupiterSwapV2OrderAdapter

    taker = "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"
    monkeypatch.setattr(config, "settings", lambda: SimpleNamespace(jupiter_taker_public_key=taker))
    adapter = object.__new__(JupiterSwapV2OrderAdapter)
    captured = {}

    def existing_order_intake(input_mint, output_mint, amount_raw, *, taker, policy):
        captured.update(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_raw=amount_raw,
            taker=taker,
            policy=policy,
        )
        return "unsigned-order"

    monkeypatch.setattr(adapter, "order_exact_in", existing_order_intake)
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    policy = object()

    result = adapter.order_exact_in_for_configured_taker(
        input_mint,
        output_mint,
        123456,
        policy=policy,
    )

    assert result == "unsigned-order"
    assert captured == {
        "input_mint": input_mint,
        "output_mint": output_mint,
        "amount_raw": 123456,
        "taker": taker,
        "policy": policy,
    }
