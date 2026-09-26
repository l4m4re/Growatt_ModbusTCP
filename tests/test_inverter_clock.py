"""Setting the inverter's real-time clock (#393).

The inverter runs its own RTC and it drifts. That matters because time-of-use windows fire
against the *inverter's* clock, not Home Assistant's — a reporter's 13:00 export window
started two minutes late, and the drift was the reason.

Protocol V1.39 documents holding registers 45-51 as writable:

    45 Sys Year | 46 Sys Month | 47 Sys Day
    48 Sys Hour | 49 Sys Min   | 50 Sys Sec  | 51 Sys Weekly

Confirmed on hardware from two unrelated device classes before implementing:

* an SPH 3600 read 2026/8/22 14:08:19 with weekday 6, and 22 August 2026 was a Saturday —
  so the weekday field counts Monday as 1
* a GroHomeManager-X (DTC 82) on a different site read 2026/8/22 09:42:17 in the same
  registers

The year is the full four digits on both. The off-grid protocol uses the same addresses but
records "Year offset is 2000" and assigns register 51 to Chip Select, and for a long time
that meant writes were withheld there entirely: a four-digit read says nothing about which
form the register accepts, and guessing wrong risks the year 26 AD.

That question is answered now (#443). An SPF 3000-6000 ES PLUS was tested directly, with a
friend at the inverter watching the front panel: register 49 (minute) took a plain
single-register write and held it. Register 45 (year) told the two forms apart cleanly -
writing `26` was accepted, briefly displayed, and then reverted to `2026` on its own;
writing `2027` was accepted, displayed, and held. Off-grid takes the full year, the
opposite of the year-2000 offset every other confirmed family uses.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from datetime import datetime

import pytest

_gm = importlib.import_module("growatt_under_test.growatt_modbus")


class _FakeClock:
    """Records writes and serves a scripted clock reading."""

    def __init__(self, registers=None):
        self.registers = registers
        self.written = None
        self.singles = {}

    def read_holding_registers(self, start, count):
        self.last_read = (start, count)
        if self.registers is None:
            return None
        return self.registers[:count]

    def write_registers(self, register, values):
        self.written = (register, list(values))
        return True

    def write_single(self, register, value):
        self.singles[register] = value
        return True


def _client(offgrid=False, registers=None):
    profile = "SPF_3000_6000_ES_PLUS" if offgrid else "SPH_3000_6000"
    client = _gm.GrowattModbus(connection_type="tcp", host="10.0.0.1", port=502,
                               register_map=profile)
    fake = _FakeClock(registers)
    client.read_holding_registers = fake.read_holding_registers
    client.write_registers = fake.write_registers
    client.write_single_register_any_fc = fake.write_single
    client._fake = fake
    return client


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def test_the_reporters_registers_decode_to_his_timestamp():
    """The exact values from the SPH scan, including the Saturday weekday."""
    client = _client(registers=[2026, 8, 22, 14, 8, 19, 6])
    assert client.read_inverter_time() == datetime(2026, 8, 22, 14, 8, 19)


def test_the_gro_home_manager_registers_decode_too():
    """A different device class on a different site, same layout."""
    client = _client(registers=[2026, 8, 22, 9, 42, 17, 6])
    assert client.read_inverter_time() == datetime(2026, 8, 22, 9, 42, 17)


def test_it_reads_the_documented_block():
    client = _client(registers=[2026, 8, 22, 14, 8, 19, 6])
    client.read_inverter_time()
    assert client._fake.last_read == (45, 7)


def test_an_unreadable_clock_returns_none_rather_than_raising():
    """A fresh or unconfigured inverter can hold zeroes here. The caller still needs to be
    able to set the clock, so this must not blow up."""
    assert _client(registers=[0, 0, 0, 0, 0, 0, 0]).read_inverter_time() is None
    assert _client(registers=None).read_inverter_time() is None
    assert _client(registers=[2026, 13, 40, 99, 99, 99, 9]).read_inverter_time() is None


def test_a_short_response_is_not_decoded():
    assert _client(registers=[2026, 8, 22]).read_inverter_time() is None


# --------------------------------------------------------------------------
# Writing
#
# Method taken from a published, working ESP32 implementation for an SPH5000
# (cosminpop.uk, Feb 2026), corroborated by an ESPHome forum finding three years earlier
# and by two failures on this tracker.
#
# The load-bearing fact: **the year is written as year-2000 and read back as four digits.**
# Write 26, read 2026. Neither protocol document records that for the V1.39 range, and an
# earlier version of this method detected the format by reading the register — which can
# only ever see the four-digit form, so it always wrote the value the inverter rejects.
# --------------------------------------------------------------------------

def test_the_year_is_written_as_an_offset_from_2000():
    """V1.39 devices take a two-digit year in the atomic clock block."""
    client = _client(registers=[2026, 8, 22, 14, 8, 19, 6])
    client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5))
    assert client._fake.written == (45, [26, 8, 25, 9, 30, 5])
    assert client._fake.singles == {}


def test_all_six_fields_are_written_as_one_block():
    """The clock cannot be left half-updated by interleaved bus traffic."""
    client = _client(registers=[2026, 8, 22, 14, 8, 19, 6])
    client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5))

    assert client._fake.written == (45, [26, 8, 25, 9, 30, 5])
    assert client._fake.singles == {}


def test_a_sync_late_in_the_minute_waits_for_the_next_one():
    """The complete block must not cross a minute boundary while being prepared."""
    client = _client(registers=[2026, 8, 22, 14, 8, 19, 6])
    client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 58))

    assert client._fake.written == (45, [26, 8, 25, 9, 31, 0])


def test_no_single_register_fallback_is_attempted():
    """A refused atomic write must not be replaced by a partial sequence."""
    client = _client(registers=[2026, 8, 22, 14, 8, 19, 6])
    client.write_single_register_any_fc = lambda r, v: pytest.fail(
        f"a single-register write to {r} was attempted"
    )
    assert client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5)) is True


def test_a_refused_block_is_reported_without_fallback():
    client = _client(registers=[2026, 8, 22, 14, 8, 19, 6])
    client.write_registers = lambda r, v: False

    with pytest.raises(_gm.ModbusWriteError) as excinfo:
        client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5))

    assert client._fake.singles == {}
    assert "atomic FC16" in str(excinfo.value)


def test_verification_compares_against_the_four_digit_year():
    """The register reads back 2026 having been written 26. Comparing the written value
    against the read value would report a mismatch on every successful sync."""
    client = _client(registers=[2026, 8, 25, 9, 30, 5, 2])
    caplog_free = client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5))
    assert caplog_free is True  # no exception, and the read-back agrees


# --------------------------------------------------------------------------
# Off-grid: reads and writes both work, but the year goes in differently (#443)
# --------------------------------------------------------------------------

def test_off_grid_can_now_be_read_and_written():
    assert _client(offgrid=True).is_clock_readable is True
    assert _client(offgrid=True).is_clock_writable is True
    assert _client(offgrid=False).is_clock_readable is True
    assert _client(offgrid=False).is_clock_writable is True


def test_off_grid_reads_decode_the_four_digit_year():
    """An SPF 5000 ES on firmware 067.02 was read against a known-good host clock and
    returned the full year at register 45, not the offset from 2000 the off-grid document
    records. These are that reporter's values (#444)."""
    client = _client(offgrid=True, registers=[2026, 9, 14, 8, 20, 29, 0])
    assert client.read_inverter_time() == datetime(2026, 9, 14, 8, 20, 29)


def test_off_grid_reads_still_accept_a_two_digit_year():
    """No off-grid device has been seen storing the documented year-2000 offset, but the
    both-encodings decode covers it if one turns up."""
    client = _client(offgrid=True, registers=[26, 9, 14, 8, 20, 29, 0])
    assert client.read_inverter_time() == datetime(2026, 9, 14, 8, 20, 29)


def test_off_grid_writes_the_full_year_not_an_offset():
    """THE finding from #443. Writing 26 to an SPF 3000-6000 ES PLUS was accepted, shown
    on the panel, and then quietly reverted on its own - the opposite of every other
    confirmed family, where 26 is exactly right. Writing the full year is what held."""
    client = _client(offgrid=True, registers=[2026, 8, 22, 14, 8, 19, 6])
    client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5))
    assert client._fake.written == (45, [2026, 8, 25, 9, 30, 5]), (
        "an off-grid inverter was sent the year-2000 offset, which this hardware accepts, "
        "displays, and silently discards"
    )


def test_non_offgrid_still_gets_the_offset_form():
    """The fix must not have flipped this for everyone - only off-grid takes the full
    year."""
    client = _client(offgrid=False, registers=[2026, 8, 22, 14, 8, 19, 6])
    client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5))
    assert client._fake.written == (45, [26, 8, 25, 9, 30, 5])


def test_off_grid_write_is_no_longer_refused():
    """Confirms the write actually reaches the fake client now, rather than raising before
    ever touching it."""
    client = _client(offgrid=True, registers=[2026, 8, 22, 14, 8, 19, 6])
    assert client.write_inverter_time(datetime(2026, 8, 25, 9, 30, 5)) is True
    assert client._fake.written == (45, [2026, 8, 25, 9, 30, 5])


# --------------------------------------------------------------------------
# Service wiring
# --------------------------------------------------------------------------

COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"


def _service_block(name: str) -> str:
    """Return the lines of services.yaml belonging to one top-level service key.

    A text slice rather than yaml.safe_load: this suite is the "no HA" job and runs on a
    bare Python with no third-party packages. Importing PyYAML here turned the whole job
    red for a day without anything else noticing (#393).
    """
    lines = (COMPONENT / "services.yaml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}:"))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_the_service_is_registered_and_documented():
    source = (COMPONENT / "diagnostic.py").read_text(encoding="utf-8")
    assert 'SERVICE_SYNC_INVERTER_TIME = "sync_inverter_time"' in source
    assert "async def sync_inverter_time(call: ServiceCall)" in source
    assert "supports_response=SupportsResponse.OPTIONAL" in source

    block = _service_block("sync_inverter_time")
    assert block, "the service is not exposed in the UI"

    after_fields = block.split("  fields:", 1)[1]
    fields = {
        line.strip().rstrip(":")
        for line in after_fields.splitlines()
        if line.startswith("    ") and not line.startswith("     ") and line.rstrip().endswith(":")
    }
    assert fields == {"device_id", "min_drift_seconds"}, fields


def test_the_undocumented_year_encoding_stays_documented():
    """The year register takes two digits and reports four on V1.39. That is in neither
    protocol document and it is the reason three earlier builds failed, so the explanation
    has to survive somewhere a maintainer will find it.

    It now lives in the docs rather than being repeated in the service picker: the action
    is confirmed working on a MIN TL-X, so the encoding is implementation detail rather
    than a caveat every user needs at the point of use (#393)."""
    root = Path(__file__).parent.parent

    docs = (root / "docs" / "controls" / "actions.md").read_text(encoding="utf-8")
    assert "two-digit year" in docs, "the docs no longer explain the year encoding"
    assert "MIN TL-X" in docs, "the docs do not name the model it is confirmed on"
    assert "issues/393" in docs, "the docs do not say where to report other models"


def test_the_service_picker_no_longer_claims_off_grid_is_excluded():
    """Off-grid clock writes are confirmed working (#443) - the UI description must not
    still tell an SPF/SPE owner the action will not attempt their model."""
    block = _service_block("sync_inverter_time")
    assert "off-grid" in block.lower() and "SPF" in block, (
        "the UI description dropped off-grid entirely - it should say the action covers "
        "SPF/SPE with a different year encoding, not go silent about it"
    )
    assert "Not attempted on" not in block and "excluded" not in block.lower(), (
        "the UI description still tells an off-grid owner this action skips their model"
    )


def test_routine_spf_sign_corrections_are_not_logged_as_warnings():
    """Status 12 corrections fire many times a day on an off-grid inverter in normal
    operation. At warning level they land in Home Assistant's error log looking like a
    fault, and a reporter raised them twice as "this error" while chasing something
    unrelated. The code's own comment says the hardware sign is *expected* to be unreliable
    in that state, so the correction working is not news (#384)."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
              / "growatt_modbus.py").read_text(encoding="utf-8")

    block = source[source.index("if status == 12:"):]
    block = block[:block.index("return battery_power")]
    assert "logger.warning" not in block, (
        "a routine status-12 sign correction is still logged at warning level"
    )
    assert block.count("logger.debug") == 2, "both correction branches should log at debug"
