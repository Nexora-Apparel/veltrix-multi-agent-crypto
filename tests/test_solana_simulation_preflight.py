import pytest

from veltrix.providers.solana_simulation_preflight import (
    SolanaSimulationWalletPreflightProvider, TOKEN_PROGRAM_ID,
)

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "11111111111111111111111111111111"


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class Session:
    def __init__(self, token_accounts, sol=100_000_000):
        self.calls = []
        self.token_accounts = token_accounts
        self.sol = sol

    def post(self, url, json, timeout):
        self.calls.append(json)
        if json["method"] == "getTokenAccountsByOwner":
            result = {"context": {"slot": 500}, "value": self.token_accounts}
        else:
            result = {"context": {"slot": 501}, "value": self.sol}
        return Response({"jsonrpc": "2.0", "id": 1, "result": result})


def account(pubkey, raw):
    return {
        "pubkey": pubkey,
        "account": {
            "owner": TOKEN_PROGRAM_ID,
            "data": {"parsed": {"info": {
                "mint": MINT, "owner": WALLET, "state": "initialized",
                "tokenAmount": {"amount": str(raw), "decimals": 6},
            }}},
        },
    }


def test_preflight_reconciles_multiple_accounts_and_checks_exact_raw_amount():
    session = Session([account("token-account-a", 700), account("token-account-b", 500)])
    provider = SolanaSimulationWalletPreflightProvider(
        "https://rpc.example", WALLET, session=session, clock_ms=lambda: 1000,
    )
    result = provider.fetch(MINT, 1000)
    assert result.state == "BALANCE_READY"
    assert result.token_balance_raw == 1200
    assert result.token_account_count == 2
    assert result.sol_balance_lamports == 100_000_000
    assert result.finalized_slot == 500
    assert [x["method"] for x in session.calls] == ["getTokenAccountsByOwner", "getBalance"]


def test_preflight_marks_insufficient_token_balance_without_claiming_honeypot():
    session = Session([account("token-account-a", 99)])
    provider = SolanaSimulationWalletPreflightProvider("https://rpc.example", WALLET, session=session)
    result = provider.fetch(MINT, 100)
    assert result.state == "INSUFFICIENT_TOKEN_BALANCE"
    assert result.token_balance_raw == 99


def test_preflight_treats_empty_token_account_list_as_zero_balance():
    provider = SolanaSimulationWalletPreflightProvider(
        "https://rpc.example", WALLET, session=Session([]),
    )
    result = provider.fetch(MINT, 1)
    assert result.state == "INSUFFICIENT_TOKEN_BALANCE"
    assert result.token_account_count == 0


def test_preflight_rejects_owner_mint_and_raw_amount_mismatches():
    bad = account("token-account-a", 10)
    bad["account"]["data"]["parsed"]["info"]["owner"] = MINT
    provider = SolanaSimulationWalletPreflightProvider(
        "https://rpc.example", WALLET, session=Session([bad]),
    )
    with pytest.raises(Exception, match="owner or mint mismatch"):
        provider.fetch(MINT, 1)


def test_preflight_rejects_non_positive_amount():
    provider = SolanaSimulationWalletPreflightProvider("https://rpc.example", WALLET, session=Session([]))
    with pytest.raises(ValueError):
        provider.fetch(MINT, 0)
