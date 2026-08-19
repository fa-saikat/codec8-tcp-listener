# Teltonika Code8 TCP Listener

------

## Codec8 AVL Record - Field Reference

Reference notes for the JSON records emitted by `tcp-listener.py`. Example record:

```json
{
  "timestamp": "2026-08-18T08:01:30.550000+00:00",
  "priority": 1,
  "lat": 23.8103732,
  "lon": 90.4123531,
  "altitude": 107,
  "angle": 243,
  "satellites": 7,
  "speed": 3,
  "event_io_id": 1,
  "io": {"239": 0, "66": 12100, "16": 28830},
  "imei": "123456789012345"
}
```

---

## `timestamp`

UTC datetime of the GPS fix, decoded from the record's 8-byte millisecond epoch timestamp.

## `priority`

Priority of the record (0 = low, 1 = high, 2 = panic). Affects how urgently the device tries to deliver it, not something the parser currently acts on.

## `lat` / `lon`

Latitude/longitude in decimal degrees, decoded from signed 4-byte integers scaled by `10,000,000`.

## `altitude`

Altitude in meters above sea level, from the GPS chip. Signed 2-byte integer - can be negative for below-sea-level locations, though on a bench test this is usually just GPS noise.

## `angle`

Heading / bearing - the direction the device is pointing, in degrees clockwise from true north (0–360°).

| Value | Direction |
| ----- | --------- |
| 0°    | North     |
| 90°   | East      |
| 180°  | South     |
| 270°  | West      |

`angle=243` → heading roughly west-southwest. This comes straight off the GPS chip and isn't necessarily accurate at very low speeds (like the 3 km/h in the example) since heading is noisy when barely moving.

## `satellites`

Number of GPS satellites the device had a lock on for this fix.

| Count | Fix quality                               |
| ----- | ----------------------------------------- |
| 0–3   | Unreliable / no real fix (common indoors) |
| 4     | Theoretical minimum for 3D fix            |
| 6–10+ | Solid fix                                 |

Matches what's been observed bench-testing the FMC130 indoors - low satellite counts are expected there.

## `speed`

Speed in **km/h**, taken directly from the GPS receiver (not calculated by comparing consecutive points). `speed=3` ≈ stationary, consistent with a bench test.

## `event_io_id`

The IO ID that *triggered* this record being sent (as opposed to it being a regular time/distance-interval report). E.g. if `event_io_id=239`, the record was pushed because ignition state changed.

## `io` - I/O element object

Keyed by **IO ID** ("AVL ID" in Teltonika's docs), grouped internally by value byte-width (1/2/4/8 bytes) but flattened into one dict by the parser. The parser reads these generically - it doesn't currently know what each ID *means*.

### IDs seen in the FMC130 test data

| IO ID | Meaning               | Width   | Raw value | Interpreted      |
| ----- | --------------------- | ------- | --------- | ---------------- |
| 239   | Ignition status       | 1 byte  | `0`       | Ignition **off** |
| 66    | External voltage (mV) | 2 bytes | `12100`   | 12.1 V           |
| 16    | Total odometer (m)    | 4 bytes | `28830`   | ~28.8 km         |

### Notes / gotchas

- **IDs are device/firmware-specific.** Full FMC130 AVL ID list: [Teltonika Codec wiki](https://wiki.teltonika-gps.com/view/Codec).
- **No scaling is currently applied.** Some IO values need dividing (e.g. certain voltage/temperature IDs are scaled ×10 or ×100) - check each ID's spec rather than assuming raw = final value.
- **Keys are strings, not ints, in the JSON output** - `json.dumps` stringifies dict keys, so `io["239"]` not `io[239]` when reading the JSON back.

---

## Accelerometer / motion-event IO IDs (FMC130, not yet in test data)

The FMC130 has its own built-in 3-axis accelerometer, independent of any vehicle CAN/OBD-II bus. These IDs are **not sent by default** - each scenario has to be enabled in Teltonika Configurator (Features Settings → Accelerometer Features / Green Driving / Crash Detection) before it will start appearing in `io`. They're eventual (event-triggered) records, not continuous like GPS/speed.

| IO ID | Meaning                  | Width          | Values / notes                                               |
| ----- | ------------------------ | -------------- | ------------------------------------------------------------ |
| 246   | Towing                   | 1 byte         | `0` = steady, `1` = towing detected (unexpected movement while parked, ignition off) |
| 247   | Crash detection          | 1 byte         | `1` = crash, `2`/`3` = limited crash trace (uncalibrated/calibrated device), up to `5` depending on severity |
| 251   | Idling                   | 1 byte         | `0` = moving, `1` = idling (ignition on, no GPS/accelerometer movement) |
| 252   | Unplug                   | 1 byte         | `0` = battery present, `1` = device unplugged from external power |
| 253   | Green driving event type | 1 byte         | `1` = harsh acceleration, `2` = harsh braking, `3` = harsh cornering |
| 254   | Green driving value      | 1 byte         | peak acceleration during the event, in hundredths of *g*. Not present if "advanced eco driving" mode is used - replaced by separate average/max/duration IOs instead |
| 257   | Crash trace data         | variable (HEX) | raw high-frequency X/Y/Z accelerometer samples captured around a crash event, for forensic reconstruction. Only sent alongside AVL ID 247 when Crash Trace is enabled |

Things that still genuinely require a CAN/OBD-II connection (not available from the FMC130 alone): engine RPM, fuel level/consumption, coolant temperature, accelerator pedal position, and DTC/error codes. The current odometer (IO 16) is GPS-distance-calculated by the device itself, not CAN-derived, so that one already works without OBD.
