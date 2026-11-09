# Sell simulation diagnostics

The diagnostic classifier maps simulation logs into conservative failure
domains: RPC/blockhash context, test-wallet balance/fees, missing token-account
setup, transfer-restriction signals, unclassified program errors, or unknown
failures.

It deliberately does not emit a honeypot verdict. A transfer-hook or
non-transferable signal requires independent inspection of mint extensions,
program identity and logs. An insufficient balance or missing associated
token account is a test-wallet setup issue, not proof of token-level restriction.
All original simulation logs remain the primary evidence.
