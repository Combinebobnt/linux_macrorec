"""Motion reduction at record time.

Recording raw pointer motion produces thousands of lines for a few seconds of
mousing, which defeats the point of a hand-editable file. Only the position where
something actually happens is worth keeping, so a run of moves collapses to its last
one, and that survivor is kept only if a mouse action follows it.

Freehand path capture is deliberately not supported; a decimating recorder mode is
listed as a follow-up.
"""

from __future__ import annotations

from .events import MOUSE_EVENTS, Event, Move, Sleep


def collapse_motion(events) -> list[Event]:
    """Drop intermediate and pointless pointer moves, preserving timing.

    Sleeps are transparent: they are kept in place, and they do not break up a run
    of moves or hide the mouse action that justifies keeping one.
    """
    events = list(events)
    out: list[Event] = []
    pending_move: Move | None = None
    pending_sleeps: list[Sleep] = []

    for event in events:
        if isinstance(event, Move):
            pending_move = event
            continue

        if isinstance(event, Sleep):
            if pending_move is None:
                out.append(event)
            else:
                pending_sleeps.append(event)
            continue

        if pending_move is not None:
            if isinstance(event, MOUSE_EVENTS):
                out.append(pending_move)
            out.extend(pending_sleeps)
            pending_move = None
            pending_sleeps = []
        out.append(event)

    # A trailing run of moves led to nothing, but the time it took still passed.
    out.extend(pending_sleeps)
    return out
