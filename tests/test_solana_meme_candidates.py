import pytest

from veltrix.solana_meme.candidates import (
    CandidateRejected,
    SolanaCandidate,
    canonical_mint,
    deduplicate_candidates,
    normalize_candidate,
    validate_candidate,
)

MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_canonical_mint_accepts_valid_solana_public_key():
    assert canonical_mint(MINT_A) == MINT_A


@pytest.mark.parametrize("value", ["", "not-a-mint", " " + MINT_A, MINT_A + " "])
def test_canonical_mint_rejects_invalid_or_noncanonical_values(value):
    with pytest.raises(CandidateRejected):
        canonical_mint(value)


def test_normalize_dexscreener_shape_as_solana_candidate():
    candidate = normalize_candidate(
        {"chainId": "solana", "tokenAddress": MINT_A},
        source="dexscreener-token-profiles",
        observed_at_ms=1_800_000_000_000,
    )
    assert candidate == SolanaCandidate(
        mint=MINT_A,
        source="dexscreener-token-profiles",
        discovered_at_ms=1_800_000_000_000,
        sources=("dexscreener-token-profiles",),
    )
    assert candidate.chain == "solana"


@pytest.mark.parametrize("payload", [
    {"chainId": "base", "tokenAddress": MINT_A},
    {"chain": "bsc", "mint": MINT_A},
    {"chainId": "solana", "tokenAddress": "0x1234"},
    {"tokenAddress": MINT_A},
    {"chainId": None, "chain": "solana", "tokenAddress": MINT_A},
    {"chainId": "solana", "chain": "base", "tokenAddress": MINT_A},
    {"chainId": "solana", "chain": "", "tokenAddress": MINT_A},
    {"chainId": 101, "tokenAddress": MINT_A},
])
def test_normalize_rejects_other_chains_missing_or_conflicting_metadata(payload):
    with pytest.raises(CandidateRejected):
        normalize_candidate(payload, source="feed", observed_at_ms=100)


def test_normalize_rejects_non_object_payload():
    with pytest.raises(CandidateRejected):
        normalize_candidate(None, source="feed", observed_at_ms=100)


@pytest.mark.parametrize("timestamp", [0, -1, True, "100", None])
def test_normalize_rejects_invalid_timestamps(timestamp):
    with pytest.raises(CandidateRejected):
        normalize_candidate(
            {"chainId": "solana", "tokenAddress": MINT_A},
            source="feed",
            observed_at_ms=timestamp,
        )


@pytest.mark.parametrize("source", ["", " ", None, 123])
def test_normalize_rejects_missing_or_invalid_source(source):
    with pytest.raises(CandidateRejected):
        normalize_candidate(
            {"chainId": "solana", "tokenAddress": MINT_A},
            source=source,
            observed_at_ms=100,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mint": "not-a-mint", "source": "feed", "discovered_at_ms": 100},
        {"mint": MINT_A, "source": " ", "discovered_at_ms": 100},
        {"mint": MINT_A, "source": "feed", "discovered_at_ms": True},
        {"mint": MINT_A, "source": "feed", "discovered_at_ms": 0},
        {"mint": MINT_A, "source": "feed", "discovered_at_ms": 100, "sources": ["feed"]},
        {"mint": MINT_A, "source": "feed", "discovered_at_ms": 100, "sources": ("",)},
    ],
)
def test_candidate_constructor_cannot_bypass_validation(kwargs):
    with pytest.raises(CandidateRejected):
        SolanaCandidate(**kwargs)


def test_dedupe_merges_sources_and_keeps_latest_observation():
    first = normalize_candidate(
        {"chainId": "solana", "tokenAddress": MINT_A},
        source="feed-a", observed_at_ms=100,
    )
    second = normalize_candidate(
        {"chain": "solana", "mint": MINT_A},
        source="feed-b", observed_at_ms=200,
    )
    other = normalize_candidate(
        {"chain": "solana", "mint": MINT_B},
        source="feed-a", observed_at_ms=150,
    )
    result = deduplicate_candidates([first, second, other])
    assert len(result) == 2
    merged = next(item for item in result if item.mint == MINT_A)
    assert merged.discovered_at_ms == 200
    assert merged.source == "feed-b"
    assert merged.sources == ("feed-a", "feed-b")


def test_dedupe_rejects_unvalidated_objects():
    with pytest.raises(CandidateRejected):
        deduplicate_candidates([{"mint": MINT_A}])



def test_normalize_rejects_conflicting_mint_aliases():
    with pytest.raises(CandidateRejected, match="mint fields conflict"):
        normalize_candidate(
            {
                "chainId": "solana",
                "tokenAddress": MINT_A,
                "mint": MINT_B,
            },
            source="feed",
            observed_at_ms=100,
        )


def test_normalize_accepts_redundant_matching_mint_aliases():
    candidate = normalize_candidate(
        {
            "chainId": "solana",
            "chain": "SOLANA",
            "tokenAddress": MINT_A,
            "mint": MINT_A,
        },
        source=" feed-a ",
        observed_at_ms=100,
    )
    assert candidate.mint == MINT_A
    assert candidate.source == "feed-a"
    assert candidate.sources == ("feed-a",)


def test_candidate_canonicalizes_source_provenance():
    candidate = SolanaCandidate(
        mint=MINT_A,
        source=" feed-a ",
        discovered_at_ms=100,
        sources=("feed-b", " feed-a ", "feed-b"),
    )
    assert candidate.source == "feed-a"
    assert candidate.sources == ("feed-a", "feed-b")


def test_candidate_constructor_rejects_invalid_provenance_items():
    with pytest.raises(CandidateRejected):
        SolanaCandidate(
            mint=MINT_A,
            source="feed-a",
            discovered_at_ms=100,
            sources=("feed-b", None),
        )


def test_storage_boundary_revalidation_rejects_forged_frozen_dataclass():
    forged = object.__new__(SolanaCandidate)
    object.__setattr__(forged, "mint", "not-a-mint")
    object.__setattr__(forged, "source", "feed")
    object.__setattr__(forged, "discovered_at_ms", 100)
    object.__setattr__(forged, "sources", ("feed",))
    with pytest.raises(CandidateRejected):
        validate_candidate(forged)


def test_storage_boundary_revalidation_reconstructs_valid_candidate():
    original = normalize_candidate(
        {"chainId": "solana", "tokenAddress": MINT_A},
        source="feed-a",
        observed_at_ms=100,
    )
    checked = validate_candidate(original)
    assert checked == original
    assert checked is not original
