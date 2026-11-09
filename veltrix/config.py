from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def _addresses(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())

@dataclass(frozen=True)
class Settings:
    balance: float = float(os.getenv("PAPER_BALANCE_USD", "500"))
    max_position: float = float(os.getenv("MAX_POSITION_USD", "50"))
    max_open: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
    stop_loss: float = float(os.getenv("STOP_LOSS_PCT", "0.06"))
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "60"))
    db_path: str = os.getenv("DB_PATH", "veltrix_paper.db")
    solana_tokens: tuple[str, ...] = _addresses("WATCHLIST_SOLANA")
    base_tokens: tuple[str, ...] = _addresses("WATCHLIST_BASE")
    bsc_tokens: tuple[str, ...] = _addresses("WATCHLIST_BSC")

def settings() -> Settings:
    if os.getenv("LIVE_TRADING", "false").lower() == "true":
        raise RuntimeError("Live trading is disabled in this project.")
    s = Settings()
    if s.balance <= 0 or s.max_position <= 0 or s.max_open < 1 or not 0 < s.stop_loss < 1 or s.poll_seconds < 15:
        raise ValueError("Invalid risk or polling configuration (minimum poll interval: 15 seconds)")
    return s
