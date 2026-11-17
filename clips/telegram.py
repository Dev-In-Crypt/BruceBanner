"""Operator messages. Phase 4 replaces send() with the real Telegram Bot API."""
import logging

log = logging.getLogger(__name__)


def send(text, buttons=None, photo=None, dry=False):
    # ponytail: log-only until Task 4.3 wires the Telegram Bot API
    log.warning("ALERT%s: %s%s", " (dry-run)" if dry else "", text, f" [photo: {photo}]" if photo else "")
