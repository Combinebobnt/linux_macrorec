"""Sleep deltas to an absolute schedule.

A macro file carries per-step `sleep` lines because that is what reads well, but
replaying by sleeping for each delta accumulates the overhead of every step. So the
deltas are converted once, at load time, into offsets from a single start instant.
A long loop then stays on time no matter how slow any individual injection was.

The speed scalar divides every delay, explicit `sleep` lines included: it scales the
whole macro's tempo rather than only the gaps the recorder inserted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import Event, Sleep

#: Gaps below this are noise from the capture path, not intent.
MIN_SLEEP_MS = 5


@dataclass(frozen=True)
class Step:
    """One action and when to perform it, in seconds after the macro starts."""

    at: float
    event: Event


@dataclass(frozen=True)
class Schedule:
    steps: tuple[Step, ...]
    #: Full length including any trailing sleep, so loop N starts where N-1 ended.
    duration: float

    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self):
        return iter(self.steps)

    def iterate(self, loops: int = 1):
        """Yield steps for `loops` passes, offsets accumulated across iterations.

        `loops` of 0 or less means repeat forever.
        """
        index = 0
        while loops <= 0 or index < loops:
            base = index * self.duration
            for step in self.steps:
                yield Step(base + step.at, step.event)
            index += 1


def build_schedule(events, speed: float = 1.0) -> Schedule:
    if speed <= 0:
        raise ValueError("speed must be greater than zero")

    steps = []
    at = 0.0
    for event in events:
        if isinstance(event, Sleep):
            at += event.ms / 1000.0 / speed
        else:
            steps.append(Step(at, event))
    return Schedule(tuple(steps), at)


def to_events(timed, min_ms: int = MIN_SLEEP_MS) -> list[Event]:
    """The inverse: turn recorded `(seconds, event)` pairs into an event list with
    `Sleep` deltas between them, which is what gets written to a file."""
    out: list[Event] = []
    previous = None
    for at, event in timed:
        if previous is not None:
            gap = round((at - previous) * 1000)
            if gap >= min_ms:
                out.append(Sleep(gap))
        out.append(event)
        previous = at
    return out
