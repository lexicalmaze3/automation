"""Custom exceptions for the scraper.

Two distinct failure classes, deliberately kept separate:

- ScraperBlocked : the site refused us or told us to stop (robots disallow,
  403/429, or a block/CAPTCHA page). This must HALT the run. It is never
  retried, never swallowed, never downgraded to a warning-and-continue.
  By design we do not attempt to evade blocks.

- ScraperError   : a transient failure survived all retries (connection error,
  read timeout, 5xx). The run stops, but this is "the site broke", not "the
  site blocked us".
"""

from __future__ import annotations


class ScraperError(Exception):
    """A request failed after exhausting retries. Not a block."""


class ScraperBlocked(Exception):
    """We were blocked or disallowed. Halt immediately; do not evade."""
