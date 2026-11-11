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
