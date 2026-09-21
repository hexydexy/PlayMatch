"""Tiny daily scheduler (one process, no cron dependency).

Runs the data job once a day at SCHEDULE_HOUR_UTC. On Kubernetes this is
replaced by a CronJob running `python -m app.snapshot`.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from . import config, snapshot

log = logging.getLogger("playmatch.scheduler")


def next_run(now: datetime) -> datetime:
    t = now.replace(hour=config.SCHEDULE_HOUR_UTC, minute=0, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        when = next_run(datetime.now(timezone.utc))
        log.info("next run at %s", when.isoformat())
        time.sleep(max(1, (when - datetime.now(timezone.utc)).total_seconds()))
        try:
            snapshot.run()
        except Exception:
            log.exception("daily job failed")  # keep the loop alive


if __name__ == "__main__":
    main()
