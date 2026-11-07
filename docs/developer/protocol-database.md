# Protocol Reference

Growatt inverters use three distinct Modbus protocol families. Knowing which protocol your
inverter uses tells you which register ranges are valid and what the integration will
attempt to read.

---

## Protocol Families

| Protocol | Register Range | Inverter Families |
| --- | --- | --- |
| [Modbus RTU V1.39](protocol-v139.md) | 0–124, 875–999, 1000–1124, 2000–2124, 3000–3374, 8000–8139 | MIN, MOD, MID, MAX, MAC, SPH, SPA, WIT, WIS |
| [VPP Protocol](protocol-vpp.md) | 30000–32000+ | SPH, MIN TL-XH, MOD TL3-XH, MID TL3-XH, WIT, WIS |
| [Off-Grid V0.26](protocol-offgrid.md) | 0–97 | SPF, SPE |

---

## Which Protocol Does My Inverter Use?

Most inverters use **Modbus RTU V1.39** as the base protocol. Newer models that support
VPP also respond to both — the integration reads both ranges and merges the data.

```
Inverter startup detection:
1. Try holding register 30000 (DTC code)
   → Success: VPP-capable inverter (also reads V1.39 registers)
   → Failure: Legacy-only inverter (V1.39 or Off-Grid only)

2. If legacy-only, check register range pattern:
   → Responds on 0-124: MIN, SPH, or Off-Grid family
   → Off-Grid protocol (0-97 only): SPF or SPE inverters
```

---

## Register Ranges at a Glance

| Range | Protocol | Content |
| --- | --- | --- |
| 0–124 | V1.39 | Base holding and input registers (all models) |
| 875–999 | V1.39 | WIT commercial registers |
| 1000–1124 | V1.39 | SPH/SPA storage control |
| 2000–2124 | V1.39 | SPA extended (single-phase AC storage) |
| 3000–3374 | V1.39 | MIN/MOD extended data |
| 8000–8139 | V1.39 | WIT battery registers |
| 30000–30099 | VPP | Device ID, rated parameters |
| 30100–30499 | VPP | Control (AC power, battery, TOU schedule) |
| 31000–31499 | VPP | Real-time status, PV, grid, battery, load |

---

## Universal Register Scanner

The **Universal Register Scanner** action (Settings → Tools → Actions →
`growatt_modbus.export_register_dump`) scans every range listed above and reports
which registers your inverter actually responds to. This is the quickest way to
verify protocol support and collect data for a new model.

See [Diagnostic Actions](../troubleshooting/diagnostic-service.md) for usage instructions.

## GII register source

The project-independent register specification is maintained in
`growatt-inverter-info`. Generate the local interchange snapshot from a
checked-out sibling repository with:

```bash
python tools/export_gii_register_source.py \
  --gii-spec ../growatt-inverter-info/spec/growatt-register-spec.json
```

The generated `Protocols/growatt_gii_register_source.json` contains the GII
canonical name, table/function-code identity, datatype, scale, applicability,
evidence state and provenance for every row, including the VPP 30000+ and
31000+ blocks. It is a consumer source snapshot, not a second hand-maintained
register map. Update it when the pinned GII commit changes.

The ARM reverse-engineering correlation is exported separately so the
consumer map and firmware evidence cannot be mistaken for one another:

```bash
python tools/export_zaba22_firmware_evidence.py
```

This creates `Protocols/growatt_zaba22_firmware_register_correlation.json`
with the ZABA22 direct-route boundaries, provider/write-case evidence,
document conflicts, the executable legacy-provider subset, scoped post-table
searches and the offline capture-conformance aggregate. It contains derived
public-safe evidence and hashes only; the firmware image and captures remain
outside this repository.
