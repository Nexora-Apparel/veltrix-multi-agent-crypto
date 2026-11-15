"""Type-level separation between paper execution and read-only wallet observation.

No wallet secrets, signing implementation, transaction submission, or provider
connections are defined here. Paper components receive only a paper-ledger
identity. Wallet observation receives only a public wallet identity and a
strictly read-only capability set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WalletIsolationError(ValueError):
    """Raised when a wallet identity crosses an execution-domain boundary."""


class Chain(str, Enum):
    SOLANA = "solana"
    BASE = "base"
    BSC = "bsc"


class Network(str, Enum):
    MAINNET = "mainnet"
    DEVNET = "devnet"
    TESTNET = "testnet"


@dataclass(frozen=True, slots=True)
class WalletIdentity:
    """Public identity used exclusively for read-only wallet observation."""

    chain: Chain
    network: Network
    public_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.chain, Chain):
            raise WalletIsolationError("chain must be a supported Chain")
        if not isinstance(self.network, Network):
            raise WalletIsolationError("network must be a supported Network")
        if (
            not isinstance(self.public_key, str)
            or not self.public_key
            or self.public_key.strip() != self.public_key
        ):
            raise WalletIsolationError("public_key must be a non-empty canonical string")


@dataclass(frozen=True, slots=True)
class PaperLedgerIdentity:
    """Synthetic account identity; it is not a blockchain wallet address."""

    ledger_id: str
    account_id: str = "default"
    chain: Chain = Chain.SOLANA

    def __post_init__(self) -> None:
        for name, value in (("ledger_id", self.ledger_id), ("account_id", self.account_id)):
            if not isinstance(value, str) or not value or value.strip() != value:
                raise WalletIsolationError(f"{name} must be a non-empty canonical string")
        if not isinstance(self.chain, Chain):
            raise WalletIsolationError("chain must be a supported Chain")


READ_ONLY_CAPABILITIES = frozenset({"balance", "transactions"})


@dataclass(frozen=True, slots=True)
class PaperExecutionContext:
    """Only the synthetic paper ledger may enter the paper execution domain."""

    ledger: PaperLedgerIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.ledger, PaperLedgerIdentity):
            raise WalletIsolationError(
                "paper execution accepts PaperLedgerIdentity only; live wallets are isolated"
            )


@dataclass(frozen=True, slots=True)
class ReadOnlyWalletContext:
    """Public wallet observation context with no mutation or signing capability."""

    wallet: WalletIdentity
    capabilities: frozenset[str] = field(default=READ_ONLY_CAPABILITIES)

    def __post_init__(self) -> None:
        if not isinstance(self.wallet, WalletIdentity):
            raise WalletIsolationError(
                "wallet observation accepts WalletIdentity only; paper ledgers are isolated"
            )
        if not isinstance(self.capabilities, frozenset):
            raise WalletIsolationError("capabilities must be an immutable frozenset")
        if not self.capabilities.issubset(READ_ONLY_CAPABILITIES):
            raise WalletIsolationError("wallet observation permits read-only capabilities only")


class WalletIsolation:
    """Explicit constructors; intentionally exposes no signer or live executor."""

    @staticmethod
    def paper(ledger: PaperLedgerIdentity) -> PaperExecutionContext:
        return PaperExecutionContext(ledger=ledger)

    @staticmethod
    def observe(wallet: WalletIdentity) -> ReadOnlyWalletContext:
        return ReadOnlyWalletContext(wallet=wallet)
