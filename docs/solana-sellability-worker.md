# Solana Sellability Worker

The sellability worker schedules only candidates that have a latest risk report
containing finalized mint-account evidence and a positive raw token supply. It
uses a bounded default quote size of 1% of supply (constrained to 0.5%–5%),
persists observations append-only, leases jobs, and retries unexpected failures.

The worker stores route observations separately from the risk evidence ledger.
It does not remove executable_sellability from the risk report's missing claims.
A route observation, no-route result, or provider failure remains an intelligence
record only. The worker is not wired into the production runtime until a real
quote adapter has been independently reviewed and its public taker address
requirements are resolved.

Tests use synthetic quote providers. They verify persistence, eligibility,
deduplication and that a route observation cannot clear the existing risk claim.
