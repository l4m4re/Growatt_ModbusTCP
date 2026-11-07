"""Staged MOD/MIN TL-XH TOU editing.

The editor keeps a complete period in memory until Apply is pressed, then reads, compares
and writes the two-register period pair once. Other inverter families keep their own TOU
register handling and do not use this controller.
"""
import asyncio
from dataclasses import dataclass
from datetime import time as dt_time
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError

from .const import MOD_TOU_PERIODS
from .tou_packing import with_enable, with_priority, with_time

if TYPE_CHECKING:
    from .coordinator import GrowattModbusCoordinator


class TouEditorError(HomeAssistantError):
    """A staged TOU operation could not be completed."""


@dataclass(frozen=True)
class TouDraft:
    """One complete, uncommitted TOU period."""

    start: dt_time
    end: dt_time
    priority: int
    enabled: bool
    base_pair: tuple[int, int]
    dirty_fields: frozenset[str] = frozenset()

    @property
    def dirty(self) -> bool:
        """Whether this draft differs from the values it was based on."""
        return bool(self.dirty_fields)


@dataclass(frozen=True)
class TouApplyResult:
    """Result of one fresh-read/compare/write/read-back operation."""

    pair: tuple[int, int]
    written: bool


def _period_definition(period: int) -> dict:
    """Return a MOD TOU period definition or raise a user-facing error."""
    if not 1 <= period <= len(MOD_TOU_PERIODS):
        raise TouEditorError(f"TOU period must be between 1 and {len(MOD_TOU_PERIODS)}")
    return MOD_TOU_PERIODS[period - 1]


def _decode_pair(start_raw: int, end_raw: int, base_pair: tuple[int, int]) -> TouDraft:
    """Decode the two packed registers into editor values."""
    start_raw = int(start_raw)
    end_raw = int(end_raw)
    try:
        start = dt_time((start_raw >> 8) & 0x1F, start_raw & 0xFF)
        end = dt_time((end_raw >> 8) & 0x1F, end_raw & 0xFF)
    except ValueError as err:
        raise TouEditorError(
            f"Invalid TOU time values 0x{start_raw:04X}/0x{end_raw:04X}"
        ) from err
    return TouDraft(
        start=start,
        end=end,
        priority=(start_raw >> 13) & 0x3,
        enabled=bool(start_raw & 0x8000),
        base_pair=base_pair,
    )


def _encode_pair(draft: TouDraft, current_start: int) -> tuple[int, int]:
    """Encode all four draft values into one complete register pair."""
    start = with_time(current_start, draft.start.hour, draft.start.minute)
    start = with_priority(start, draft.priority)
    start = with_enable(start, draft.enabled)
    end = (draft.end.hour << 8) | draft.end.minute
    return start, end


def _read_pair(client, period: dict) -> tuple[int, int]:
    """Read one period pair while the Modbus bus is held."""
    values = client.read_holding_registers(period["start_reg"], 2)
    if values is None or len(values) != 2:
        raise TouEditorError(
            f"Could not read TOU period {period['period']} registers "
            f"{period['start_reg']}/{period['end_reg']}; nothing was written"
        )
    return int(values[0]), int(values[1])


def _apply_pair(
    client,
    period: dict,
    draft: TouDraft,
    expected_pair: tuple[int, int] | None,
) -> TouApplyResult:
    """Apply one complete period pair in a single executor-thread transaction."""
    with client.write_batch(f"Apply MOD TOU period {period['period']}"):
        current_pair = _read_pair(client, period)

        if expected_pair is not None and current_pair != expected_pair:
            raise TouEditorError(
                f"TOU period {period['period']} changed externally since editing started "
                f"(current 0x{current_pair[0]:04X}/0x{current_pair[1]:04X}, "
                f"expected 0x{expected_pair[0]:04X}/0x{expected_pair[1]:04X}); "
                "reload before applying"
            )

        desired_pair = _encode_pair(draft, current_pair[0])
        if desired_pair == current_pair:
            return TouApplyResult(pair=current_pair, written=False)

        if not client.write_registers(period["start_reg"], list(desired_pair)):
            raise TouEditorError(
                f"TOU period {period['period']} write was rejected "
                f"(registers {period['start_reg']}/{period['end_reg']})"
            )

        read_back = _read_pair(client, period)
        if read_back != desired_pair:
            raise TouEditorError(
                f"TOU period {period['period']} write was accepted but read-back differed "
                f"(wanted 0x{desired_pair[0]:04X}/0x{desired_pair[1]:04X}, "
                f"got 0x{read_back[0]:04X}/0x{read_back[1]:04X})"
            )

        return TouApplyResult(pair=read_back, written=True)


class TouEditor:
    """Manage draft values and atomic writes for one Growatt coordinator."""

    def __init__(self, coordinator: GrowattModbusCoordinator) -> None:
        """Initialize the editor."""
        self.coordinator = coordinator
        self._drafts: dict[int, TouDraft] = {}
        self._write_lock = asyncio.Lock()

    def _data_pair(self, period: dict) -> tuple[int, int] | None:
        """Return the latest coordinator pair when it contains real inverter data."""
        if not getattr(self.coordinator, "has_real_data", False):
            return None
        data = self.coordinator.data
        if data is None:
            return None
        return (
            int(getattr(data, period["start_field"], 0)),
            int(getattr(data, period["end_field"], 0)),
        )

    def draft(self, period: int) -> TouDraft | None:
        """Return a draft, initializing it from the latest poll when needed."""
        period_def = _period_definition(period)
        current = self._drafts.get(period)
        if current is not None and current.dirty:
            return current

        pair = self._data_pair(period_def)
        if pair is None:
            return current
        if current is None or current.base_pair != pair:
            try:
                current = _decode_pair(*pair, base_pair=pair)
            except TouEditorError:
                return current
            self._drafts[period] = current
        return current

    def pending_fields(self, period: int) -> tuple[str, ...]:
        """Return the fields currently changed in a draft."""
        draft = self.draft(period)
        return tuple(sorted(draft.dirty_fields)) if draft else ()

    def is_dirty(self, period: int) -> bool:
        """Return whether a period has uncommitted changes."""
        draft = self.draft(period)
        return bool(draft and draft.dirty)

    def update_draft(self, period: int, field: str, value) -> None:
        """Change one draft field without touching Modbus."""
        if field not in {"start", "end", "priority", "enabled"}:
            raise TouEditorError(f"Unknown TOU draft field: {field}")
        draft = self.draft(period)
        if draft is None:
            raise TouEditorError("TOU values are not available until the inverter has replied")
        if field in {"start", "end"} and not isinstance(value, dt_time):
            raise TouEditorError(f"TOU {field} must be a time")
        if field == "priority" and value not in (0, 1, 2):
            raise TouEditorError("TOU priority must be 0 (Load), 1 (Battery) or 2 (Grid)")
        if field == "enabled" and not isinstance(value, bool):
            raise TouEditorError("TOU enabled must be a boolean")

        updated = TouDraft(
            start=value if field == "start" else draft.start,
            end=value if field == "end" else draft.end,
            priority=value if field == "priority" else draft.priority,
            enabled=value if field == "enabled" else draft.enabled,
            base_pair=draft.base_pair,
        )
        base = _decode_pair(*draft.base_pair, base_pair=draft.base_pair)
        updated = TouDraft(
            start=updated.start,
            end=updated.end,
            priority=updated.priority,
            enabled=updated.enabled,
            base_pair=updated.base_pair,
            dirty_fields=frozenset(
                name
                for name in ("start", "end", "priority", "enabled")
                if getattr(updated, name) != getattr(base, name)
            ),
        )
        self._drafts[period] = updated
        self.coordinator.async_update_listeners()

    async def reload_period(self, period: int) -> TouDraft:
        """Discard a draft and reload one period from the inverter."""
        period_def = _period_definition(period)
        async with self._write_lock:
            pair = await self.coordinator.hass.async_add_executor_job(
                self._read_pair_locked, period_def
            )
        draft = _decode_pair(*pair, base_pair=pair)
        self._drafts[period] = draft
        self.coordinator.async_update_listeners()
        return draft

    def _read_pair_locked(self, period: dict) -> tuple[int, int]:
        """Read one pair while excluding the poller."""
        with self.coordinator.modbus_client.write_batch(
            f"Reload MOD TOU period {period['period']}"
        ):
            return _read_pair(self.coordinator.modbus_client, period)

    async def async_set_tou_period(
        self,
        period: int,
        start: dt_time,
        end: dt_time,
        priority: int,
        enabled: bool,
        *,
        expected_pair: tuple[int, int] | None = None,
    ) -> TouApplyResult:
        """Apply a complete TOU period using one central write operation."""
        period_def = _period_definition(period)
        if not isinstance(start, dt_time) or not isinstance(end, dt_time):
            raise TouEditorError("TOU start and end must be times")
        if priority not in (0, 1, 2):
            raise TouEditorError("TOU priority must be 0 (Load), 1 (Battery) or 2 (Grid)")
        if not isinstance(enabled, bool):
            raise TouEditorError("TOU enabled must be a boolean")
        draft = TouDraft(
            start=start,
            end=end,
            priority=priority,
            enabled=enabled,
            base_pair=expected_pair if expected_pair is not None else (0, 0),
        )
        async with self._write_lock:
            result = await self.coordinator.hass.async_add_executor_job(
                _apply_pair,
                self.coordinator.modbus_client,
                period_def,
                draft,
                expected_pair,
            )

        confirmed = _decode_pair(*result.pair, base_pair=result.pair)
        self._drafts[period] = confirmed
        data = self.coordinator.data
        if data is not None:
            setattr(data, period_def["start_field"], result.pair[0])
            setattr(data, period_def["end_field"], result.pair[1])
        if result.written:
            self.coordinator.track_write(
                period_def["start_reg"], result.pair[0], period_def["start_field"]
            )
            self.coordinator.track_write(
                period_def["end_reg"], result.pair[1], period_def["end_field"]
            )
        self.coordinator.async_update_listeners()
        await self.coordinator.async_request_refresh()
        return result

    async def apply_period(self, period: int) -> TouApplyResult:
        """Apply the current manual draft with conflict detection."""
        draft = self.draft(period)
        if draft is None:
            raise TouEditorError("TOU values are not available until the inverter has replied")
        return await self.async_set_tou_period(
            period,
            draft.start,
            draft.end,
            draft.priority,
            draft.enabled,
            expected_pair=draft.base_pair,
        )


def get_tou_editor(coordinator: GrowattModbusCoordinator) -> TouEditor:
    """Return the editor attached to a coordinator."""
    editor = getattr(coordinator, "tou_editor", None)
    if editor is None:
        editor = TouEditor(coordinator)
        coordinator.tou_editor = editor
    return editor
