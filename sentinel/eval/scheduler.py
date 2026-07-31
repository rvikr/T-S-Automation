"""Background scheduler that runs the golden-set eval harness nightly.

A single daemon thread wakes at ``EVAL_SCHEDULE_HOUR`` UTC each night, runs
``run_golden_set(live=True)``, computes metrics, and writes the run report to
``EVAL_RUNS_DIR``.  If the resulting accuracy falls below ``EVAL_ACCURACY_FLOOR``
a WARNING is logged so operators are alerted without halting the service.

The scheduler is started once at app startup via :func:`start_eval_scheduler`.
A module-level :class:`threading.Event` prevents double-starts (e.g. from
Streamlit's file-watcher reloader firing twice in rapid succession).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from sentinel.config import EVAL_ACCURACY_FLOOR, EVAL_RUNS_DIR, EVAL_SCHEDULE_HOUR
from sentinel.eval.run_eval import compute_metrics, run_golden_set, write_report

logger = logging.getLogger(__name__)

# Set when the background thread has been started; guards against double-start.
_started = threading.Event()


def _seconds_until_next_scheduled_hour() -> float:
    """Return the number of seconds until the next occurrence of EVAL_SCHEDULE_HOUR UTC."""
    now = datetime.now(UTC)
    today_target = now.replace(hour=EVAL_SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
    if now >= today_target:
        # Target already passed today — wait until tomorrow.
        delta = today_target.timestamp() + 86400 - now.timestamp()
    else:
        delta = today_target.timestamp() - now.timestamp()
    return max(0.0, delta)


def _eval_loop() -> None:
    """Run the golden-set eval on each scheduled UTC hour tick, indefinitely."""
    while True:
        wait = _seconds_until_next_scheduled_hour()
        logger.info(
            "Eval scheduler: next run in %.0f s (at %02d:00 UTC)",
            wait,
            EVAL_SCHEDULE_HOUR,
        )
        time.sleep(wait)

        logger.info("Eval scheduler: starting nightly golden-set run (live=True)")
        try:
            scores = run_golden_set(live=True)
            metrics = compute_metrics(scores)
            run_dir = write_report(scores, metrics, EVAL_RUNS_DIR, mode="live")
            accuracy = metrics.get("accuracy")
            logger.info(
                "Eval scheduler: run complete — accuracy=%.1f%% run_dir=%s",
                (accuracy or 0) * 100,
                run_dir,
            )
            if accuracy is not None and accuracy < EVAL_ACCURACY_FLOOR:
                logger.warning(
                    "Eval accuracy %.1f%% is below the floor of %.1f%% — review %s",
                    accuracy * 100,
                    EVAL_ACCURACY_FLOOR * 100,
                    run_dir,
                )
        except Exception:
            logger.exception("Eval scheduler: golden-set run raised an unexpected exception")


def start_eval_scheduler() -> None:
    """Start the background eval scheduler thread (idempotent).

    Subsequent calls after the first are silently ignored so Streamlit's
    file-watcher reloader and any other multi-call path cannot accumulate
    threads.
    """
    if _started.is_set():
        return
    _started.set()
    thread = threading.Thread(target=_eval_loop, daemon=True, name="sentinel-eval-scheduler")
    thread.start()
    logger.info("Eval scheduler started (thread id=%d)", thread.ident or 0)
