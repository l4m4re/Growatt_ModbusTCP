"""Apply and reload controls for staged MOD/MIN TL-XH TOU periods."""
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory

from .const import DEVICE_TYPE_BATTERY
from .entity import GrowattEntity
from .tou_editor import get_tou_editor


class _TouButton(GrowattEntity, ButtonEntity):
    """Common state for one staged TOU operation."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, config_entry: ConfigEntry, period: int, action: str) -> None:
        """Initialize a TOU operation button."""
        super().__init__(
            coordinator,
            config_entry,
            f"mod_tou_{period}_{action}",
            DEVICE_TYPE_BATTERY,
        )
        self._period = period

    @property
    def available(self) -> bool:
        """Only offer operations for supported, live TOU periods."""
        return (
            super().available
            and not self.coordinator.tou_legacy_locked
            and get_tou_editor(self.coordinator).draft(self._period) is not None
        )


class GrowattTouApplyButton(_TouButton):
    """Apply one complete TOU period with one atomic FC16 write."""

    _attr_icon = "mdi:content-save-check-outline"

    def __init__(self, coordinator, config_entry: ConfigEntry, period: int) -> None:
        """Initialize an Apply button."""
        super().__init__(coordinator, config_entry, period, "apply")
        self._attr_name = f"TOU Period {period} Apply"

    async def async_press(self) -> None:
        """Apply the draft or raise a clear error without losing it."""
        await get_tou_editor(self.coordinator).apply_period(self._period)


class GrowattTouReloadButton(_TouButton):
    """Discard local edits and reload one TOU period from the inverter."""

    _attr_icon = "mdi:reload"

    def __init__(self, coordinator, config_entry: ConfigEntry, period: int) -> None:
        """Initialize a Reload button."""
        super().__init__(coordinator, config_entry, period, "reload")
        self._attr_name = f"TOU Period {period} Reload"

    async def async_press(self) -> None:
        """Reload the period from hardware."""
        await get_tou_editor(self.coordinator).reload_period(self._period)
