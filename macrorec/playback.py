"""Running a schedule against a player, on a worker thread.

Two things make this a module rather than a loop inside the GUI:

- **It must not run on the GUI thread.** There, Stop and Loop are both unreachable
  and the window freezes for the length of the macro.
- **It waits until an absolute instant, never for a delta.** `timeline` produces
  offsets from a single origin; sleeping each gap in turn would add the cost of every
  injection to every later step, and a long loop would drift steadily late.
"""

from __future__ import annotations

import threading
import time


class Playback:
    """One run of a schedule. Not reusable: make a new one per playback."""

    def __init__(self, player, schedule, loops: int = 1,
                 on_step=None, on_finish=None):
        self.player = player
        self.schedule = schedule
        self.loops = loops
        self.on_step = on_step
        self.on_finish = on_finish
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("playback already started")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        error = None
        count = len(self.schedule)
        try:
            if count:
                origin = time.monotonic()
                for index, step in enumerate(self.schedule.iterate(self.loops)):
                    # wait() returns as soon as Stop is pressed, so a macro with a
                    # long sleep in it still stops instantly.
                    if self._stop.wait(max(0.0, origin + step.at - time.monotonic())):
                        break
                    self.player.perform(step.event)
                    if self.on_step is not None:
                        loop_index, step_index = divmod(index, count)
                        self.on_step(loop_index, step_index, step)
        except Exception as exc:  # surfaced to the caller, never swallowed
            error = exc
        finally:
            self._finished.set()
            if self.on_finish is not None:
                self.on_finish(self._stop.is_set(), error)

    def stop(self) -> None:
        """Ask playback to stop. Safe from any thread, including a panic grab."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the run ends. Returns False if it timed out first."""
        return self._finished.wait(timeout)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def was_stopped(self) -> bool:
        return self._stop.is_set()
