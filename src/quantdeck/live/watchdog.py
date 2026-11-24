"""A process that has died can't report it. The watchdog runs separately and
raises the alarm when a run's heartbeat goes quiet."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ..notify import Notifier

log = logging.getLogger(__name__)


def check_runs(runs_dir: Path, stale_after: float, alerted: set[str], now: datetime | None = None) -> list[tuple]:
    """Return (event, run name, message) for runs that went quiet or came back."""
    now = now or datetime.now(timezone.utc)
    out = []
    if not runs_dir.exists():
        return out
    for f in sorted(runs_dir.glob("*/status.json")):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = s.get("name") or f.parent.name
        if s.get("state") == "stopped":
            alerted.discard(name)
            continue
        try:
            beat = datetime.fromisoformat(s["heartbeat"])
        except (KeyError, ValueError):
            continue
        age = (now - beat).total_seconds()
        if age > stale_after and name not in alerted:
            alerted.add(name)
            out.append(("stale", name, f"no heartbeat for {age / 60:.0f} min (last state: {s.get('state')})"))
        elif age <= stale_after and name in alerted:
            alerted.discard(name)
            out.append(("recovered", name, "heartbeat is back"))
    return out


def watch(runs_dir: str | Path, notifier: Notifier, stale_after: float = 600, interval: float = 60,
          max_checks: int | None = None) -> None:
    runs_dir = Path(runs_dir)
    alerted: set[str] = set()
    checks = 0
    log.info("watching %s (stale after %ds)", runs_dir, stale_after)
    while True:
        for event, name, msg in check_runs(runs_dir, stale_after, alerted):
            log.warning("%s: %s", name, msg)
            # "recovered" rides on the stale channel so it isn't filtered out
            notifier.notify("stale", f"[{name}] {'run looks dead' if event == 'stale' else 'run recovered'}", msg,
                            key=f"{event}:{name}")
        checks += 1
        if max_checks is not None and checks >= max_checks:
            break
        time.sleep(interval)
