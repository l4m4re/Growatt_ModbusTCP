"""Packed MOD/MID/MIN TL-XH TOU words for holding registers 3038-3059."""

PRIORITY_MASK = 0x6000   # bits 13-14
ENABLE_MASK = 0x8000     # bit 15
FLAGS_MASK = 0xE000      # enable + priority


def with_priority(start: int, priority: int) -> int:
    """Start word with its priority replaced, time and enable kept."""
    return (int(start) & ~PRIORITY_MASK & 0xFFFF) | ((int(priority) & 0x3) << 13)


def with_enable(start: int, enabled: bool) -> int:
    """Start word with its enable bit replaced, time and priority kept."""
    return (int(start) & ~ENABLE_MASK & 0xFFFF) | (ENABLE_MASK if enabled else 0)


def with_time(start: int, hour: int, minute: int) -> int:
    """Start word with its time replaced, enable and priority kept."""
    return (int(start) & FLAGS_MASK) | ((int(hour) & 0x1F) << 8) | (int(minute) & 0xFF)
