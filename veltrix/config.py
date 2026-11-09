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
    wallet_evidence_max_age_ms: int = int(os.getenv("WALLET_EVIDENCE_MAX_AGE_MS", "300000"))
    safety_evidence_max_age_ms: int = int(os.getenv("SAFETY_EVIDENCE_MAX_AGE_MS", "300000"))
    goplus_access_token: str = os.getenv("GOPLUS_ACCESS_TOKEN", "")
    helius_api_key: str = os.getenv("HELIUS_API_KEY", "")
    alchemy_api_key: str = os.getenv("ALCHEMY_API_KEY", "")
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
            or s.wallet_evidence_max_age_ms <= 0 or s.safety_evidence_max_age_ms <= 0):
        raise ValueError("Invalid risk, discovery or polling configuration")
    return s
