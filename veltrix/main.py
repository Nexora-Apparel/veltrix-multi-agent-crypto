import argparse, time
from .config import settings
from .core import Core

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=24)
    args = p.parse_args()
    cfg = settings()
    if args.hours <= 0: raise SystemExit("--hours must be positive")
    Core(cfg.max_open, cfg.max_position)
    print(f"VELTRIX PAPER SESSION armed: balance={cfg.balance:.2f} USD; duration={args.hours:g}h")
    print("No provider configured: waiting safely; no synthetic trades will be generated.")
    deadline = time.monotonic() + args.hours * 3600
    try:
        while time.monotonic() < deadline: time.sleep(min(30, max(0, deadline-time.monotonic())))
    except KeyboardInterrupt: print("Session interrupted; performance is not certified.")
if __name__ == "__main__": main()
