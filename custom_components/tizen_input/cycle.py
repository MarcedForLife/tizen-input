"""What a press of the Cycle button does.

Kept free of Home Assistant imports so the decision can be tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from .client import Source


class DisplayPower(StrEnum):
    """The display's power as a press sees it."""

    OFF = "off"
    ON = "on"
    # The press just turned the display on, or there's no power control, so the press
    # won't turn it on or off.
    KEEP = "keep"


class CycleAction(StrEnum):
    """What a press does."""

    TURN_ON = "turn_on"
    SELECT = "select"
    TURN_OFF = "turn_off"
    # Run the default entity and select the default input, whichever are set.
    RUN_DEFAULTS = "run_defaults"
    STAY = "stay"


@dataclass(frozen=True)
class CycleStep:
    """A press's action, with the input to select for SELECT.

    reselect is selected before acting, to bring the input viewer back in front of an app the
    press launched.
    """

    action: CycleAction
    source: Source | None = None
    reselect: Source | None = None


def cycle_position(source: Source) -> tuple[str, int]:
    """Inputs cycle by type, then number, whatever order the app lists them in."""
    return source.type, source.number


def connected_in_order(sources: Iterable[Source]) -> list[Source]:
    return sorted((source for source in sources if source.connected), key=cycle_position)


def _next_connected(current: Source | None, connected: list[Source]) -> Source | None:
    """The first connected input after the current one, wrapping, or None when no other has a signal."""
    if current is None:
        return connected[0] if connected else None
    others = [source for source in connected if cycle_position(source) != cycle_position(current)]
    if not others:
        return None
    later = [source for source in others if cycle_position(source) > cycle_position(current)]
    return (later or others)[0]


def _choose_action(
    power: DisplayPower, current: Source | None, sources: Sequence[Source], has_defaults: bool
) -> CycleStep:
    if power is DisplayPower.OFF:
        return CycleStep(CycleAction.TURN_ON)
    connected = connected_in_order(sources)
    if target := _next_connected(current, connected):
        return CycleStep(CycleAction.SELECT, target)
    if not connected and has_defaults:
        return CycleStep(CycleAction.RUN_DEFAULTS)
    return CycleStep(CycleAction.TURN_OFF if power is DisplayPower.ON else CycleAction.STAY)


def choose_step(
    power: DisplayPower,
    current: Source | None,
    sources: Sequence[Source],
    has_default_entity: bool,
    has_default_input: bool,
    app_launched: bool = False,
) -> CycleStep:
    """An off display turns on first.

    Once it's on, a press moves to the next input with a signal, runs the defaults when no input
    has one, and otherwise turns the display off. A press that launched the app has the app's UI
    in front, so a step that selects no input re-selects the current one first.
    """
    step = _choose_action(power, current, sources, has_default_entity or has_default_input)
    selects_no_input = step.action in (CycleAction.TURN_OFF, CycleAction.STAY) or (
        step.action is CycleAction.RUN_DEFAULTS and not has_default_input
    )
    if app_launched and current is not None and selects_no_input:
        return replace(step, reselect=current)
    return step
