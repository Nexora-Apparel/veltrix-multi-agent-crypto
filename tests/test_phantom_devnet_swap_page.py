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


def test_swap_accepts_dynamic_sol_input_and_has_no_universal_price_impact_ceiling():
    source = PAGE.read_text(encoding="utf-8")
    assert "function parseSolAmountToLamports(value)" in source
    assert 'const lamports = parseSolAmountToLamports($("amount").value)' in source
    assert "quote.inputAmount" in source
    assert "policy.inputAmount" in source
    assert "expectedInputRaw" in source
    assert "TEST_LAMPORTS" not in source
    assert "MAX_LAMPORTS" not in source
    assert "RESERVE_LAMPORTS" not in source
    assert "0.05 SOL reserve" not in source
    assert "MAX_IMPACT" not in source
    assert "impact>MAX_IMPACT" not in source
    assert "impact<0" not in source


def test_devnet_faucet_topups_are_chunked_before_transaction_construction():
    source = PAGE.read_text(encoding="utf-8")
    assert "async function ensureDevnetFunds(requiredBalanceLamports)" in source
    assert "connection.requestAirdrop(wallet, Number(topUp))" in source
    assert 'connection.confirmTransaction(lastSignature, "confirmed")' in source
    assert "await ensureDevnetFunds(lamports + 10000000n)" in source
    assert "const topUp = deficit > 1000000000n ? 1000000000n : deficit" in source
    assert "canonical Devnet identity check failed" in source
    assert "Devnet faucet could not supply the remaining test balance" in source


def test_phantom_swap_requires_explicit_user_approval_after_final_freshness_gate():
    source = PAGE.read_text(encoding="utf-8")
    assert '<button id="execute" disabled>Review and approve in Phantom</button>' in source
    assert "User-approved Phantom signing · Solana Devnet only" in source
    assert "Nothing is sent unless you approve it in Phantom." in source
    execute = source[source.index("async function execute()"):source.index("if (window.__VELTRIX_TEST__")]
    assert "await finalPreSignFreshnessGate()" in execute
    assert "provider.signTransaction(finalTx)" in execute
    assert "provider.publicKey.equals(wallet)" in execute
    assert "confirmTransaction(signature" in execute
    assert "getTransaction(signature" in execute
    assert "reconcileConfirmedTransaction(" in execute
    assert "CONFIRMED_SEMANTIC_MATCH_BUT_BYTE_MISMATCH" in source
    assert "firstByteDifference" in source
    assert "reviewedMessageSha256" in source
    assert "tokenBalanceRaw()" in execute
    assert "window.confirm(" not in source
    assert "await provider.connect()" in source


def test_phantom_submission_is_one_shot_and_never_automatically_retried():
    source = PAGE.read_text(encoding="utf-8")
    execute = source[source.index("async function execute()"):source.index("if (window.__VELTRIX_TEST__")]
    assert "prepared.executionStarted" in execute
    assert "Do not retry" in execute
    assert "received < BigInt(reviewed.quote.otherAmountThreshold)" in execute
    assert "FAILED_ON_CHAIN" in execute
    assert "CONFIRMED_BUT_UNVERIFIED" in execute
    assert "SUBMISSION_STATUS_UNKNOWN" in execute


def test_unsigned_transactions_are_checked_and_simulated_before_signing():
    source = PAGE.read_text(encoding="utf-8")
    assert "numRequiredSignatures!==1" in source
    assert "simulateTransaction" in source
    assert "fee payer" in source.lower()
    assert "canonical Devnet" in source


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


def test_price_impact_is_informational_and_not_a_universal_rejection_gate():
    source = PAGE.read_text(encoding="utf-8")
    assert "if (!Number.isFinite(impact))" in source
    assert '$("impactValue").textContent=impact.toFixed(3)+"%"' in source
    assert "if (!Number.isFinite(freshImpact) || freshOutputRaw < reviewedMinimum)" in source
    assert "MAX_IMPACT" not in source
    assert "Direct CPMM price impact exceeds the 2% safety ceiling." not in source



def test_execution_protection_is_automatic_and_minimum_is_sealed_into_transaction():
    source = PAGE.read_text(encoding="utf-8")
    engine = (PAGE.parent / "automatic-exchange-pricing.mjs").read_text(encoding="utf-8")
    assert 'id="slippage"' not in source
    assert "function parseSlippageBps(value)" not in source
    assert "computeProtectionBps" in source
    assert "calculateMinimumOutput" in source
    assert "createQuoteSnapshot" in source
    assert "quoteSnapshot.minimumOutputRaw !== q.otherAmountThreshold" in source
    assert 'id="protectionValue"' in source
    assert "maximum slippage" not in source.lower()
    assert "quoteAgeMs" in engine and "liquidityUsd" in engine and "volatilityBps" in engine
    assert "expiresAtMs" in engine


def test_confirmation_reconciles_exact_input_minimum_and_actual_wallet_output():
    source = PAGE.read_text(encoding="utf-8")
    assert "expectedInputRaw" in source
    assert "BigInt(String(expectedInputRaw))" in source
    assert "actualOutputRaw" in source
    assert "received.toString() !== reconciliation.diagnostics.actualOutputRaw" in source
    assert "walletUsdcBalanceDeltaBelowMinimum" in source


def test_only_user_editable_amount_invalidates_prepared_review():
    source = PAGE.read_text(encoding="utf-8")
    assert '$("amount").addEventListener("input"' in source
    assert '$("slippage").addEventListener("input"' not in source
    assert 'id="slippage"' not in source

def test_amount_is_locked_during_quote_and_signing_operations():
    source = PAGE.read_text(encoding="utf-8")
    assert '$("amount").disabled = value' in source



def test_phantom_review_is_bound_to_exact_unsigned_transaction_bytes():
    source = PAGE.read_text(encoding="utf-8")
    assert "snapshotUnsignedTransaction(tx)" in source
    assert "assertExactReviewedBytes(" in source
    assert "reviewedBytes" in source
    assert "bytesEqual(serializeUnsignedTransaction(tx),reviewedBytes)" in source
    assert "serializeUnsignedTransaction(parsed),bytes" in source
    assert "assertExactReviewedBytes(tx,prepared.reviewedBytes[0])" in source


def test_phantom_review_expires_and_rejects_multi_transaction_bundles():
    source = PAGE.read_text(encoding="utf-8")
    assert "prepared.txs.length !== 1" in source
    assert "prepared.reviewedBytes.length !== 1" in source
    assert "Date.now() - prepared.preparedAt > 15000" in source
    assert "prepared.walletAddress !== wallet.toBase58()" in source


def test_browser_semantic_verifier_has_no_signing_bypass():
    source = PAGE.read_text(encoding="utf-8")
    assert "async function inspectTransaction(tx, quote, reviewedBytes)" in source
    assert "async function resolveV0Accounts(message)" in source
    assert "async function verifyCpmmCpi(tx, policy, reviewedBytes, accountPresence)" in source
    assert "Transaction intent rejected:" in source



def test_browser_v0_decoder_resolves_canonical_devnet_alt_and_privileges():
    source = PAGE.read_text(encoding="utf-8")
    assert "AddressLookupTableAccount.deserialize(accountDataBytes(info" in source
    assert "AddressLookupTab1e1111111111111111111111" in source
    assert "message.addressTableLookups.length>32" in source
    assert "duplicate address lookup table reference" in source
    assert "tableKey !== ALT_ADDRESS" not in source
    assert "getGenesisHash" in source
    assert 'commitment:"finalized"' in source
    assert "lastExtendedSlot" in source
    assert "lastExtendedSlotStartIndex" in source
    assert "lookup.writableIndexes" in source
    assert "lookup.readonlyIndexes" in source
    assert "effectivePrivileges(message, resolved)" in source
    assert "compiled instruction index outside dynamically resolved account set" in source


def test_browser_router_policy_checks_full_setup_route_cleanup_and_compute_budget():
    source = PAGE.read_text(encoding="utf-8")
    assert "function decodeRouterPolicy(message, resolved, quote)" in source
    assert "ROUTER_ACCOUNT_COUNT = 17" in source
    assert "expectedSetup" in source
    assert "expectedRoute" in source
    assert "expectedCleanup" in source
    assert "compute-unit price invalid" in source
    assert "compute-unit limit outside policy" in source
    assert "readU64(route.data,1) !== expectedInput" in source
    assert "const expectedInput = BigInt(String(quote.inputAmount))" in source


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
    assert "await inspectDirectCpmmTransaction(tx,q,reviewedBytes)" in source
    assert "Transaction intent rejected:" in source
    assert "provider.signTransaction(finalTx)" in source
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
    assert "raydium.cpmm.getPoolInfoFromRpc(POOL)" in source
    assert "poolInfo.id !== POOL || poolInfo.programId !== CPMM" in source
    assert "raydium.cpmm.swap(" in source
    assert "const minimumOut = new BN(pricing.calculateMinimumOutput(swapResult.outputAmount.toString(), protectionBps))" in source
    assert "slippage:protectionBps/10000" in source
    assert "measurePoolVolatilityBps(raydium, baseIn)" in source
    assert "const TRADE =" not in source


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



def test_cpmm_and_token_account_decoders_are_exposed_only_to_guarded_tests():
    source = PAGE.read_text(encoding="utf-8")
    assert "function decodeCpmmSwapData(data, expectedMinimum=0n, expectedInput=null)" in source
    assert "function decodeTokenAccount(" in source
    assert "decodeCpmmSwapData,decodeTokenAccount" in source
    harness = PAGE.parents[1] / "tests" / "phantom_verifier_adversarial.mjs"
    harness_source = harness.read_text(encoding="utf-8")
    assert "wrong CPMM discriminator" in harness_source
    assert "altered CPMM input" in harness_source
    assert "wrong token mint" in harness_source
    assert "wrong token owner" in harness_source



def test_direct_cpmm_builder_is_pinned_and_never_signs_or_submits():
    root = PAGE.parents[1]
    source = (root / "scripts" / "build-devnet-cpmm-unsigned.mjs").read_text(encoding="utf-8")
    package = (root / "package.json").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "raydium-devnet-swap-preflight.yml").read_text(encoding="utf-8")
    assert '"@raydium-io/raydium-sdk-v2": "0.2.64-alpha"' in package
    assert 'const POOL = "5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo"' in source
    assert 'const INPUT = parseSolToLamports(process.env.SWAP_INPUT_SOL || "0.001")' in source
    assert 'process.env.SWAP_SLIPPAGE_BPS || "100"' in source
    assert "slippage:SLIPPAGE_BPS/10000" in source
    assert "priceImpactPct" in source
    assert "no universal threshold blocks the build" in source
    assert "priceImpactPct >" not in source
    assert 'SWAP_INPUT_SOL: "0.005"' in workflow
    assert 'SWAP_SLIPPAGE_BPS: "250"' in workflow
    assert 'EXPECT_IMPACT_ABOVE_2: "true"' in workflow
    assert 'cluster:"devnet"' in source
    assert "getPoolInfoFromRpc(POOL)" in source
    assert "raydium.cpmm.swap(" in source
    assert "sigVerify:false" in source
    assert 'evidence_type:"veltrix_direct_devnet_cpmm_unsigned_transaction"' in source
    assert "signed:false,submitted:false,broadcast:false" in source
    assert "tx.signatures.some(item=>item.signature !== null)" in source
    assert "tx.sign(" not in source
    assert "sendRawTransaction" not in source
    assert "devnet:cpmm:build" in workflow



def test_browser_direct_cpmm_verifier_checks_pool_accounts_and_runtime_slippage():
    source = PAGE.read_text(encoding="utf-8")
    assert "function decodeDirectCpmmPolicy(message, resolved, quote)" in source
    assert "async function inspectDirectCpmmTransaction(tx,quote,reviewedBytes)" in source
    assert "async function verifyDirectCpmmRuntime(tx,policy,reviewedBytes,accountPresence)" in source
    assert "temporary WSOL System CreateAccountWithSeed shape mismatch" in source
    assert "direct CPMM exact input/output token transfer CPIs not proven" in source
    assert "direct CPMM accepted minimum output one raw unit above actual output" in source
    assert "inspectDirectCpmmTransaction" in source


def test_fresh_direct_cpmm_transaction_is_audited_in_browser_harness():
    root = PAGE.parents[1]
    harness = root / "tests" / "phantom_direct_cpmm_live_audit.mjs"
    workflow = root / ".github" / "workflows" / "raydium-devnet-swap-preflight.yml"
    assert harness.exists()
    assert "inspectDirectCpmmTransaction(tx" in harness.read_text(encoding="utf-8")
    assert "node tests/phantom_direct_cpmm_live_audit.mjs" in workflow.read_text(encoding="utf-8")



def test_browser_alt_evidence_uses_deserialized_state_address_count():
    source = PAGE.read_text(encoding="utf-8")
    assert "addresses:state.addresses.length" in source
    assert "addresses:addresses.length" not in source



def test_amm_config_discriminator_is_compared_as_raw_bytes():
    source = PAGE.read_text(encoding="utf-8")
    assert "expectedConfigDisc = Uint8Array.from([218,244,33,104,203,203,43,111])" in source
    assert "equalBytes(configData.slice(0,8),expectedConfigDisc)" in source
    assert 'b58encode(accountDataBytes(config,"AMM config").slice(0,8)) !== "daf42168cbcb2b6f"' not in source



def test_browser_ui_uses_direct_cpmm_sdk_and_only_enables_user_approved_signing():
    source = PAGE.read_text(encoding="utf-8")
    assert 'import("https://esm.sh/@raydium-io/raydium-sdk-v2@0.2.64-alpha?bundle")' in source
    assert "getPoolInfoFromRpc(POOL)" in source
    assert "inspectDirectCpmmTransaction(tx,q,reviewedBytes)" in source
    assert "provider.signTransaction(finalTx)" in source
    assert "await finalPreSignFreshnessGate()" in source
    assert '$("execute").disabled=false' in source
    assert '$("execute").textContent="Review and approve in Phantom"' in source



def test_devnet_ci_uses_fresh_direct_cpmm_audit_without_stale_router_fixture():
    workflow = (PAGE.parents[1] / ".github" / "workflows" / "raydium-devnet-swap-preflight.yml").read_text(encoding="utf-8")
    assert "node tests/phantom_direct_cpmm_live_audit.mjs" in workflow
    assert "python -m veltrix.solana_devnet_transaction_audit" not in workflow
    assert "python -m veltrix.solana_devnet_swap_preflight" not in workflow
    assert "try_ephemeral_swap" not in workflow


def test_final_pre_sign_freshness_gate_rechecks_wallet_balance_blockhash_pool_quote_and_exact_bytes():
    source = PAGE.read_text(encoding="utf-8")
    assert "async function finalPreSignFreshnessGate()" in source
    gate = source[source.index("async function finalPreSignFreshnessGate()"):source.index("async function execute()")]
    assert "Date.now() - prepared.preparedAt > 15000" in gate
    assert "provider.publicKey.equals(wallet)" in gate
    assert "connection.getGenesisHash()" in gate
    assert "BigInt(prepared.requiredBalanceLamports)" in gate
    assert "connection.isBlockhashValid(normalizedTransactionMessage(tx).recentBlockhash" in gate
    assert "raydium.cpmm.getPoolInfoFromRpc(POOL)" in gate
    assert "freshOutputRaw < reviewedMinimum" in gate
    assert "freshMinimum.toString() !== prepared.quote.otherAmountThreshold" not in gate
    assert "fresh output=" in gate
    assert "freshImpact > MAX_IMPACT" not in gate
    assert "freshOutputRaw < reviewedMinimum" in gate
    assert "await inspectDirectCpmmTransaction(tx,prepared.quote,prepared.reviewedBytes[0])" in gate
    assert "assertExactReviewedBytes(tx,prepared.reviewedBytes[0])" in gate
    execute = source[source.index("async function execute()"):source.index("if (window.__VELTRIX_TEST__")]
    assert "await finalPreSignFreshnessGate()" in execute
    assert "provider.signTransaction(finalTx)" in execute



def test_missing_or_invalid_network_fee_estimate_fails_closed():
    source = PAGE.read_text(encoding="utf-8")
    assert "if (!fee || !Number.isSafeInteger(fee.value) || fee.value < 0)" in source
    assert "Network fee estimate is unavailable or invalid; transaction preparation fails closed." in source
    assert "const totalFee=fee.value;" in source


def test_quote_review_displays_exact_output_automatic_protection_and_fee_items():
    source = PAGE.read_text(encoding="utf-8").read_text(encoding="utf-8")
    assert 'id="outputRawValue"' in source
    assert 'id="minimumRawValue"' in source
    assert 'id="protectionValue"' in source
    assert 'id="quoteExpiryValue"' in source
    assert 'id="dexFeeValue"' in source
    assert 'id="routeFeeValue"' in source
    assert 'id="platformFeeValue"' in source
    assert "function fmtFixedRaw(raw, decimals)" in source
    assert 'textContent=fmtFixedRaw(quotedRaw,6)+" USDC"' in source
    assert 'textContent=fmtFixedRaw(minimumRaw,6)+" USDC"' in source


def test_direct_cpmm_builder_emits_phantom_compatible_legacy_transaction_without_alt():
    root = PAGE.parents[1]
    builder = (root / "scripts" / "build-devnet-cpmm-unsigned.mjs").read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    harness = (root / "tests" / "phantom_direct_cpmm_live_audit.mjs").read_text(encoding="utf-8")
    assert "txVersion:TxVersion.LEGACY" in builder
    assert "tx.serialize({requireAllSignatures:false,verifySignatures:false})" in builder
    assert "built.transaction.setSigners(owner)" in builder
    assert "connection.getLatestBlockhash(\"confirmed\")" in builder
    assert "built.transaction.setSigners(wallet)" in page
    assert "connection.getLatestBlockhash(\"confirmed\")" in page
    assert "1232-byte packet limit" in builder
    assert 'transaction_version:"legacy"' in builder
    assert "address_lookup_tables:[]" in builder
    assert "TxVersion.LEGACY" in page
    assert "resolveLegacyAccounts(message)" in page
    assert "Phantom KMS-safe execution requires a legacy transaction with no address lookup tables" in page
    assert "resolveV0Accounts(message)" in page
    assert "duplicate public key in resolved v0 account set" in page
    assert "legacy transaction unexpectedly references address lookup tables" in page
    assert "provider.signTransaction(finalTx)" in page
    assert "connection.sendRawTransaction(signedWireBytes" in page
    assert "assertSignedMessageUnchanged(reviewedMessageBytes,signedMessageBytes)" in page
    assert "Transaction.from(bytes)" in harness
    assert 'assert.equal(artifact.transaction_version,"legacy")' in harness


def test_wallet_capability_adapter_requires_legacy_before_quote_and_approval():
    source = PAGE.read_text(encoding="utf-8")
    assert "function detectWalletCapabilities(p)" in source
    assert 'advertised.has("legacy")' in source
    assert 'advertised.includes("legacy")' in source
    assert "walletCapabilities=detectWalletCapabilities(provider)" in source
    assert "return {legacy:true,v0:false" in source
    assert 'advertised.has(0)||advertised.has("0")' not in source
    assert 'advertised.includes(0)||advertised.includes("0")' not in source
    assert "if(!walletCapabilities.legacy || !prepared" in source
    assert "Connected Phantom wallet does not expose the required legacy signTransaction capability." in source



def test_direct_cpmm_ata_rent_is_state_aware_and_disclosed_before_approval():
    source = PAGE.read_text(encoding="utf-8")
    assert 'connection.getMinimumBalanceForRentExemption(165,"confirmed")' in source
    assert "validateOutputAtaCpiEvidence(accountPresence,persistentAtaRentLamports" in source
    assert "existing USDC ATA must not incur rent or be recreated/initialized" in source
    assert "new USDC ATA must have exactly one creation/size/immutable-owner/initialization CPI funded with exact live rent" in source
    assert 'id="usdcAccountStatusValue"' in source
    assert 'id="ataRentValue"' in source
    assert 'id="netDebitValue"' in source
    assert "BigInt(prepared.requiredBalanceLamports)" in source
    assert "BigInt(prepared.requiredBalanceLamports)" in source


def test_direct_cpmm_runtime_passes_live_output_ata_presence_to_cpi_verifier():
    source = PAGE.read_text(encoding="utf-8")
    assert "const accountPresence=await verifyPoolAndAccounts(policy,resolvedInfo)" in source
    assert "verifyDirectCpmmRuntime(tx,policy,reviewedBytes,accountPresence)" in source
    assert "outputAtaPresent:ataEvidence.outputAtaPresent" in source
    assert "persistentAtaRentLamports:ataEvidence.persistentAtaRentLamports" in source



def test_builder_minimum_output_error_reflects_configured_slippage():
    source = (Path(__file__).parents[1] / "scripts" / "build-devnet-cpmm-unsigned.mjs").read_text(encoding="utf-8")
    assert 'fail("configured slippage produces a zero minimum output")' in source
    assert "calculated 1% slippage floor is zero" not in source


def test_funding_calculation_covers_temporary_wsol_peak_without_artificial_reserve():
    source = PAGE.read_text(encoding="utf-8")
    assert "temporaryWsolFundingLamports:rentFunding" in source
    assert "const temporaryWsolFundingLamports=BigInt(semanticAudit.temporaryWsolFundingLamports)" in source
    assert "const requiredBalanceLamports=temporaryWsolFundingLamports+persistentAtaRentLamports+BigInt(totalFee)" in source
    assert "await ensureDevnetFunds(lamports + 10000000n)" in source
    assert "fundedBalanceLamports<requiredBalanceLamports" in source
    assert "await ensureDevnetFunds(requiredBalanceLamports)" not in source
    assert "BigInt(prepared.requiredBalanceLamports)" in source
    assert "0.05 SOL reserve" not in source
    assert 'id="temporaryFundingValue"' in source
    assert 'id="requiredBalanceValue"' in source
