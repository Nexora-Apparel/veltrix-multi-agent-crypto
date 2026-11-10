import dataclasses

import pytest

from veltrix.wallet_isolation import (
    Chain,
    Network,
    PaperExecutionContext,
    PaperLedgerIdentity,
    ReadOnlyWalletContext,
    WalletIdentity,
    WalletIsolation,
    WalletIsolationError,
)


def test_paper_execution_accepts_only_synthetic_ledger_identity():
    ledger = PaperLedgerIdentity(ledger_id="paper-ledger-001")
    context = WalletIsolation.paper(ledger)

    assert isinstance(context, PaperExecutionContext)
    assert context.ledger is ledger
    assert not hasattr(context, "wallet")
    assert not hasattr(context, "signer")


def test_paper_execution_rejects_live_wallet_identity():
    wallet = WalletIdentity(
        chain=Chain.SOLANA,
        network=Network.MAINNET,
        public_key="PublicWalletAddress",
    )
    with pytest.raises(WalletIsolationError, match="PaperLedgerIdentity only"):
        PaperExecutionContext(ledger=wallet)


def test_read_only_observation_accepts_public_wallet_identity():
    wallet = WalletIdentity(
        chain=Chain.SOLANA,
        network=Network.MAINNET,
        public_key="PublicWalletAddress",
    )
    context = WalletIsolation.observe(wallet)

    assert isinstance(context, ReadOnlyWalletContext)
    assert context.wallet is wallet
    assert context.capabilities == frozenset({"balance", "transactions"})
    assert not hasattr(context, "signer")
    assert not hasattr(context, "submit_transaction")


def test_read_only_observation_rejects_paper_ledger_identity():
    ledger = PaperLedgerIdentity(ledger_id="paper-ledger-001")
    with pytest.raises(WalletIsolationError, match="WalletIdentity only"):
        ReadOnlyWalletContext(wallet=ledger)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chain": "solana", "network": Network.MAINNET, "public_key": "wallet"},
        {"chain": Chain.SOLANA, "network": "mainnet", "public_key": "wallet"},
        {"chain": Chain.SOLANA, "network": Network.MAINNET, "public_key": ""},
        {"chain": Chain.SOLANA, "network": Network.MAINNET, "public_key": " wallet"},
    ],
)
def test_wallet_identity_rejects_invalid_fields(kwargs):
    with pytest.raises(WalletIsolationError):
        WalletIdentity(**kwargs)


@pytest.mark.parametrize("chain", ["solana", "unknown", None])
def test_paper_ledger_rejects_untyped_chain(chain):
    with pytest.raises(WalletIsolationError):
        PaperLedgerIdentity(ledger_id="paper-ledger", chain=chain)


@pytest.mark.parametrize("capabilities", [
    {"balance"},
    frozenset({"balance", "sign"}),
    frozenset({"balance", "submit"}),
    frozenset({"write"}),
])
def test_read_only_context_rejects_mutating_or_mutable_capabilities(capabilities):
    wallet = WalletIdentity(
        chain=Chain.BASE,
        network=Network.MAINNET,
        public_key="0xPublicAddress",
    )
    with pytest.raises(WalletIsolationError):
        ReadOnlyWalletContext(wallet=wallet, capabilities=capabilities)


def test_wallet_identities_are_immutable_and_domain_types_are_distinct():
    paper = PaperLedgerIdentity(ledger_id="paper-ledger")
    wallet = WalletIdentity(
        chain=Chain.BSC,
        network=Network.MAINNET,
        public_key="0xPublicAddress",
    )
    assert PaperLedgerIdentity is not WalletIdentity
    with pytest.raises(dataclasses.FrozenInstanceError):
        paper.ledger_id = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        wallet.public_key = "changed"


def test_contexts_expose_no_secret_or_signing_fields():
    paper_fields = {field.name for field in dataclasses.fields(PaperExecutionContext)}
    read_only_fields = {field.name for field in dataclasses.fields(ReadOnlyWalletContext)}
    assert paper_fields == {"ledger"}
    assert read_only_fields == {"wallet", "capabilities"}
    assert "private_key" not in {field.name for field in dataclasses.fields(WalletIdentity)}
    assert "seed_phrase" not in {field.name for field in dataclasses.fields(WalletIdentity)}
