"""Constants for the Tizen Input integration."""

DOMAIN = "tizen_input"
CONF_SOURCES = "sources"
CONF_UNREADABLE_SOURCE = "unreadable_source"
CONF_DEFAULT_ENTITY = "default_entity"
CONF_DEFAULT_INPUT = "default_input"

TURN_ON_SERVICE = ("homeassistant", "turn_on")
# The service a Cycle press calls on the default entity, by the entity's domain.
DEFAULT_ENTITY_SERVICES: dict[str, tuple[str, str]] = {
    "button": ("button", "press"),
    "input_button": ("input_button", "press"),
    "input_boolean": TURN_ON_SERVICE,
    "light": TURN_ON_SERVICE,
    "scene": TURN_ON_SERVICE,
    "script": TURN_ON_SERVICE,
    "switch": TURN_ON_SERVICE,
}
