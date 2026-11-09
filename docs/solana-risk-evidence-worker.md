# Solana Risk Evidence Persistence and Refresh

The risk evidence store persists each typed report as append-only JSON with a
stable content digest, mint, observation timestamp, evidence-readiness state,
missing claims, and source errors. Prior reports are never overwritten.

The worker:
- queues candidates only after at least one market snapshot exists;
- claims bounded batches with SQLite leases;
- calls the configured claim-separated evidence collector;
- stores partial reports even when individual providers fail, because those
  failures and missing claims are explicit evidence;
- retries collector exceptions with capped exponential backoff;
- refreshes reports on a configurable cadence (default five minutes);
- recovers expired processing leases after interruption.

A stored report is not a safe/unsafe verdict. The risk_state field describes
evidence readiness only. This worker cannot create a BUY signal, approve a
position, or call the paper broker.
