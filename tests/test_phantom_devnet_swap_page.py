from pathlib import Path


PAGE = Path(__file__).parents[1] / "wallet" / "phantom-devnet-swap.html"


def test_phantom_page_is_devnet_only_and_has_no_secret_input():
    source = PAGE.read_text(encoding="utf-8")
    assert "https://api.devnet.solana.com" in source
    assert "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG" in source
    assert "mainnet-beta" not in source.lower()
    assert "private key" in source.lower()
    assert "seed phrase" in source.lower()
    assert 'type="password"' not in source.lower()


def test_swap_limits_and_impact_gate_are_present():
    source = PAGE.read_text(encoding="utf-8")
    assert "MAX_LAMPORTS = 5000000" in source
    assert "lamports > MAX_LAMPORTS" in source
    assert "RESERVE_LAMPORTS = 50000000" in source
    assert "MAX_IMPACT = 2" in source
    assert "impact > MAX_IMPACT" in source


def test_phantom_signing_requires_explicit_user_click_and_confirmation():
    source = PAGE.read_text(encoding="utf-8")
    assert 'id="execute"' in source
    assert 'addEventListener("click"' in source
    assert "window.confirm(" in source
    assert "provider.signAndSendTransaction(tx" in source
    assert "await provider.connect()" in source


def test_unsigned_transactions_are_checked_and_simulated_before_signing():
    source = PAGE.read_text(encoding="utf-8")
    assert "numRequiredSignatures !== 1" in source
    assert "ALLOWED.has(id)" in source
    assert "simulateTransaction(tx" in source
    assert "fee payer" in source.lower()
    assert "canonical Devnet" in source
    assert "tokenBalanceAfter - BigInt(prepared.tokenBalanceBefore)" in source
    assert "Verified USDC increase" in source


def test_no_automatic_execution_on_page_load():
    source = PAGE.read_text(encoding="utf-8")
    script = source.split("<script>", 1)[1].split("</script>", 1)[0]
    assert '$("execute").addEventListener("click"' in script
    assert "window.onload" not in script
    assert "addEventListener('load'" not in script
    assert 'addEventListener("load"' not in script


def test_quote_and_signing_are_invalidated_on_account_or_amount_change():
    source = PAGE.read_text(encoding="utf-8")
    assert 'on("accountChanged"' in source
    assert 'on("disconnect"' in source
    assert '$("amount").addEventListener("input"' in source
    assert "provider.publicKey.equals(wallet)" in source
    assert "invalidatePrepared(" in source


def test_missing_negative_or_excessive_price_impact_fails_closed():
    source = PAGE.read_text(encoding="utf-8")
    assert "impact < 0 || impact > MAX_IMPACT" in source
    assert "Price impact is missing, invalid or above" in source
