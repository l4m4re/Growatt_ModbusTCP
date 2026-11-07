"""Time platform for Growatt Modbus Integration."""
import logging
from datetime import time as dt_time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, WRITABLE_REGISTERS, CONF_REGISTER_MAP, get_device_type_for_control, DEVICE_TYPE_BATTERY, MOD_TOU_PERIODS, TOU_LOCKED_MESSAGE
from .coordinator import GrowattModbusCoordinator
from .entity import GrowattEntity
from .growatt_modbus import ModbusWriteError, RATE_LIMITED_MESSAGE
from .tou_editor import get_tou_editor

_LOGGER = logging.getLogger(__name__)

# Writable platform — serialise. See number.py for the reasoning.
PARALLEL_UPDATES = 1

# Controls that use hex-packed time encoding: register_value = hours*256 + minutes
# e.g. 06:00 = 0x0600 = 1536, 22:00 = 0x1600 = 5632
TIME_CONTROLS = {k for k in WRITABLE_REGISTERS if 'time_period' in k and k.endswith(('_start', '_end'))}

# MOD_TOU_PERIODS is defined in const.py and imported above


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Growatt Modbus time entities."""
    coordinator = config_entry.runtime_data

    register_map_name = config_entry.data.get(CONF_REGISTER_MAP)
    from .const import REGISTER_MAPS
    holding_registers = REGISTER_MAPS.get(register_map_name, {}).get('holding_registers', {})

    entities = []
    for control_name in sorted(TIME_CONTROLS):  # sorted for deterministic order
        control_config = WRITABLE_REGISTERS[control_name]
        if control_config['register'] not in holding_registers:
            continue
        entities.append(GrowattGenericTime(coordinator, config_entry, control_name, control_config))
        _LOGGER.info("%s time control enabled (register %d)", control_name, control_config['register'])

    # WIT VPP TOU time pickers (periods 1-10, start + end each = up to 20 entities)
    is_wit = str(register_map_name).upper() in ("WIT_4000_15000TL3", "WIT_29900_50000TL3_XHU")
    if is_wit:
        for _period in range(1, 11):
            _start_reg = 30412 + (_period - 1) * 3
            if _start_reg in holding_registers:
                entities.append(GrowattWitVppTouTime(coordinator, config_entry, _period, is_start=True))
                entities.append(GrowattWitVppTouTime(coordinator, config_entry, _period, is_start=False))

    # MOD/MIN TL-XH staged TOU time pickers - not on firmware that rejects them (#485)
    if 3038 in holding_registers and coordinator.tou_legacy_locked:
        _LOGGER.info("MOD TOU time controls not created: firmware VPP %s (#485)",
                     coordinator.vpp_protocol_label)
    elif 3038 in holding_registers:
        tou_off = REGISTER_MAPS.get(register_map_name, {}).get('tou_disabled_by_default', False)
        for period_def in MOD_TOU_PERIODS:
            for is_start in (True, False):
                entity = GrowattModTouTime(coordinator, config_entry, period_def, is_start=is_start)
                if tou_off:
                    entity._attr_entity_registry_enabled_default = False
                entities.append(entity)
        _LOGGER.info("MOD TOU time controls enabled (%d time entities for %d periods)",
                     len(MOD_TOU_PERIODS) * 2, len(MOD_TOU_PERIODS))

    if entities:
        _LOGGER.info("Created %d time entities for %s", len(entities), config_entry.data['name'])
        async_add_entities(entities)


class GrowattWitVppTouTime(GrowattEntity, TimeEntity):
    """WIT VPP TOU period start or end time.

    Stores time as plain minutes since midnight (0–1439 start, 0–1440 end).
    Converts to/from datetime.time for the HA time picker.
    Writes use FC16 — WIT inverter rejects FC06 on VPP holding registers.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: GrowattModbusCoordinator,
        config_entry: ConfigEntry,
        period: int,
        is_start: bool,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            f"vpp_tou_p{period}_{'start' if is_start else 'end'}",
            DEVICE_TYPE_BATTERY,
        )
        self._period = period
        self._is_start = is_start
        offset = 0 if is_start else 1
        self._register = 30412 + (period - 1) * 3 + offset
        self._coordinator_attr = f"wit_vpp_tou_p{period}_{'start' if is_start else 'end'}"

        self._attr_icon = "mdi:clock-start" if is_start else "mdi:clock-end"
        label = "Start" if is_start else "End"
        self._attr_name = f"TOU Period {period} {label}"

    @property
    def native_value(self) -> dt_time | None:
        minutes = getattr(self.coordinator, self._coordinator_attr, None)
        if minutes is None:
            return None
        try:
            m = int(minutes)
            return dt_time(m // 60, m % 60)
        except (ValueError, ZeroDivisionError):
            return None

    async def async_set_value(self, value: dt_time) -> None:
        minutes = value.hour * 60 + value.minute
        slot = "start" if self._is_start else "end"
        _LOGGER.info("[WIT-TOU] Period %d %s → %s (%d min, reg %d)",
                     self._period, slot, value.strftime("%H:%M"), minutes, self._register)
        try:
            success = await self.hass.async_add_executor_job(
                self.coordinator.modbus_client.write_registers,
                self._register,
                [minutes],
            )
            if success:
                setattr(self.coordinator, self._coordinator_attr, minutes)
                _LOGGER.info("[WIT-TOU] Period %d %s set to %s", self._period, slot, value.strftime("%H:%M"))
                await self.coordinator.async_request_refresh()
            else:
                _LOGGER.error("[WIT-TOU] Failed to write period %d %s (reg %d)", self._period, slot, self._register)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("[WIT-TOU] Period %d %s write error: %s", self._period, slot, err)


class GrowattGenericTime(GrowattEntity, TimeEntity):
    """Time entity for inverter time period start/end controls.

    Hardware stores time as hex-packed bytes: hours*256 + minutes.
    e.g. 06:00 = 0x0600 = 1536, 22:00 = 0x1600 = 5632.
    """

    # has_entity_name comes from GrowattEntity, as do unique_id and device_info.
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: GrowattModbusCoordinator,
        config_entry: ConfigEntry,
        control_name: str,
        control_config: dict,
    ) -> None:
        """Initialize the time entity."""
        super().__init__(
            coordinator,
            config_entry,
            control_name,
            get_device_type_for_control(control_name),
        )
        self._control_name = control_name
        self._control_config = control_config

        friendly_name = control_config.get('label') or control_name.replace('_', ' ').title()
        self._attr_name = friendly_name

    @property
    def native_value(self) -> dt_time | None:
        """Return the current time value decoded from hex-packed register."""
        data = self.coordinator.data
        if data is None:
            return None
        raw = getattr(data, self._control_name, None)
        if raw is None:
            return None
        hours = (int(raw) >> 8) & 0xFF
        minutes = int(raw) & 0xFF
        try:
            return dt_time(hours, minutes)
        except ValueError:
            _LOGGER.warning(
                "Invalid packed time value 0x%04X (%d) for %s — hours=%d, minutes=%d",
                int(raw), int(raw), self._control_name, hours, minutes,
            )
            return None

    async def async_set_value(self, value: dt_time) -> None:
        """Write a new time value using atomic FC16 for [start, end, enable] triples.

        SPH firmware silently rejects FC06 single-register writes to time period start/end
        registers — the value appears to write but reverts within ~6 seconds. The inverter
        requires all three registers [start, end, enable] written atomically in one FC16 call.

        Falls back to FC06 single-register write if sibling registers can't be resolved.
        """
        raw_value = (value.hour << 8) | value.minute
        register = self._control_config['register']
        name = self._control_name

        # Identify start/end triples and resolve siblings
        if name.endswith('_start'):
            base = name[:-6]
            is_start = True
        elif name.endswith('_end'):
            base = name[:-4]
            is_start = False
        else:
            await self._write_single(register, raw_value, value)
            return

        start_name = f"{base}_start"
        end_name = f"{base}_end"
        enable_name = f"{base}_enable"

        start_cfg = WRITABLE_REGISTERS.get(start_name)
        end_cfg = WRITABLE_REGISTERS.get(end_name)
        enable_cfg = WRITABLE_REGISTERS.get(enable_name)

        if not start_cfg or not end_cfg or not enable_cfg:
            _LOGGER.warning(
                "%s: sibling registers not found — falling back to single-register FC06 write", name,
            )
            await self._write_single(register, raw_value, value)
            return

        start_reg = start_cfg['register']
        end_reg = end_cfg['register']
        enable_reg = enable_cfg['register']
        if end_reg != start_reg + 1 or enable_reg != start_reg + 2:
            _LOGGER.warning(
                "%s: registers not consecutive (start=%d end=%d enable=%d) — falling back",
                name, start_reg, end_reg, enable_reg,
            )
            await self._write_single(register, raw_value, value)
            return

        # Read the current [start, end, enable] triple fresh from hardware rather than
        # trusting coordinator.data (which is up to scan_interval stale). Using cached
        # values for sibling registers causes back-to-back writes within the same poll
        # window to revert each other: the second write reads the old sibling value and
        # writes it back, silently undoing the first write.
        triple = await self.hass.async_add_executor_job(
            self.coordinator.modbus_client.read_holding_registers, start_reg, 3
        )
        if triple is not None and len(triple) >= 3:
            current_start, current_end, current_enable = int(triple[0]), int(triple[1]), int(triple[2])
            values_are_fresh = True
        else:
            _LOGGER.warning(
                "%s: could not read fresh register triple (reg %d) — falling back to cached data",
                name, start_reg,
            )
            data = self.coordinator.data
            current_start = int(getattr(data, start_name, 0) or 0) if data else 0
            current_end = int(getattr(data, end_name, 0) or 0) if data else 0
            current_enable = int(getattr(data, enable_name, 0) or 0) if data else 0
            values_are_fresh = False

        new_start = raw_value if is_start else current_start
        new_end = raw_value if not is_start else current_end

        # Skip a write that would change nothing (#392).
        #
        # These registers are believed to be held in non-volatile memory with finite write
        # endurance — believed rather than known: Growatt marks a handful of VPP registers
        # "Not storage" and documents nothing about the rest, so we treat the rest
        # conservatively. A price-driven controller recomputing all nine slots daily will
        # usually find most of them unchanged, and there is no reason to spend a write
        # cycle proving it. `number.py` has done this since v1.6.6; the time entities did
        # not, which is where a TOU scheduler actually writes.
        #
        # Only when the comparison is against a *fresh* read. On the cached fallback the
        # values may be up to a scan interval old, and a skipped write that should have
        # happened is worse than a redundant one — the same reasoning that makes this
        # method re-read the siblings rather than trust coordinator.data at all.
        if values_are_fresh and new_start == current_start and new_end == current_end:
            _LOGGER.debug(
                "%s: already reads start=0x%04X end=0x%04X — skipping write to register %d",
                name, current_start, current_end, start_reg,
            )
            return

        _LOGGER.debug(
            "%s: atomic FC16 → reg %d [start=0x%04X, end=0x%04X, enable=%d]",
            name, start_reg, new_start, new_end, current_enable,
        )
        try:
            success = await self.hass.async_add_executor_job(
                self.coordinator.modbus_client.write_registers,
                start_reg,
                [new_start, new_end, current_enable],
            )
        except ModbusWriteError:
            _LOGGER.error("Failed atomic FC16 write for %s (register %d)", name, start_reg)
            return

        if success:
            _LOGGER.info(
                "Set %s to %s (atomic FC16: start=0x%04X, end=0x%04X, enable=%d)",
                name, value.strftime("%H:%M"), new_start, new_end, current_enable,
            )
            self.coordinator.track_write(start_reg, new_start, start_name)
            self.coordinator.track_write(end_reg, new_end, end_name)
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.warning("%s: atomic FC16 write returned False (register %d)", name, start_reg)

    async def _write_single(self, register: int, raw_value: int, value: dt_time) -> None:
        """Fallback: write a single register via FC06 with readback verification."""
        try:
            write_ok, verified = await self.hass.async_add_executor_job(
                self.coordinator.modbus_client.write_register_verified, register, raw_value,
            )
        except ModbusWriteError:
            _LOGGER.error("Failed to write %s (register %d)", self._control_name, register)
            return
        if not write_ok:
            raise HomeAssistantError(RATE_LIMITED_MESSAGE.format(name=self._control_name))
        if write_ok:
            if verified:
                _LOGGER.info(
                    "Set %s to %s (raw=0x%04X=%d, verified)",
                    self._control_name, value.strftime("%H:%M"), raw_value, raw_value,
                )
            else:
                _LOGGER.warning(
                    "%s: write succeeded but value reverted. Possible causes: "
                    "ShineWiFi/cloud dongle overriding local writes, inverter firmware "
                    "rejecting the value, or a prerequisite setting not enabled.",
                    self._control_name,
                )
            self.coordinator.track_write(register, raw_value, self._control_name)
            await self.coordinator.async_request_refresh()


class GrowattModTouTime(GrowattEntity, TimeEntity):
    """Time entity for MOD TL3-XH TOU period start/end registers.

    Start registers encode: bit15=enable, bit13-14=priority, bit8-12=hour, bit0-7=minute.
    End registers encode: bit8-12=hour, bit0-7=minute (plain hex-packed).

    Changes are staged and written with the complete period by Apply.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: GrowattModbusCoordinator,
        config_entry: ConfigEntry,
        period_def: dict,
        is_start: bool,
    ) -> None:
        """Initialize the MOD TOU time entity."""
        super().__init__(
            coordinator,
            config_entry,
            f"mod_tou_{period_def['period']}_{'start' if is_start else 'end'}",
            DEVICE_TYPE_BATTERY,
        )
        self._period_def = period_def
        self._is_start = is_start
        self._period = period_def["period"]

        if is_start:
            self._register = period_def["start_reg"]
            self._data_field = period_def["start_field"]
            self._attr_name = f"TOU Period {self._period} Begin"
        else:
            self._register = period_def["end_reg"]
            self._data_field = period_def["end_field"]
            self._attr_name = f"TOU Period {self._period} End"

    @property
    def available(self) -> bool:
        # Not on firmware that rejects these TOU writes (#485).
        return super().available and not self.coordinator.tou_legacy_locked

    @property
    def native_value(self) -> dt_time | None:
        """Return the current draft time."""
        draft = get_tou_editor(self.coordinator).draft(self._period)
        if draft is None:
            return None
        return draft.start if self._is_start else draft.end

    async def async_set_value(self, value: dt_time) -> None:
        """Stage a new time without writing Modbus."""
        if self.coordinator.tou_legacy_locked:
            raise HomeAssistantError(
                TOU_LOCKED_MESSAGE.format(version=self.coordinator.vpp_protocol_label))
        get_tou_editor(self.coordinator).update_draft(
            self._period, "start" if self._is_start else "end", value
        )
