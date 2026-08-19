#!/usr/bin/env python3

# Codec8 emulator / dummy sender for testing tcp-listener.py
# without a real Teltonika device. Performs the IMEI handshake,
# then sends batches of randomly-generated Codec8 AVL records
# with correct CRC-16/IBM checksums.

import argparse
import random
import socket
import struct
import time
from datetime import datetime, timezone

# --- Codec8 constants ---
CODEC_ID = 0x08

# IO elements to fake, grouped by value byte-width (matches parser's
# 1b/2b/4b/8b groups). ids are arbitrary but stable across packets.
IO_1B = {239: lambda: random.choice([0, 1])}          # ignition on/off
IO_2B = {66: lambda: random.randint(11000, 12600)}    # external voltage (mV)
IO_4B = {16: lambda: random.randint(0, 50000)}        # total odometer (m)
IO_8B = {}                                            # none by default

# --- Motion-event IO IDs (FMC130 accelerometer, --motion-events flag) ---
# These are NOT sent on every record in real life -- each only appears when
# its triggering condition happens, and the record that carries it sets
# event_io_id to that ID. IO 257 (crash trace) is intentionally NOT emulated
# here: it's variable-length raw HEX, which doesn't fit standard Codec8's
# fixed 1/2/4/8-byte IO groups -- that needs Codec8 8 Extended framing.
MOTION_EVENT_CHANCE = {
    246: 0.02,   # towing
    247: 0.01,   # crash detection (rare)
    251: 0.05,   # idling
    252: 0.02,   # unplug
    253: 0.05,   # green driving event (fires with 254 alongside it)
}


def maybe_motion_event(ignition: int, speed: int):
    """Decide whether a motion/accelerometer event fires on this record.

    Returns (io_dict, event_io_id) where io_dict is empty if nothing fired.
    Conditions are loosely realistic (e.g. towing only makes sense with
    ignition off; idling only with ignition on and speed 0) but this is
    still a simulator, not a physics model.
    """
    io = {}

    # Towing: unexpected movement while parked, ignition off
    if ignition == 0 and random.random() < MOTION_EVENT_CHANCE[246]:
        io[246] = 1
        return io, 246

    # Crash detection: severity 1 (crash) or 2-5 (limited trace, cal/uncal)
    if random.random() < MOTION_EVENT_CHANCE[247]:
        io[247] = random.choice([1, 2, 3, 4, 5])
        return io, 247

    # Idling: ignition on, stopped, no accelerometer movement
    if ignition == 1 and speed == 0 and random.random() < MOTION_EVENT_CHANCE[251]:
        io[251] = 1
        return io, 251

    # Unplug: device loses external power
    if random.random() < MOTION_EVENT_CHANCE[252]:
        io[252] = 1
        return io, 252

    # Green driving: harsh accel(1) / brake(2) / corner(3) + peak value (hundredths of g)
    if random.random() < MOTION_EVENT_CHANCE[253]:
        event_type = random.choice([1, 2, 3])
        io[253] = event_type
        io[254] = random.randint(35, 90)  # e.g. 0.35g-0.90g peak
        return io, 253

    return io, None


def crc16_ibm(data: bytes) -> int:
    """CRC-16/IBM (aka ARC): poly 0xA001, init 0x0000 — what Codec8 uses."""
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def build_io_block(id_map: dict, width_bytes: int) -> bytes:
    """N (count byte) followed by N * (io_id, value) pairs for one width group."""
    fmt = {1: "B", 2: "H", 4: "I", 8: "Q"}[width_bytes]
    out = struct.pack(">B", len(id_map))
    for io_id, gen in id_map.items():
        out += struct.pack(">B", io_id)
        out += struct.pack(">" + fmt, gen())
    return out


def build_record(lat: float, lon: float, speed: int, altitude: int, angle: int,
                  extra_io_1b: dict = None, event_io_id: int = None) -> bytes:
    ts_ms = int(time.time() * 1000)
    priority = 1

    rec = struct.pack(">Q", ts_ms)
    rec += struct.pack(">B", priority)
    rec += struct.pack(">i", int(lon * 10_000_000))
    rec += struct.pack(">i", int(lat * 10_000_000))
    rec += struct.pack(">h", altitude)
    rec += struct.pack(">H", angle)
    rec += struct.pack(">B", random.randint(4, 12))   # satellites
    rec += struct.pack(">H", speed)

    # extra_io_1b holds any fired motion-event IDs for this record (all 1-byte)
    extra_io_1b = extra_io_1b or {}
    io_1b_combined = {**IO_1B, **{k: (lambda v=v: v) for k, v in extra_io_1b.items()}}

    total_io = len(io_1b_combined) + len(IO_2B) + len(IO_4B) + len(IO_8B)
    # event_io_id: whichever motion event fired triggered this record;
    # otherwise fall back to the fake "fires on IO #1" default
    rec += struct.pack(">B", event_io_id if event_io_id is not None else 1)
    rec += struct.pack(">B", total_io)   # n_total_io

    rec += build_io_block(io_1b_combined, 1)
    rec += build_io_block(IO_2B, 2)
    rec += build_io_block(IO_4B, 4)
    rec += build_io_block(IO_8B, 8)

    return rec


def build_avl_packet(records: list) -> bytes:
    """Wraps AVL records into a full Codec8 TCP frame: preamble+len+data+crc."""
    data = struct.pack(">B", CODEC_ID)
    data += struct.pack(">B", len(records))
    for rec in records:
        data += rec
    data += struct.pack(">B", len(records))  # num_data_2, must match num_data_1

    crc = crc16_ibm(data)
    packet = struct.pack(">I", 0)             # preamble
    packet += struct.pack(">I", len(data))    # data field length
    packet += data
    packet += struct.pack(">I", crc)          # CRC is a 4-byte field, value in low 2 bytes
    return packet


def random_walk(lat: float, lon: float, step: float = 0.0006):
    lat += random.uniform(-step, step)
    lon += random.uniform(-step, step)
    return lat, lon


def send_imei(sock: socket.socket, imei: str):
    imei_bytes = imei.encode("ascii")
    sock.sendall(struct.pack(">H", len(imei_bytes)) + imei_bytes)
    ack = sock.recv(1)
    if ack != b"\x01":
        raise RuntimeError(f"Server rejected IMEI handshake (got {ack!r})")
    print(f"[+] IMEI {imei} accepted by server")


def run(host: str, port: int, imei: str, count: int, interval: float,
        records_per_packet: int, start_lat: float, start_lon: float,
        motion_events: bool = False):
    lat, lon = start_lat, start_lon
    ignition = 1  # tracked across records so motion-event conditions make sense

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((host, port))
        send_imei(sock, imei)

        for i in range(count):
            records = []
            fired_ids = []
            for _ in range(records_per_packet):
                lat, lon = random_walk(lat, lon)
                speed = random.randint(0, 90)
                altitude = random.randint(0, 120)
                angle = random.randint(0, 359)

                extra_io, event_id = ({}, None)
                if motion_events:
                    # occasionally flip ignition too, so towing/idling conditions occur
                    if random.random() < 0.03:
                        ignition = 1 - ignition
                    extra_io, event_id = maybe_motion_event(ignition, speed)
                    if event_id is not None:
                        fired_ids.append(event_id)
                    extra_io[239] = ignition  # keep ignition IO consistent with state
                    # avoid double-adding 239 via IO_1B's own random generator
                    extra_io = {**{k: v for k, v in extra_io.items()}}

                records.append(build_record(lat, lon, speed, altitude, angle,
                                             extra_io_1b=extra_io, event_io_id=event_id))

            packet = build_avl_packet(records)
            sock.sendall(packet)

            resp = sock.recv(4)
            acked = struct.unpack(">I", resp)[0] if len(resp) == 4 else None
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            events_note = f" | motion events fired: {fired_ids}" if fired_ids else ""
            print(f"[{ts}] sent packet {i + 1}/{count} "
                  f"({records_per_packet} records, lat={lat:.6f} lon={lon:.6f}) "
                  f"-> server acked {acked} records{events_note}")

            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Codec8 dummy AVL sender (device emulator)")
    parser.add_argument("--host", default="127.0.0.1", help="Listener host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5027, help="Listener port (default: 5027)")
    parser.add_argument("--imei", default="123456789012345", help="Fake IMEI to send (15 digits)")
    parser.add_argument("--count", type=int, default=10, help="Number of packets to send")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between packets")
    parser.add_argument("--records-per-packet", type=int, default=1,
                         help="AVL records bundled per packet")
    parser.add_argument("--lat", type=float, default=23.8103, help="Starting latitude (default: Dhaka)")
    parser.add_argument("--lon", type=float, default=90.4125, help="Starting longitude (default: Dhaka)")
    parser.add_argument("--motion-events", action="store_true",
                         help="Randomly emulate accelerometer/motion IO IDs "
                              "(246 towing, 247 crash, 251 idling, 252 unplug, "
                              "253/254 green driving). Off by default. Note: IO 257 "
                              "(crash trace) is never emulated -- it's variable-length "
                              "data that needs Codec8 8 Extended framing.")
    args = parser.parse_args()

    print(f"[*] Connecting to {args.host}:{args.port} as IMEI {args.imei}")
    run(args.host, args.port, args.imei, args.count, args.interval,
        args.records_per_packet, args.lat, args.lon, motion_events=args.motion_events)
    print("[*] Done.")


if __name__ == "__main__":
    main()
