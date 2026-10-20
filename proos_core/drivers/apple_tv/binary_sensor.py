"""Binary sensor support for Apple TV."""

from pyatv.const import FeatureName, FeatureState, KeyboardFocusState
from pyatv.interface import AppleTV, KeyboardListener

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import CONF_NAME, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SIGNAL_CONNECTED, AppleTvConfigEntry
from .entity import AppleTVEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AppleTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Load Apple TV binary sensors based on a config entry."""
    manager = config_entry.runtime_data
    assert config_entry.unique_id is not None
    name: str = config_entry.data[CONF_NAME]

    # ProOS: connection-health sensor. Added unconditionally (not gated on a
    # connection) so it can report "disconnected" even when the device is offline
    # at startup, and stays `available` regardless of connection state. This is the
    # stable, debounce-friendly health signal the ProOS awareness layer consumes,
    # instead of inferring health from the flapping media_player or the logs.
    async_add_entities([AppleTVConnection(name, config_entry.unique_id, manager)])

    added = False

    @callback
    def setup_entities(atv: AppleTV) -> None:
        nonlocal added
        if added:
            return
        if atv.features.in_state(FeatureState.Available, FeatureName.TextFocusState):
            async_add_entities(
                [AppleTVKeyboardFocused(name, config_entry.unique_id, manager)]
            )
            added = True

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_CONNECTED}_{config_entry.unique_id}", setup_entities
        )
    )

    # The manager may have already connected (and dispatched SIGNAL_CONNECTED)
    # before this platform was forwarded, in which case the signal above was
    # missed; handle that case directly.
    if manager.atv is not None:
        setup_entities(manager.atv)


class AppleTVConnection(AppleTVEntity, BinarySensorEntity):
    """Binary sensor reporting the Apple TV integration connection health.

    `on`  = the integration has a live connection to the device.
    `off` = no connection (device offline, or the protocol link is down/wedged).

    Unlike the media_player, this entity is ALWAYS available, so the awareness
    layer always has a reading to act on.
    """

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_available = True
    _attr_is_on = False

    def __init__(self, name: str, identifier: str, manager) -> None:
        """Initialize a distinct unique_id so it doesn't collide with other entities."""
        super().__init__(name, identifier, manager)
        self._attr_unique_id = f"{identifier}_connection"

    @callback
    def async_device_connected(self, atv: AppleTV) -> None:
        """Handle when a connection is made to the device."""
        self._attr_is_on = True

    @callback
    def async_device_disconnected(self) -> None:
        """Handle when the connection to the device is lost."""
        self._attr_is_on = False


class AppleTVKeyboardFocused(AppleTVEntity, BinarySensorEntity, KeyboardListener):
    """Binary sensor for Text input focused."""

    _attr_translation_key = "keyboard_focused"
    _attr_available = True

    @callback
    def async_device_connected(self, atv: AppleTV) -> None:
        """Handle when connection is made to device."""
        self._attr_available = True
        # Listen to keyboard updates
        atv.keyboard.listener = self
        # Set initial state based on current focus state
        self._update_state(atv.keyboard.text_focus_state is KeyboardFocusState.Focused)

    @callback
    def async_device_disconnected(self) -> None:
        """Handle when connection was lost to device."""
        self._attr_available = False
        self._update_state(False)

    def focusstate_update(
        self, old_state: KeyboardFocusState, new_state: KeyboardFocusState
    ) -> None:
        """Update keyboard state when it changes.

        This is a callback function from pyatv.interface.KeyboardListener.
        """
        self._update_state(new_state is KeyboardFocusState.Focused)

    def _update_state(self, new_state: bool) -> None:
        """Update and report."""
        self._attr_is_on = new_state
        self.async_write_ha_state()
