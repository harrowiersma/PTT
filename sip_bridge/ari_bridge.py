# sip_bridge/ari_bridge.py
"""ARI bridge — Phase 2b-audio. Full implementation lands in Task 6+."""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("ari-bridge")


def main() -> None:
    LOG.info("ari-bridge stub — Task 6 implementation pending")
    # Sit idle so supervisord doesn't flap.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
