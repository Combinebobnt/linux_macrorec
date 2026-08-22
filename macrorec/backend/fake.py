"""In-memory backend for logic tests. Needs no display.

`FakePlayer` writes down the primitives it was asked to perform, so a test can assert
on what a real backend would have injected. `FakeRecorder` replays a canned event
list, standing in for a human at the keyboard.
"""

from __future__ import annotations

import threading

from .base import EventSink, Player, Recorder


class FakePlayer(Player):
    """Records calls instead of injecting them. `calls` holds tuples like
    ("key_down", "a") or ("move", 10, 20).

    `skip_syms` is honoured exactly as `X11Player` honours it. A fake that ignored
    it would let a test pass while the real player suppressed the keystroke, which
    is the wrong direction for a stand-in to be wrong in.
    """

    def __init__(self, skip_syms=()):
        self.calls: list[tuple] = []
        self.skip_syms = set(skip_syms)
        self.skipped: list[str] = []
        self.closed = False

    def key_down(self, sym: str) -> None:
        if sym in self.skip_syms:
            self.skipped.append(sym)
            return
        self.calls.append(("key_down", sym))

    def key_up(self, sym: str) -> None:
        if sym in self.skip_syms:
            self.skipped.append(sym)
            return
        self.calls.append(("key_up", sym))

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))

    def button_down(self, button: str) -> None:
        self.calls.append(("button_down", button))

    def button_up(self, button: str) -> None:
        self.calls.append(("button_up", button))

    def scroll(self, direction: str) -> None:
        self.calls.append(("scroll", direction))

    def close(self) -> None:
        self.closed = True


class FakeRecorder(Recorder):
    """Emits a canned `(timestamp, event)` list when started.

    Delivery happens on a worker thread, matching the X backend: there,
    `record_enable_context()` blocks, so events can only arrive after `start()` has
    returned. A fake that delivered synchronously would teach tests a contract the
    real recorder cannot honour. Call `drain()` to wait for the canned list.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self._recording = False
        self._thread: threading.Thread | None = None
        self._delivered = threading.Event()
        self.start_count = 0

    def start(self, sink: EventSink) -> None:
        if self._recording:
            raise RuntimeError("already recording")
        self._recording = True
        self.start_count += 1
        self._delivered.clear()
        self._thread = threading.Thread(
            target=self._deliver, args=(sink,), daemon=True)
        self._thread.start()

    def _deliver(self, sink: EventSink) -> None:
        for at, event in self.script:
            if not self._recording:
                break
            sink(at, event)
        self._delivered.set()

    def drain(self, timeout: float = 2.0) -> bool:
        """Wait for the canned script to finish arriving. Test-only convenience."""
        return self._delivered.wait(timeout)

    def stop(self) -> None:
        self._recording = False
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None

    @property
    def is_recording(self) -> bool:
        return self._recording
