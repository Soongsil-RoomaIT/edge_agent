"""실행: python -m edge_agent [--config config/config.toml]"""

from __future__ import annotations

import argparse
import logging
import signal

from .agent import build_agent
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Room care edge agent")
    parser.add_argument("--config", default="config/config.toml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    agent = build_agent(load_config(args.config))
    signal.signal(signal.SIGINT, lambda *_: agent.stop())
    signal.signal(signal.SIGTERM, lambda *_: agent.stop())
    agent.run()


if __name__ == "__main__":
    main()
