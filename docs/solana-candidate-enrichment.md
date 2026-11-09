# Candidate enrichment worker

`CandidateEnrichmentWorker` joins the launch candidate table to the existing
Solana market snapshot adapter and append-only market evidence store.

## Processing contract

1. Import unseen mint candidates into `solana_enrichment_jobs`.
2. Claim a bounded batch under a SQLite write transaction.
3. Lease each job so a crashed worker can be recovered after lease expiry.
4. Fetch a fresh, validated Solana market snapshot.
5. Persist the observation, then mark the job completed.
6. On errors, retain a bounded error summary and schedule exponential retry.
7. Move jobs to `dead` after the configured maximum attempts.

The launch and market stores must use the same persistent SQLite database file.
The worker refuses in-memory or split databases because they would break the
single transaction/persistence boundary.

## Operating model

Call `run_once(limit=25)` from a supervised worker loop or scheduled worker
process. The method is bounded and idempotent: completed jobs are not fetched
again. A crashed in-flight job is eligible for retry when its lease expires.

`status()` returns pending, processing, retry, completed and dead counts.
Dead jobs require an operator review or an explicit administrative retry path;
they are not silently requeued forever.

## Safety boundary

The worker performs market-data enrichment only. It does not infer safety,
assign a buy score, invoke agents, create paper positions, connect to wallets,
or sign/submit transactions. Fresh market data is necessary but insufficient
for a token-risk clearance.
