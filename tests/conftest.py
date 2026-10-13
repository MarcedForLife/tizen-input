"""Load the integration's Home Assistant free modules without its package __init__, which needs Home Assistant."""

import socket
import sys
import types
from pathlib import Path

import pytest

PACKAGE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "tizen_input"

# A bare package that only knows its path, so client.py and power.py import each other as usual.
package = types.ModuleType("tizen_input")
package.__path__ = [str(PACKAGE_PATH)]
sys.modules["tizen_input"] = package


@pytest.fixture
def unused_port() -> int:
    """A local TCP port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
