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
    assert "TEST_LAMPORTS = 1000000" in source
    assert "MAX_LAMPORTS = TEST_LAMPORTS" in source
    assert "lamports !== TEST_LAMPORTS" in source
    assert "lamports > MAX_LAMPORTS" in source
    assert "RESERVE_LAMPORTS = 50000000" in source
    assert "MAX_IMPACT = 2" in source
    assert "impact > MAX_IMPACT" in source


def test_phantom_signing_requires_explicit_user_click_and_confirmation():
    source = PAGE.read_text(encoding="utf-8")
    assert 'id="execute"' in source
    assert 'addEventListener("click"' in source
    assert "window.confirm(" in source
    assert "provider.signAndSendTransaction(finalTx" in source
    assert "await provider.connect()" in source


def test_unsigned_transactions_are_checked_and_simulated_before_signing():
    source = PAGE.read_text(encoding="utf-8")
    assert "numRequiredSignatures !== 1" in source
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


def test_amount_is_locked_during_quote_and_signing_operations():
    source = PAGE.read_text(encoding="utf-8")
    assert '$("amount").disabled = value' in source



def test_phantom_review_is_bound_to_exact_unsigned_transaction_bytes():
    source = PAGE.read_text(encoding="utf-8")
    assert "snapshotUnsignedTransaction(tx)" in source
    assert "assertExactReviewedBytes(" in source
    assert "reviewedBytes" in source
    assert "bytesEqual(currentBytes, reviewedBytes)" in source
    assert "roundTrip.serialize(), bytes" in source
    assert "finalTx = assertExactReviewedBytes(tx, prepared.reviewedBytes[i])" in source


def test_phantom_review_expires_and_rejects_multi_transaction_bundles():
    source = PAGE.read_text(encoding="utf-8")
    assert "built.data.length !== 1" in source
    assert "Date.now() - prepared.preparedAt > 60000" in source
    assert "prepared.walletAddress !== wallet.toBase58()" in source


def test_browser_semantic_verifier_has_no_signing_bypass():
    source = PAGE.read_text(encoding="utf-8")
    assert "async function inspectTransaction(tx, quote, reviewedBytes)" in source
    assert "async function resolveV0Accounts(message)" in source
    assert "async function verifyCpmmCpi(tx, policy, reviewedBytes, accountPresence)" in source
    assert "Transaction intent rejected:" in source



def test_browser_v0_decoder_resolves_canonical_devnet_alt_and_privileges():
    source = PAGE.read_text(encoding="utf-8")
    assert "AddressLookupTableAccount.deserialize(data)" in source
    assert "AddressLookupTab1e1111111111111111111111111" in source
    assert "getGenesisHash" in source
    assert 'commitment:"finalized"' in source
    assert "lastExtendedSlot" in source
    assert "lastExtendedSlotStartIndex" in source
    assert "lookup.writableIndexes" in source
    assert "lookup.readonlyIndexes" in source
    assert "effectivePrivileges(message, resolved)" in source
    assert "compiled instruction index out of bounds" in source


def test_browser_router_policy_checks_full_setup_route_cleanup_and_compute_budget():
    source = PAGE.read_text(encoding="utf-8")
    assert "function decodeRouterPolicy(message, resolved, quote)" in source
    assert "ROUTER_ACCOUNT_COUNT = 17" in source
    assert "expectedSetup" in source
    assert "expectedRoute" in source
    assert "expectedCleanup" in source
    assert "compute-unit price invalid" in source
    assert "compute-unit limit outside policy" in source
    assert "readU64(route.data,1) !== BigInt(TEST_LAMPORTS)" in source


def test_browser_cpmm_policy_checks_live_pool_and_inner_swap_cpi():
    source = PAGE.read_text(encoding="utf-8")
    assert "function decodeTokenAccount(" in source
    assert "function verifyPoolAndAccounts(" in source
    assert "findProgramAddressSync([POOL_AUTH_SEED]" in source
    assert "async function verifyCpmmCpi(tx, policy, reviewedBytes, accountPresence)" in source
    assert "CPMM_SWAP_DISC" in source
    assert "innerInstructions:true" in source
    assert "expected exactly one CPMM CPI" in source
    assert "outerMinimumEnforced:true" in source


def test_browser_semantic_audit_runs_before_review_and_before_signing():
    source = PAGE.read_text(encoding="utf-8")
    assert "await inspectTransaction(tx, q, snapshot)" in source
    assert "await inspectTransaction(tx, prepared.quote, prepared.reviewedBytes[i])" in source
    assert "Transaction intent rejected:" in source
    assert "provider.signAndSendTransaction(finalTx" in source
    assert "inspectTransaction(tx, quote, reviewedBytes)" in source



def test_browser_inner_cpi_policy_rejects_unexpected_effects():
    source = PAGE.read_text(encoding="utf-8")
    assert "const allowedPrograms = new Set([CPMM,TOKEN,TOKEN_2022,ATA_PROGRAM,SYSTEM])" in source
    assert "unexpected inner CPI program" in source
    assert "System transfer is not exact wrapped-SOL funding" in source
    assert "SPL transfer authority mismatch" in source
    assert "CloseAccount recipient/authority mismatch" in source
    assert "unexpected SPL transfer source/destination/amount" in source
    assert "outerMinimumEnforced:true" in source


def test_pool_vault_direction_is_bound_to_mint_order():
    source = PAGE.read_text(encoding="utf-8")
    assert "expectedVaultIn" in source
    assert "expectedVaultOut" in source
    assert "router vault direction does not match WSOL-to-USDC pool mint order" in source



def test_quote_is_locked_to_the_exactly_audited_single_hop_pool():
    source = PAGE.read_text(encoding="utf-8")
    assert "q.routePlan.length !== 1" in source
    assert "q.routePlan[0].poolId !== POOL" in source
    assert "Multi-hop or changed-pool routes are rejected." in source


def test_browser_verifier_checks_pool_token_programs_and_all_privileges():
    source = PAGE.read_text(encoding="utf-8")
    assert "const requiredPrivileges = [" in source
    assert "effective signer/writable privilege mismatch" in source
    assert "pool vault token-program owner does not match pool state" in source
    assert "pool mint account owner does not match pool token-program declarations" in source



def test_adversarial_node_harness_is_wired_into_ci():
    workflow = PAGE.parents[1] / ".github" / "workflows" / "raydium-devnet-swap-preflight.yml"
    harness = PAGE.parents[1] / "tests" / "phantom_verifier_adversarial.mjs"
    assert harness.exists()
    assert "node tests/phantom_verifier_adversarial.mjs" in workflow.read_text(encoding="utf-8")
    assert "window.__VELTRIX_TEST__ === true" in PAGE.read_text(encoding="utf-8")
