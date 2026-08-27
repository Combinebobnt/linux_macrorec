"""Motion reduction at record time.

Recording raw pointer motion produces thousands of lines for a few seconds of
mousing, which defeats the point of a hand-editable file. Only the position where
something actually happens is worth keeping, so a run of moves collapses to its last
one, and that survivor is kept only if a mouse action follows it.

That is the default. `sample_motion` is the opt-in alternative, for freehand and drag
paths where the route taken is the point; the GUI picks between the two. `merge_sleeps`
runs last whichever was chosen.
"""

from __future__ import annotations

from .events import MOUSE_EVENTS, Event, Move, MoveRel, Sleep

#: One retained pointer sample per ~16ms, roughly 60 a second. Chosen well above
#: timeline's MIN_SLEEP_MS of 5, so every gap this leaves becomes a `sleep` line of its
#: own rather than being folded into the next one. Sub-floor gaps are carried forward,
#: not lost, so this no longer decides whether the path keeps its total duration - only
#: how finely that duration is written down.
MOTION_SAMPLE_SECONDS = 0.016


def collapse_motion(events) -> list[Event]:
    """Drop intermediate and pointless pointer moves, preserving timing.

    Sleeps are transparent: none is ever dropped, and they do not break up a run of
    moves or hide the mouse action that justifies keeping one. They are relocated
    around the surviving move, though, and which side they land on matters.

    Time spent *travelling* is emitted before the move, and time spent resting on the
    final position after it. Both halves are real: a user drags the pointer over, then
    pauses, then clicks. Putting all of it after the move would dwell on the target for
    the whole approach, firing hover state, tooltips and hover menus the recording never
    triggered; putting all of it before would give the target no hover at all and take a
    drag's settle time away from the application. Splitting reproduces the original
    timing exactly rather than redistributing it.
    """
    events = list(events)
    out: list[Event] = []
    pending_move: Move | None = None
    travel_sleeps: list[Sleep] = []
    post_move_sleeps: list[Sleep] = []

    for event in events:
        if isinstance(event, Move):
            # The previous move's dwell was travel after all: something moved again.
            travel_sleeps.extend(post_move_sleeps)
            post_move_sleeps = []
            pending_move = event
            continue

        if isinstance(event, Sleep):
            if pending_move is None:
                out.append(event)
            else:
                post_move_sleeps.append(event)
            continue

        if pending_move is not None:
            out.extend(travel_sleeps)
            if isinstance(event, MOUSE_EVENTS):
                out.append(pending_move)
            out.extend(post_move_sleeps)
            pending_move = None
            travel_sleeps = []
            post_move_sleeps = []
        out.append(event)

    # A trailing run of moves led to nothing, but the time it took still passed.
    out.extend(travel_sleeps)
    out.extend(post_move_sleeps)
    return out


def merge_sleeps(events) -> list[Event]:
    """Fold every run of adjacent `Sleep`s into one, summing their milliseconds.

    Recovering a fast stroke's elapsed time (see `timeline.to_events`) turns it into a
    long run of `sleep 5` lines, which defeats the point of a hand-editable file. One
    `sleep 812` says the same thing.

    A single idempotent sweep over the finished list, rather than merging inside
    `collapse_motion`'s buffers: a `Sleep` emitted directly there, with no move pending,
    can sit next to one flushed out of a buffer, and in-place merging would miss that
    pair. Record time only - two consecutive `sleep` lines in a hand-written file have
    to round-trip, so `script.parse` must never do this.

    Total duration is unchanged, so this is presentation, not timing.
    """
    out: list[Event] = []
    total = 0
    for event in events:
        if isinstance(event, Sleep):
            total += event.ms
            continue
        if total:
            out.append(Sleep(total))
            total = 0
        out.append(event)
    if total:
        out.append(Sleep(total))
    return out


def sample_motion(timed, interval: float = MOTION_SAMPLE_SECONDS):
    """Thin pointer motion to one sample per `interval`, keeping the path.

    Takes and returns the recorder's `(seconds, event)` pairs, so it runs *before*
    `timeline.to_events`. Thinning afterwards would drop the `Sleep`s that carry the
    timing along with the moves they sit between, and replay the stroke faster than it
    was made. (`to_events` now defers a sub-MIN_SLEEP_MS gap rather than discarding it,
    so the ordering no longer decides the *total* duration - only the path's shape,
    which is reason enough.)

    A move held back is flushed when any other event arrives, so a click still
    happens at the exact pixel it was made at rather than at a sample up to
    `interval` stale. That flush usually lands within MIN_SLEEP_MS of the previous
    sample, which puts both moves at one schedule offset. That is intended: a
    same-instant correction of a pixel or two is the price of an exact click.
    """
    out: list[tuple[float, Event]] = []
    last_kept_at: float | None = None
    pending: tuple[float, Event] | None = None

    for at, event in timed:
        if isinstance(event, Move):
            if last_kept_at is None or at - last_kept_at >= interval:
                out.append((at, event))
                last_kept_at = at
                pending = None
            else:
                pending = (at, event)
            continue

        if pending is not None:
            out.append(pending)
            last_kept_at = pending[0]
            pending = None
        out.append((at, event))

    # Where the pointer came to rest is worth keeping even though nothing follows it.
    if pending is not None:
        out.append(pending)
    return out


def accumulate_motion(timed, interval: float = MOTION_SAMPLE_SECONDS):
    """Thin `MoveRel` deltas to one sample per `interval`, summing rather than
    keeping the last.

    `sample_motion`'s keep-last rule is right for `Move`, where only the endpoint
    matters. It is wrong for `MoveRel`: a delta describes displacement, not a
    position, and keeping only the last one in an interval silently discards every
    delta before it - a high-polling-rate mouse would lose most of a turn. Summing
    is the only rule that preserves total displacement, which is why this is a
    separate function rather than a flag on `sample_motion`.

    Same pre-`timeline.to_events` ordering and the same above-`MIN_SLEEP_MS`
    interval rule as `sample_motion`, for the same reason: thinning after
    `to_events` would throw away the `Sleep`s standing between the deltas, and the
    recorded turn would replay faster than it was made.

    `XI2Recorder` already emits whole-unit deltas (it carries its own fractional
    remainder forward - see its module), so summing here is exact integer
    addition with no rounding of its own to worry about.
    """
    out: list[tuple[float, Event]] = []
    window_start: float | None = None
    pending_at: float | None = None
    sum_dx = 0
    sum_dy = 0

    def flush() -> None:
        nonlocal window_start, pending_at, sum_dx, sum_dy
        if pending_at is not None:
            out.append((pending_at, MoveRel(sum_dx, sum_dy)))
        window_start = None
        pending_at = None
        sum_dx = 0
        sum_dy = 0

    for at, event in timed:
        if isinstance(event, MoveRel):
            if window_start is not None and at - window_start >= interval:
                flush()
            if window_start is None:
                window_start = at
            sum_dx += event.dx
            sum_dy += event.dy
            pending_at = at
            continue

        flush()
        out.append((at, event))

    # A held-back sum at the end of the recording is still real displacement.
    flush()
    return out
