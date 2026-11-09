from veltrix.providers.solana_mint import (
    SolanaMintAuthoritySnapshot,
    TOKEN_PROGRAM_ID,
)
from veltrix.solana_meme.risk_smoke import BONK_MINT, run_risk_smoke


class FakeMintProvider:
    def fetch_snapshot(self, mint):
        assert mint == BONK_MINT
        return SolanaMintAuthoritySnapshot(
            mint=mint,
            provider="solana-finalized-mint-account",
            evidence_id="evidence",
            observed_at_ms=1000,
            finalized_slot=123,
            token_program_id=TOKEN_PROGRAM_ID,
            mint_authority=None,
            freeze_authority=None,
            supply_raw=100,
            decimals=5,
            extension_types=(),
            unknown_extension_ids=(),
            extension_tlv_structure_valid=True,
            account_data_sha256="c" * 64,
        )


def test_risk_smoke_validates_finalized_mint_identity_and_supply():
    result = run_risk_smoke(provider=FakeMintProvider())
    assert result.mint_snapshot.mint == BONK_MINT
    assert result.mint_snapshot.supply_raw == 100
