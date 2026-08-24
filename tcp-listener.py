#!/usr/bin/env python3

# Minimal Teltonika Codec8 TCP listener.
# Handles IMEI handshake & Codec8 AVL data packets
# Parses and prints record to stdout

import json
import socket
import struct
import threading
from datetime import datetime, timezone

HOST = "0.0.0.0"
PORT = 5027

def recv_exact(conn, n):
    buf = bytearray()

    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            if buf:
                raise ConnectionError(
                    f"connection closed mid-message: expected {n} bytes, but got {len(buf)}"
                )
            return None
        buf.extend(chunk)
    return bytes(buf)

def parse_codec8(data: bytes):
    codec_id = data[0]
    num_data_1 = data[1]
    offset = 2
    records = []

    for _ in range(num_data_1):
        timestamp_ms = struct.unpack(">Q", data[offset:offset + 8])[0]
        offset += 8
        priority = data[offset]
        offset += 1

        # GPS 
        longitude = struct.unpack(">i", data[offset:offset + 4])[0] / 10_000_000
        offset += 4
        latitude = struct.unpack(">i", data[offset:offset + 4])[0] / 10_000_000
        offset += 4
        altitude = struct.unpack(">h", data[offset:offset + 2])[0]
        offset += 2
        angle = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
        satellites = data[offset]
        offset += 1
        speed = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2

        event_io_id = data[offset]
        offset += 1
        n_total_io = data[offset]
        offset += 1

        io_values = {}
        for size, label in ((1, "1b"), (2, "2b"), (4, "4b"), (8, "8b")):
            n = data[offset]
            offset += 1
            for _ in range(n):
                io_id = data[offset]
                offset += 1
                val = int.from_bytes(data[offset:offset + size], "big")
                offset += size
                io_values[io_id] = val

        records.append({
            "timestamp": datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc),
            "priority": priority,
            "lat": latitude,
            "lon": longitude,
            "altitude": altitude,
            "angle": angle,
            "satellites": satellites,
            "speed": speed,
            "event_io_id": event_io_id,
            "io": io_values,
        })

    num_data_2 = data[offset]
    return num_data_1, records

def record_to_json(imei: str, record: dict) -> str:
    payload = {**record, "imei": imei}
    return json.dumps(payload, default=_json_default)

def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

def crc16_ibm(data: bytes) -> int:
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF

def handle_client(conn: socket.socket, addr):
    print(f"[+] Connection from {addr}")
    try:
        imei_len_bytes = recv_exact(conn, 2)
        if imei_len_bytes is None:
            conn.close()
            return
        imei_len = struct.unpack(">H", imei_len_bytes)[0]

        imei_bytes = recv_exact(conn, imei_len)
        if imei_bytes is None:
            conn.close()
            return
        imei = imei_bytes.decode("ascii", errors="ignore")
        print(f"    IMEI: {imei}")

        # TODO: check IMEI against a whitelist of known devices prior to accepting
        conn.sendall(b"\x01")

        while True:
            header = recv_exact(conn, 4)
            if header is None:
                break
            preamble = struct.unpack(">I", header)[0]
            if preamble != 0:
                break

            length_bytes = recv_exact(conn, 4)
            if length_bytes is None:
                break
            data_length = struct.unpack(">I", length_bytes)[0]

            data = recv_exact(conn, data_length)
            if data is None:
                break

            crc_bytes = recv_exact(conn, 4)
            if crc_bytes is None:
                break
            received_crc = struct.unpack(">I", crc_bytes)[0] & 0xFFFF

            computed_crc = crc16_ibm(data)
            if computed_crc != received_crc:
                print(f"    [!] [{imei}] CRC mismatch: got {received_crc:#06x}, "
                      f"computed {computed_crc:#06x} -- discarding packet")
                # NAK by acking 0 records so the device knows to resend
                # rather than assuming the corrupted packet was accepted.
                conn.sendall(struct.pack(">I", 0))
                continue

            num_records, records = parse_codec8(data)

            for r in records:
                print(record_to_json(imei, r))
                # TODO: write to db keyed by imei

            conn.sendall(struct.pack(">I", num_records))

    except (ConnectionError, OSError) as e:
        print(f"    [!] Problem with connection {addr}: {e}")
    except Exception as e:
        print(f"    [!] Error with {addr}: {e}")
    finally:
        conn.close()
        print(f"[-] Disconnected {addr}")

def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(5)
    print(f"Listening on {HOST}:{PORT}")

    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    main()
