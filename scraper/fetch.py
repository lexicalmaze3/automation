"""Fetch layer: robots-aware, rate-limited, retry-with-backoff HTTP client.

Public surface: `Fetcher(cfg, logger).fetch(url) -> (status_code, html)`.

Design rules (see errors.py for the two failure classes):
- robots.txt is fetched ONCE per run and cached; every URL is checked against
  it before we make a request. Disallowed -> ScraperBlocked, no request.
- Rate limit: sleep rate_limit_seconds + random jitter BEFORE each request.
- Transient failures (connection error, read timeout, 5xx) -> exponential
  backoff, then ScraperError.
- Hard blocks (403/429, or block/CAPTCHA markers in a 200 body) -> ScraperBlocked
  immediately, no retry.

We never rotate identity, cycle proxies, or solve CAPTCHAs. Blocked means stop.
"""

from __future__ import annotations

import logging
import random
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

# Trust the operating system's certificate store when available. This lets the
# scraper work behind corporate TLS-inspecting proxies (whose root CA lives in
# the OS store, not in certifi) WITHOUT ever disabling verification. This is
# proper trust configuration, not block evasion.
try:
    import truststore

    truststore.inject_into_ssl()
    _TRUSTSTORE = True
except Exception:  # pragma: no cover - truststore is optional
    _TRUSTSTORE = False

from .config import Config
from .errors import ScraperBlocked, ScraperError
from .logging_setup import alert, log

# Substrings that indicate a block/challenge page even when served with 200.
# Matched case-insensitively against the response body. Conservative by design:
# when in doubt we halt rather than risk hammering a site that is fending us off.
BLOCK_MARKERS = (
    "recaptcha",
    "g-recaptcha",
    "hcaptcha",
    "cf-challenge",
    "cf-chl",
    "just a moment...",              # Cloudflare interstitial <title>
    "attention required! | cloudflare",
    "checking your browser before",
    "access denied",
    "too many requests",
    "unusual traffic",
    "verify you are a human",
)


class Fetcher:
    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "he,en;q=0.8",
            }
        )
        self._timeout = (cfg.request_timeout_seconds, cfg.request_timeout_seconds)
        # robots.txt cache, keyed by scheme+host. Fetched once, reused all run.
        self._robots: dict[str, RobotFileParser] = {}

    # ---- robots.txt -------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        robots_url = urljoin(origin, "/robots.txt")
        rp = RobotFileParser()
        try:
            resp = self.session.get(robots_url, timeout=self._timeout)
            if resp.status_code >= 400:
                # No usable robots.txt (404 etc.) -> standard behaviour is
                # "allow all". An empty parser allows everything.
                rp.parse([])
                log(self.logger, logging.INFO, "robots.txt not available; assuming allow-all",
                    robots_url=robots_url, status=resp.status_code)
            else:
                rp.parse(resp.text.splitlines())
                log(self.logger, logging.INFO, "robots.txt fetched and parsed",
                    robots_url=robots_url, status=resp.status_code,
                    bytes=len(resp.text))
        except requests.RequestException as exc:
            # Could not reach robots.txt. Fail open (allow) but say so loudly.
            rp.parse([])
            log(self.logger, logging.WARNING,
                "robots.txt unreachable; assuming allow-all", robots_url=robots_url,
                error=str(exc))
        self._robots[origin] = rp
        return rp

    def _check_robots(self, url: str) -> None:
        rp = self._robots_for(url)
        allowed = rp.can_fetch(self.cfg.user_agent, url)
        log(self.logger, logging.INFO, "robots.txt checked",
            url=url, user_agent=self.cfg.user_agent, allow=allowed)
        if not allowed:
            alert(self.logger, "robots.txt DISALLOWS this URL; halting (not scraping)",
                  url=url, user_agent=self.cfg.user_agent)
            raise ScraperBlocked(f"robots.txt disallows {url}")

    # ---- rate limiting ----------------------------------------------------

    def _sleep_before_request(self) -> None:
        jitter = random.uniform(self.cfg.jitter_min_seconds, self.cfg.jitter_max_seconds)
        delay = self.cfg.rate_limit_seconds + jitter
        log(self.logger, logging.DEBUG, "rate-limit sleep before request",
            base=self.cfg.rate_limit_seconds, jitter=round(jitter, 3),
            total=round(delay, 3))
        time.sleep(delay)

    # ---- block detection --------------------------------------------------

    def _detect_block_page(self, url: str, body: str) -> None:
        low = body.lower()
        for marker in BLOCK_MARKERS:
            if marker in low:
                alert(self.logger, "block/CAPTCHA marker in 200 body; halting",
                      url=url, marker=marker)
                raise ScraperBlocked(f"block page detected at {url} (marker: {marker!r})")

    # ---- fetch ------------------------------------------------------------

    def fetch(self, url: str) -> tuple[int, str]:
        """Fetch one URL. Returns (status_code, html) or raises.

        Raises ScraperBlocked on 403/429/robots-disallow/block-page (halt, no
        retry). Raises ScraperError when transient failures survive all retries.
        """
        # robots check happens BEFORE any request to the target URL.
        self._check_robots(url)

        # Rate-limit sleep happens once, before the first attempt. Retries use
        # their own exponential backoff on top.
        self._sleep_before_request()

        attempts = self.cfg.max_retries + 1  # initial try + N retries
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                log(self.logger, logging.INFO, "HTTP request",
                    url=url, attempt=attempt + 1, of=attempts)
                resp = self.session.get(url, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                if attempt < self.cfg.max_retries:
                    backoff = 2 ** attempt  # 1, 2, 4, ...
                    log(self.logger, logging.WARNING,
                        "transient network error; backing off",
                        url=url, error=str(exc), attempt=attempt + 1,
                        backoff_seconds=backoff)
                    time.sleep(backoff)
                    continue
                break  # out of retries -> raise ScraperError below

            status = resp.status_code

            # --- hard blocks: no retry, halt immediately ---
            if status in (403, 429):
                alert(self.logger, "HARD BLOCK received; halting (no retry, no evasion)",
                      url=url, status=status)
                raise ScraperBlocked(f"HTTP {status} block at {url}")

            # --- transient server errors: retry with backoff ---
            if status >= 500:
                last_error = ScraperError(f"HTTP {status} at {url}")
                if attempt < self.cfg.max_retries:
                    backoff = 2 ** attempt  # 1, 2, 4
                    log(self.logger, logging.WARNING,
                        "server error (5xx); backing off",
                        url=url, status=status, attempt=attempt + 1,
                        backoff_seconds=backoff)
                    time.sleep(backoff)
                    continue
                break  # out of retries -> raise ScraperError below

            # --- 2xx/3xx/other 4xx: inspect body for stealth block pages ---
            if 200 <= status < 300:
                self._detect_block_page(url, resp.text)

            log(self.logger, logging.INFO, "fetch ok", url=url, status=status,
                bytes=len(resp.text))
            return status, resp.text

        # Retries exhausted on a transient failure.
        alert(self.logger, "transient failure survived all retries; giving up",
              url=url, retries=self.cfg.max_retries, error=str(last_error))
        raise ScraperError(
            f"gave up on {url} after {self.cfg.max_retries} retries: {last_error}"
        ) from last_error
