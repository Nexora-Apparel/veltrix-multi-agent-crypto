"""Opt-in, read-only production acceptance probes.

These tests never submit transactions. They validate endpoint reachability and
basic response identity/shape only; they do not certify token safety. Set
VELTRIX_RUN_LIVE_PROVIDER_ACCEPTANCE=true and the documented environment
variables to execute them. Without credentials/addresses they are skipped.
"""
import os
import pytest
import requests


RUN_LIVE = os.getenv("VELTRIX_RUN_LIVE_PROVIDER_ACCEPTANCE", "").lower() == "true"
pytestmark = pytest.mark.skipif(
    not RUN_LIVE, reason="live provider acceptance is opt-in and requires operator credentials"
)


@pytest.mark.parametrize("chain,network", [
    ("solana", "sol"),
    ("base", "base"),
    ("bsc", "bsc"),
])
def test_insightx_read_only_cluster_endpoint(chain, network):
    token = os.getenv(f"VELTRIX_ACCEPTANCE_{chain.upper()}_TOKEN")
    api_key = os.getenv("INSIGHTX_API_KEY")
    if not token or not api_key:
        pytest.skip(f"missing {chain} acceptance token or INSIGHTX_API_KEY")
    response = requests.get(
        f"https://api.insightx.network/dex-metrics/v1/{network}/{token}/clusters",
        headers={"X-API-Key": api_key, "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict)
    # The public docs do not publish a stable detailed response schema.
    # Validate only a non-empty JSON object; never infer completeness from it.
    assert payload


@pytest.mark.parametrize("chain,chain_id", [("base", "8453"), ("bsc", "56")])
def test_goplus_read_only_locker_endpoint(chain, chain_id):
    token = os.getenv(f"VELTRIX_ACCEPTANCE_{chain.upper()}_TOKEN")
    access_token = os.getenv("GOPLUS_ACCESS_TOKEN")
    if not token or not access_token:
        pytest.skip(f"missing {chain} acceptance token or GOPLUS_ACCESS_TOKEN")
    response = requests.get(
        "https://api.gopluslabs.io/open/api/v1/locks/token",
        params={"chainId": chain_id, "pageNum": 1, "pageSize": 100, "tokenAddress": token},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict)
