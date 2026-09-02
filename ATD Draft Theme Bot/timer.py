# timer.py
# Generic per-drafter countdown with skip-based decay, matching the rule
# from Pack Draft: each timeout knocks the drafter's next timer down by a
# configurable amount, to a configurable floor. Themes decide when to
# start/cancel a clock and what happens on timeout (usually an auto-pick);
# this module only owns the asyncio bookkeeping.

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class PickTimer:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._deadlines: dict[int, float] = {}  # drafter_id -> time.monotonic() when it fires

    @staticmethod
    def effective_minutes(base_minutes: float, decay_minutes: float, floor_minutes: float,
                           skip_count: int) -> float:
        return max(floor_minutes, base_minutes - decay_minutes * skip_count)

    def is_running(self, drafter_id: int) -> bool:
        t = self._tasks.get(drafter_id)
        return t is not None and not t.done()

    def remaining_seconds(self, drafter_id: int) -> float | None:
        """Seconds left before this drafter's clock fires, or None if no timer is running for them."""
        if not self.is_running(drafter_id):
            return None
        deadline = self._deadlines.get(drafter_id)
        return max(0.0, deadline - time.monotonic()) if deadline is not None else None

    def cancel(self, drafter_id: int):
        t = self._tasks.pop(drafter_id, None)
        self._deadlines.pop(drafter_id, None)
        if t and not t.done():
            t.cancel()
            log.info("PickTimer | cancelled key=%s", drafter_id)

    def start(self, drafter_id: int, minutes: float, on_timeout):
        """on_timeout: async callable, no args — invoked if the clock isn't
        cancelled first (i.e. the drafter didn't act in time)."""
        self.cancel(drafter_id)
        self._deadlines[drafter_id] = time.monotonic() + minutes * 60
        log.info("PickTimer | started key=%s minutes=%.2f", drafter_id, minutes)

        async def _run():
            try:
                await asyncio.sleep(minutes * 60)
            except asyncio.CancelledError:
                log.info("PickTimer | task cancelled mid-sleep key=%s", drafter_id)
                return
            self._tasks.pop(drafter_id, None)
            self._deadlines.pop(drafter_id, None)
            # This is the line that answers "did it actually fire" — if the
            # deadline passed and this never shows up in the logs, the task
            # itself never ran (as opposed to running and failing, which
            # would show the exception below instead).
            log.info("PickTimer | firing on_timeout key=%s", drafter_id)
            try:
                await on_timeout()
            except Exception:
                log.exception("PickTimer | on_timeout failed for drafter_id=%s", drafter_id)
            else:
                log.info("PickTimer | on_timeout completed key=%s", drafter_id)

        self._tasks[drafter_id] = asyncio.create_task(_run())
