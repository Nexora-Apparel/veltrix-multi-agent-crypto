"""Single bounded pipeline cycle for ephemeral scheduled runners."""
from __future__ import annotations

import logging
import os
import sys

from .pipeline_runtime import build_runtime


def main() -> int:
    logging.basicConfig(
        level=os.getenv("VELTRIX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    runtime = build_runtime()
    result = runtime.run_once()
    status = str(result.job.get("status") or "UNKNOWN")
    logging.info(
        "pipeline cycle completed status=%s job_id=%s executed=%s live_execution=false",
        status, result.job.get("id"), result.job.get("executed"),
    )
    return 0 if status == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
