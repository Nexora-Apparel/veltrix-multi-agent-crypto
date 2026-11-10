# Turso database adapter — H1 prototype

## Scope

The default backend remains local SQLite. The remote backend is selected only
when `VELTRIX_SOLANA_DISCOVERY_DB` (or `DB_PATH`) is explicitly set to a
secure `libsql://*.turso.io` URL and `TURSO_AUTH_TOKEN` is present.

Install the optional, source-pinned driver with:

```sh
python -m pip install -r requirements-turso.txt
```

Example environment:

```sh
VELTRIX_SOLANA_DISCOVERY_DB=libsql://<database>.turso.io
TURSO_AUTH_TOKEN=<database-scoped-token>
```

Never commit the token or print it in logs. Use a dedicated disposable database
for conformance and integration testing. The remote adapter deliberately does
not accept plain HTTP, arbitrary hosts, SQLite URI mode, or implicit fallback.

## Converted persistence components

- Solana launch candidate / feed store
- Solana market snapshot store
- Solana risk evidence store
- Solana consensus decision store
- PaperBroker ledger used by Phase 07

The local SQLite code path remains the default. WAL and local-only PRAGMAs are
not sent to Turso. The remote adapter uses the pinned DB-API driver's row
factory and connection implementation.

## Current limitations

This is an adapter integration prototype, not production migration approval.
The H0 live probe established a narrow DB-API contract. H1 must still verify
actual application schema initialization, SQLite-specific SQL and transaction
behaviour against the disposable database, then run the full local test suite.

The dashboard server still reads a local SQLite file and is not converted to
Turso. The Cloudflare Worker read-only dashboard API is a later phase.
Other SQLite stores outside the listed Solana/paper pipeline components are
not implicitly converted.

Do not point the remote URL at the production ledger yet. No existing SQLite
database has been uploaded, copied, overwritten, or migrated. Live trading,
signing and transaction submission remain disabled.
