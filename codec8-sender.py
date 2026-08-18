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


def build_record(lat: float, lon: float, speed: int, altitude: int, angle: int) -> bytes:
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

    total_io = len(IO_1B) + len(IO_2B) + len(IO_4B) + len(IO_8B)
    rec += struct.pack(">B", 1)          # event_io_id (fake: fires on IO #1)
    rec += struct.pack(">B", total_io)   # n_total_io

    rec += build_io_block(IO_1B, 1)
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
        records_per_packet: int, start_lat: float, start_lon: float):
    lat, lon = start_lat, start_lon

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((host, port))
        send_imei(sock, imei)

        for i in range(count):
            records = []
            for _ in range(records_per_packet):
                lat, lon = random_walk(lat, lon)
                speed = random.randint(0, 90)
                altitude = random.randint(0, 120)
                angle = random.randint(0, 359)
                records.append(build_record(lat, lon, speed, altitude, angle))

            packet = build_avl_packet(records)
            sock.sendall(packet)

            resp = sock.recv(4)
            acked = struct.unpack(">I", resp)[0] if len(resp) == 4 else None
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] sent packet {i + 1}/{count} "
                  f"({records_per_packet} records, lat={lat:.6f} lon={lon:.6f}) "
                  f"-> server acked {acked} records")

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
    args = parser.parse_args()

    print(f"[*] Connecting to {args.host}:{args.port} as IMEI {args.imei}")
    run(args.host, args.port, args.imei, args.count, args.interval,
        args.records_per_packet, args.lat, args.lon)
    print("[*] Done.")


if __name__ == "__main__":
    main()
