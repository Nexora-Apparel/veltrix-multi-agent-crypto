# Solana launch discovery adapters

## PumpPortal live launch feed

PumpPortalDiscovery connects to the documented PumpPortal data WebSocket and sends only the subscribeNewToken message. It does not subscribe to token/account trade streams, call trade endpoints, or sign transactions. The new-token subscription is documented as free and does not require an API key.

Each emitted candidate is normalized as a Solana mint and tagged with source identity and local receive timestamp. The feed is real-time only, not a historical backfill. Production consumers must persist observations, reconnect with bounded exponential backoff, deduplicate mints and record coverage gaps.

## DexScreener secondary leads and enrichment

DexScreener latest profiles and latest/top boosts can be used as secondary candidate leads, not as a comprehensive organic new-launch feed. Profiles and boosts are promotional signals and must never increase a token safety score by themselves. Use its documented token-pair endpoint to enrich candidates after discovery.

## Fail-closed rules

- A discovery event is not a buy signal.
- Invalid mint addresses and non-create PumpPortal events are discarded.
- Missing metadata is allowed at discovery time but must be fetched and independently verified before risk evaluation.
- Stream disconnects create a coverage gap; they must be recorded, not silently treated as continuous coverage.
- No wallet, signing, swap or live-trading capability exists in this module.