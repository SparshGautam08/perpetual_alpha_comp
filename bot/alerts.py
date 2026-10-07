"""Phone alerts via ntfy (https://ntfy.sh): install the ntfy app, subscribe
to the topic in NTFY_TOPIC. Anyone who knows the topic name can read it,
so use a long random name and keep it out of git."""
import logging
import time

import requests

from . import config

log = logging.getLogger("bot")
_last_sent: dict[str, float] = {}


def alert(title: str, message: str, priority: str = "default", key: str | None = None,
          every_s: int = 600) -> None:
    """Send an alert. Repeats of the same `key` are dropped for `every_s`
    seconds so a stuck error doesn't flood phones. priority: min, low,
    default, high, urgent."""
    log.warning(f"ALERT [{priority}] {title}: {message}")
    k = key or title
    if time.time() - _last_sent.get(k, 0) < every_s:
        return
    _last_sent[k] = time.time()
    if not config.NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{config.NTFY_TOPIC}", data=message.encode(),
                      headers={"Title": f"par bot: {title}", "Priority": priority},
                      timeout=10)
    except Exception as e:   # an alert failure must never crash the bot
        log.error(f"Could not send alert: {e}")
