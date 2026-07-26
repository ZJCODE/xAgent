"""Private child-process entrypoint for the RuntimeLauncher."""
from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from .launcher import RuntimeLauncher


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xagent-runtime")
    parser.add_argument("--config-dir", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(RuntimeLauncher(args.config_dir).run_foreground())
    except KeyboardInterrupt:
        return 130
    except Exception:
        logging.getLogger(__name__).exception("Runtime terminated")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
