"""The abstract Recorder/Player interface.

This seam exists so an evdev/uinput backend can be added later, for Wayland or for a
machine where `input` group membership has been granted, without the parser, the
timeline or the GUI knowing about it.

Subclasses implement the six primitives. `perform()` is shared: dispatching an Event
to primitives is the same work whatever is on the other end.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from ..events import (
    Click,
    Event,
    KeyDown,
    KeyTap,
    KeyUp,
    MouseDown,
    MouseUp,
    Move,
    Scroll,
    Sleep,
    TypeText,
    expand_type,
)

#: Called with (seconds since recording started, event).
EventSink = Callable[[float, Event], None]


class Recorder(ABC):
    """Captures real input. Recording and playback are mutually exclusive: an active
    recorder would otherwise capture the player's own injected events."""

    @abstractmethod
    def start(self, sink: EventSink) -> None:
        """Begin capturing, calling `sink` for each event. Returns immediately."""

    @abstractmethod
    def stop(self) -> None:
        """Stop capturing. Safe to call when not recording."""

    @property
    @abstractmethod
    def is_recording(self) -> bool:
        ...


class Player(ABC):
    """Injects input."""

    def perform(self, event: Event) -> None:
        """Carry out one event. Sleep is not handled here: waiting is the caller's
        job, against the timeline, so delays do not accumulate drift."""
        if isinstance(event, KeyTap):
            self.key_down(event.sym)
            self.key_up(event.sym)
        elif isinstance(event, KeyDown):
            self.key_down(event.sym)
        elif isinstance(event, KeyUp):
            self.key_up(event.sym)
        elif isinstance(event, TypeText):
            for tap in expand_type(event.text):
                self.key_down(tap.sym)
                self.key_up(tap.sym)
        elif isinstance(event, Move):
            self.move(event.x, event.y)
        elif isinstance(event, Click):
            self.button_down(event.button)
            self.button_up(event.button)
        elif isinstance(event, MouseDown):
            self.button_down(event.button)
        elif isinstance(event, MouseUp):
            self.button_up(event.button)
        elif isinstance(event, Scroll):
            for _ in range(event.count):
                self.scroll(event.direction)
        elif isinstance(event, Sleep):
            raise TypeError("Sleep is scheduled by the timeline, not performed")
        else:
            raise TypeError(f"no player support for {type(event).__name__}")

    @abstractmethod
    def key_down(self, sym: str) -> None:
        ...

    @abstractmethod
    def key_up(self, sym: str) -> None:
        ...

    @abstractmethod
    def move(self, x: int, y: int) -> None:
        ...

    @abstractmethod
    def button_down(self, button: str) -> None:
        ...

    @abstractmethod
    def button_up(self, button: str) -> None:
        ...

    @abstractmethod
    def scroll(self, direction: str) -> None:
        ...

    def close(self) -> None:
        """Release any connection. Default is a no-op."""
