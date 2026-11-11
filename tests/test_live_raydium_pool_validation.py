from __future__ import annotations

from veltrix.live_raydium_pool_validation import HeliusHeaderSession


class CaptureSession:
    def __init__(self):
        self.args = None
        self.kwargs = None

    def post(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return "response"


def test_helius_api_key_uses_documented_x_api_key_header():
    session = HeliusHeaderSession("test-key")
    capture = CaptureSession()
    session._session = capture

    result = session.post(
        "https://mainnet.helius-rpc.com/",
        json={"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []},
        headers={"Content-Type": "application/json"},
        timeout=12,
        allow_redirects=False,
    )

    assert result == "response"
    assert capture.args == ("https://mainnet.helius-rpc.com/",)
    assert capture.kwargs["headers"]["X-Api-Key"] == "test-key"
    assert "api-key" not in capture.kwargs["headers"]
    assert "api-key" not in capture.args[0]


def test_helius_header_session_does_not_add_auth_header_without_key():
    session = HeliusHeaderSession(None)
    capture = CaptureSession()
    session._session = capture

    session.post(
        "https://mainnet.helius-rpc.com/",
        json={"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []},
        headers={"Content-Type": "application/json"},
        timeout=12,
        allow_redirects=False,
    )

    assert capture.kwargs["headers"] == {"Content-Type": "application/json"}


def test_mainnet_genesis_hash_matches_canonical_solana_mainnet():
    from veltrix.live_raydium_pool_validation import MAINNET_GENESIS

    assert MAINNET_GENESIS == "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"



def test_vault_authority_must_match_pool_address():
    from types import SimpleNamespace

    import pytest
    from solders.pubkey import Pubkey
    from veltrix.live_raydium_pool_validation import decode_token_vault

    mint = Pubkey.new_unique()
    authority = Pubkey.new_unique()
    wrong_authority = Pubkey.new_unique()
    data = bytes(mint) + bytes(authority) + (123).to_bytes(8, "little") + bytes(93)
    account = SimpleNamespace(
        address=str(Pubkey.new_unique()),
        owner="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        data=data,
    )

    decoded = decode_token_vault(account, str(mint), str(authority))
    assert decoded["token_authority"] == str(authority)

    with pytest.raises(ValueError, match="authority does not match"):
        decode_token_vault(account, str(mint), str(wrong_authority))



def test_authoritative_snapshot_rejects_changed_pool_dependencies():
    import pytest
    from veltrix.live_raydium_pool_validation import require_stable_pool_dependencies

    discovered = ("config-a", "observation-a", "vault-a", "vault-b")
    require_stable_pool_dependencies(discovered, discovered)

    changed = ("config-a", "observation-a", "vault-a", "vault-c")
    with pytest.raises(ValueError, match="dependencies changed"):
        require_stable_pool_dependencies(discovered, changed)
