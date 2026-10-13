"""Cycle decision tests.

The decision has no Home Assistant imports, so these run without Home Assistant installed.
"""

from dataclasses import replace

import pytest

from tizen_input.client import Source
from tizen_input.cycle import CycleAction, DisplayPower, choose_step, connected_in_order

PC = Source("DP", 1, "PC")
HDMI_1 = Source("HDMI", 1)
HDMI_2 = Source("HDMI", 2)
OFF, ON, KEEP = DisplayPower.OFF, DisplayPower.ON, DisplayPower.KEEP
TURN_ON, SELECT, TURN_OFF = CycleAction.TURN_ON, CycleAction.SELECT, CycleAction.TURN_OFF
RUN_DEFAULTS, STAY = CycleAction.RUN_DEFAULTS, CycleAction.STAY


def with_signal(*connected):
    """The display's inputs, marked connected where given, in an order unlike the cycle order."""
    return [replace(source, connected=source in connected) for source in (HDMI_2, PC, HDMI_1)]


def label(source):
    return source.label if source else None


@pytest.mark.parametrize(
    ("power", "current", "connected", "has_default_entity", "has_default_input", "action", "target"),
    [
        pytest.param(OFF, PC, [PC, HDMI_2], False, False, TURN_ON, None, id="off turns on"),
        pytest.param(OFF, None, [], True, True, TURN_ON, None, id="off turns on whatever the inputs"),
        pytest.param(ON, PC, [PC, HDMI_2], False, False, SELECT, "HDMI 2", id="next connected input"),
        pytest.param(ON, PC, [PC, HDMI_1, HDMI_2], False, False, SELECT, "HDMI 1", id="nearest next input"),
        pytest.param(ON, HDMI_2, [PC, HDMI_2], False, False, SELECT, "PC", id="wraps to the first input"),
        pytest.param(ON, HDMI_1, [PC, HDMI_2], False, False, SELECT, "HDMI 2", id="current input without a signal"),
        pytest.param(ON, None, [HDMI_1, HDMI_2], False, False, SELECT, "HDMI 1", id="unknown input picks the first"),
        pytest.param(ON, None, [PC], False, False, SELECT, "PC", id="unknown input with one connected"),
        pytest.param(ON, PC, [PC], True, True, TURN_OFF, None, id="only the current input turns off"),
        pytest.param(ON, PC, [], False, False, TURN_OFF, None, id="nothing connected without defaults"),
        pytest.param(ON, PC, [], True, False, RUN_DEFAULTS, None, id="nothing connected with a default entity"),
        pytest.param(ON, None, [], False, True, RUN_DEFAULTS, None, id="nothing connected with a default input"),
        pytest.param(KEEP, HDMI_2, [PC, HDMI_2], False, False, SELECT, "PC", id="kept power still switches"),
        pytest.param(KEEP, PC, [PC], False, False, STAY, None, id="kept power stays on the only input"),
        pytest.param(KEEP, PC, [], False, False, STAY, None, id="kept power stays without defaults"),
        pytest.param(KEEP, None, [], True, True, RUN_DEFAULTS, None, id="kept power runs the defaults"),
    ],
)
def test_chooses_the_step(power, current, connected, has_default_entity, has_default_input, action, target):
    step = choose_step(power, current, with_signal(*connected), has_default_entity, has_default_input)
    assert step.action is action
    assert label(step.source) == target
    assert step.reselect is None


@pytest.mark.parametrize(
    ("power", "current", "connected", "has_default_entity", "has_default_input", "action", "reselect"),
    [
        pytest.param(ON, PC, [PC], False, False, TURN_OFF, "PC", id="before turning off"),
        pytest.param(KEEP, HDMI_2, [HDMI_2], False, False, STAY, "HDMI 2", id="before staying"),
        pytest.param(KEEP, PC, [], False, False, STAY, "PC", id="before staying with nothing connected"),
        pytest.param(ON, PC, [], True, False, RUN_DEFAULTS, "PC", id="before running only the default entity"),
        pytest.param(ON, PC, [], True, True, RUN_DEFAULTS, None, id="not when the default input is selected"),
        pytest.param(ON, PC, [], False, True, RUN_DEFAULTS, None, id="not when only the default input is set"),
        pytest.param(ON, PC, [PC, HDMI_2], False, False, SELECT, None, id="not when switching"),
        pytest.param(ON, None, [], False, False, TURN_OFF, None, id="not when the current input is unknown"),
    ],
)
def test_reselects_the_current_input_after_launching_the_app(
    power, current, connected, has_default_entity, has_default_input, action, reselect
):
    inputs = with_signal(*connected)
    step = choose_step(power, current, inputs, has_default_entity, has_default_input, app_launched=True)
    assert step.action is action
    assert label(step.reselect) == reselect


def test_orders_connected_inputs_by_type_then_number():
    ordered = connected_in_order(with_signal(PC, HDMI_1, HDMI_2))
    assert [source.label for source in ordered] == ["PC", "HDMI 1", "HDMI 2"]
