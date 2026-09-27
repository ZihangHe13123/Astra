"""Native Messaging stdio host; a single connection, no buffering/reconnect/replay."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
from .browser_control_transport import MAX_FRAME, default_endpoint_dir, encode_frame
from .browser_control_storage import check_directory, read_private


def read_descriptor(directory: Path) -> dict:
    check_directory(directory)
    data = json.loads(read_private(directory / 'endpoint.json', 4096))
    if not isinstance(data, dict) or type(data.get('port')) is not int or not 0 < data['port'] < 65536 or not isinstance(data.get('token'), str) or len(data['token']) < 32:
        raise ValueError('Invalid browser control descriptor')
    return data


def read_sync_frame(stream) -> dict:
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            chunk = stream.read(size-len(chunks))
            if not chunk: raise EOFError
            chunks.extend(chunk)
        return chunks
    size = struct.unpack('<I', exact(4))[0]
    if not 0 < size <= MAX_FRAME: raise ValueError('Invalid native frame length')
    value = json.loads(exact(size))
    if not isinstance(value, dict): raise ValueError('Invalid native frame')
    return value


def bridge(directory: Path, input_stream, output_stream) -> None:
    descriptor = read_descriptor(directory)
    with socket.create_connection(('127.0.0.1', descriptor['port']), timeout=3) as connection:
        connection.sendall(encode_frame({'token':descriptor['token']}))
        socket_stream = connection.makefile('rb', buffering=0)
        if read_sync_frame(socket_stream).get('ok') is not True:
            raise PermissionError('Browser control authentication failed')
        connection.settimeout(None)
        # Opening a native port is not proof that an Astra runtime is available.
        # This versioned signal precedes every normal request, without secrets.
        output_stream.write(encode_frame({'type':'astra_control_ready','version':1}))
        output_stream.flush()
        def forward_responses():
            try:
                while True:
                    response = read_sync_frame(input_stream)
                    # Extension only supplies responses, never localhost credentials.
                    if not isinstance(response.get('id'), str) or type(response.get('ok')) is not bool:
                        raise ValueError('Invalid native response')
                    connection.sendall(encode_frame(response))
            except (OSError, EOFError, ValueError):
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        threading.Thread(target=forward_responses, daemon=True).start()
        try:
            while True:
                request = read_sync_frame(socket_stream)
                output_stream.write(encode_frame(request)); output_stream.flush()
        except EOFError:
            pass
        finally:
            socket_stream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--endpoint-dir', type=Path, default=default_endpoint_dir())
    # Chromium supplies its origin as an extra argument; manifests restrict it.
    options, _ = parser.parse_known_args()
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        # A daemon reader owns this unbuffered duplicate for this one-shot host.
        # Closing it from the main thread can deadlock behind a pending Windows
        # CRT read. Process exit closes it after the socket side disconnects.
        input_stream = os.fdopen(os.dup(sys.stdin.fileno()), 'rb', buffering=0)
        bridge(options.endpoint_dir, input_stream, sys.stdout.buffer)
        return 0
    except (OSError, ValueError, EOFError):
        print('Astra browser control unavailable. Start the runtime and reconnect the extension.', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
