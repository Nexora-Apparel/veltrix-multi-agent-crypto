# Supervised Solana launch stream

## LaunchStreamManager

The manager supervises the PumpPortal new-token adapter and persists normalized
mint observations in SQLite. It reconnects after exceptions or unexpected
stream termination using exponential backoff bounded by the configured maximum.
The delay resets after a candidate is observed.

## Persistence

- `solana_launch_candidates`: one row per mint, first/latest observation times,
  primary source and merged source identities.
- `launch_feed_events`: append-only connection attempts, candidate observations
  and outage events.
- `launch_feed_outages`: outage start and recovery timestamps. An outage remains
  active until a subsequent candidate is observed.

The SQLite database path must be on persistent storage in deployment. An
ephemeral filesystem will lose history on restart.

## Coverage interpretation

This records observed interruptions, not proof of uninterrupted provider
delivery. A quiet market and a disconnected socket can look identical until a
socket error or a later event is observed. The manager therefore never labels
feed coverage as complete or gap-free. Operational monitoring should alert on
long periods without events and independently verify socket health.

## Runtime boundary

Run the manager in a dedicated worker process, not inside a short-lived web
request. Supply a process shutdown event and a persistent SQLite path. The
manager only discovers and stores candidates; it does not enrich, score, buy,
sell, sign, or submit transactions.
