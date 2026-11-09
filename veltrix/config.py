from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def _addresses(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())

def _provider_names(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())

def _chains() -> tuple[str, ...]:
    allowed = {"solana", "base", "bsc"}
    return tuple(x.strip().lower() for x in os.getenv("DISCOVERY_CHAINS", "solana,base,bsc").split(",") if x.strip().lower() in allowed)

@dataclass(frozen=True)
class Settings:
    balance: float = float(os.getenv("PAPER_BALANCE_USD", "500"))
    max_position: float = float(os.getenv("MAX_POSITION_USD", "50"))
    max_open: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
    stop_loss: float = float(os.getenv("STOP_LOSS_PCT", "0.06"))
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "60"))
    db_path: str = os.getenv("DB_PATH", "veltrix_paper.db")
    discovery_enabled: bool = os.getenv("DISCOVERY_ENABLED", "false").lower() == "true"
    discovery_chains: tuple[str, ...] = _chains()
    max_discovered_per_chain: int = int(os.getenv("MAX_DISCOVERED_PER_CHAIN", "20"))
    trusted_wallet_providers: tuple[str, ...] = _provider_names("TRUSTED_WALLET_PROVIDERS")
    trusted_safety_providers: tuple[str, ...] = _provider_names("TRUSTED_SAFETY_PROVIDERS")
    trusted_liquidity_providers: tuple[str, ...] = _provider_names("TRUSTED_LIQUIDITY_PROVIDERS")
    trusted_developer_providers: tuple[str, ...] = _provider_names("TRUSTED_DEVELOPER_PROVIDERS")
    trusted_bundle_providers: tuple[str, ...] = _provider_names("TRUSTED_BUNDLE_PROVIDERS")
    trusted_funding_providers: tuple[str, ...] = _provider_names("TRUSTED_FUNDING_PROVIDERS")
    trusted_launch_origin_providers: tuple[str, ...] = _provider_names("TRUSTED_LAUNCH_ORIGIN_PROVIDERS")
    launch_intelligence_enabled: bool = os.getenv("LAUNCH_INTELLIGENCE_ENABLED", "false").lower() == "true"
    wallet_evidence_max_age_ms: int = int(os.getenv("WALLET_EVIDENCE_MAX_AGE_MS", "300000"))
    safety_evidence_max_age_ms: int = int(os.getenv("SAFETY_EVIDENCE_MAX_AGE_MS", "300000"))
    goplus_access_token: str = os.getenv("GOPLUS_ACCESS_TOKEN", "")
    helius_api_key: str = os.getenv("HELIUS_API_KEY", "")
    alchemy_api_key: str = os.getenv("ALCHEMY_API_KEY", "")
    solana_rpc_url: str = os.getenv("SOLANA_RPC_URL", "")
    holder_inventory_max_accounts: int = int(os.getenv("HOLDER_INVENTORY_MAX_ACCOUNTS", "50000"))
    holder_inventory_max_logs: int = int(os.getenv("HOLDER_INVENTORY_MAX_LOGS", "200000"))
    holder_inventory_max_holders: int = int(os.getenv("HOLDER_INVENTORY_MAX_HOLDERS", "50000"))
    base_rpc_url: str = os.getenv("BASE_RPC_URL", "")
    bsc_rpc_url: str = os.getenv("BSC_RPC_URL", "")
    launch_window_ms: int = int(os.getenv("LAUNCH_WINDOW_MS", "120000"))
    launch_index_max_pages: int = int(os.getenv("LAUNCH_INDEX_MAX_PAGES", "20"))
    launch_block_chunk_size: int = int(os.getenv("LAUNCH_BLOCK_CHUNK_SIZE", "2000"))
    funding_lookback_blocks: int = int(os.getenv("FUNDING_LOOKBACK_BLOCKS", "20000"))
    funding_graph_max_pages: int = int(os.getenv("FUNDING_GRAPH_MAX_PAGES", "100"))
    funding_graph_max_wallets: int = int(os.getenv("FUNDING_GRAPH_MAX_WALLETS", "2000"))
    tracked_smart_wallets: tuple[str, ...] = _addresses("TRACKED_SMART_WALLETS")
    tracked_evm_wallets: tuple[str, ...] = _addresses("TRACKED_EVM_WALLETS")
    alchemy_quote_tokens_base: tuple[str, ...] = _addresses("ALCHEMY_QUOTE_TOKENS_BASE")
    alchemy_quote_tokens_bsc: tuple[str, ...] = _addresses("ALCHEMY_QUOTE_TOKENS_BSC")
    solana_tokens: tuple[str, ...] = _addresses("WATCHLIST_SOLANA")
    base_tokens: tuple[str, ...] = _addresses("WATCHLIST_BASE")
    bsc_tokens: tuple[str, ...] = _addresses("WATCHLIST_BSC")

def settings() -> Settings:
    if os.getenv("LIVE_TRADING", "false").lower() == "true":
        raise RuntimeError("Live trading is disabled in this project.")
    s = Settings()
    if (s.balance <= 0 or s.max_position <= 0 or s.max_open < 1 or not 0 < s.stop_loss < 1
            or s.poll_seconds < 15 or s.max_discovered_per_chain < 1
            or s.wallet_evidence_max_age_ms <= 0 or s.safety_evidence_max_age_ms <= 0
            or s.launch_window_ms <= 0 or s.launch_index_max_pages < 1
            or s.launch_index_max_pages > 100
            or s.launch_block_chunk_size < 1 or s.launch_block_chunk_size > 10_000
            or s.funding_lookback_blocks < 1
            or not 1 <= s.holder_inventory_max_accounts <= 100_000
            or s.holder_inventory_max_logs < 1
            or s.holder_inventory_max_holders < 1):
        raise ValueError("Invalid risk, discovery or polling configuration")
    return s
