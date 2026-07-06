"""CLI entry point.

Stage 1: load config, log the target we WOULD scrape, write nothing.
Later stages plug fetch/parse/output into run().
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config, ConfigError, load_config
from .errors import ScraperBlocked, ScraperError
from .fetch import Fetcher
from .logging_setup import alert, log, setup_logging


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scraper",
        description="Configurable web scraper. All target-specific values live in config.yaml.",
    )
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p.add_argument("--env", default=".env", help="Path to .env file with secrets")
    p.add_argument("--log-file", default="scraper.log", help="Path to the log file")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except write output or deliver alerts.",
    )
    p.add_argument(
        "--url",
        default=None,
        help="Override target_url for a single fetch (debug/testing the fetch layer).",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug-level logging")
    return p


def run(cfg: Config, logger: logging.Logger, dry_run: bool, url_override: str | None = None) -> int:
    """Orchestrate a scrape run. Stages 3-5 flesh this out.

    Stage 2: perform ONE robots-checked, rate-limited fetch of the target.
    A block or exhausted-retry failure propagates and exits the process non-zero.
    """
    target = url_override or cfg.target_url
    log(
        logger,
        logging.INFO,
        "Loaded config; target resolved",
        target_url=target,
        max_pages=cfg.max_pages,
        rate_limit_seconds=cfg.rate_limit_seconds,
        user_agent=cfg.user_agent,
        dedupe_key=cfg.dedupe_key,
        active_fields=sorted(cfg.active_fields.keys()),
        pagination_mode=cfg.pagination.mode,
        csv_path=cfg.output.csv_path,
        google_sheet=bool(cfg.output.google_sheet_id),
        dry_run=dry_run,
    )

    # Note: --dry-run still fetches (it only skips writing/delivering output).
    fetcher = Fetcher(cfg, logger)
    status, html = fetcher.fetch(target)
    log(logger, logging.INFO, "Stage 2 fetch complete", url=target, status=status,
        bytes=len(html))

    # Stage 3 will parse `html` into rows; nothing is written yet.
    log(logger, logging.INFO, "Stage 2 run complete; no output written",
        dry_run=dry_run)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logger = setup_logging(
        log_file=args.log_file,
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    try:
        cfg = load_config(args.config, args.env)
    except ConfigError as exc:
        log(logger, logging.ERROR, "Configuration error", error=str(exc))
        return 2

    try:
        return run(cfg, logger, dry_run=args.dry_run, url_override=args.url)
    except ScraperBlocked as exc:
        # Blocked means STOP. Never retried, never downgraded. Exit non-zero.
        alert(logger, "RUN HALTED: blocked", error=str(exc))
        return 3
    except ScraperError as exc:
        log(logger, logging.ERROR, "RUN FAILED: transient error exhausted retries",
            error=str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level guard, log loudly
        log(logger, logging.ERROR, "Unhandled error during run", error=str(exc))
        logger.exception("traceback")
        return 1


if __name__ == "__main__":
    sys.exit(main())
