#!/usr/bin/env python3
"""Use a launchd-owned loopback socket, while running as the ordinary Mac user."""
import ctypes
import select
import socket
import threading

LIMIT = threading.BoundedSemaphore(32)


def relay(client):
    try:
        with client:
            try:
                remote = socket.create_connection(('127.0.0.1', 18320), timeout=5)
            except OSError:
                body = b'Personal Router is reconnecting. Check Tailscale and try again in a moment.\n'
                client.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Type: text/plain\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
                return
            with remote:
                client.settimeout(15);remote.settimeout(15)
                while True:
                    readable, _, _ = select.select([client, remote], [], [], 60)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (remote if source is client else client).sendall(data)
    except (OSError, ValueError):
        pass
    finally:
        LIMIT.release()


def main():
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    lib.launch_activate_socket.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), ctypes.POINTER(ctypes.c_size_t)]
    lib.launch_activate_socket.restype = ctypes.c_int
    lib.free.argtypes = [ctypes.c_void_p]
    descriptors = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t()
    code = lib.launch_activate_socket(b'Listener', ctypes.byref(descriptors), ctypes.byref(count))
    if code != 0 or count.value != 1:
        raise SystemExit('Expected one launchd listener; code=' + str(code))
    fd = descriptors[0]
    lib.free(descriptors)
    listener = socket.socket(fileno=fd)
    listener.setblocking(True)
    if listener.getsockname()[0] != '127.0.0.1':
        raise SystemExit('Refusing a non-loopback listener')
    while True:
        client, _ = listener.accept()
        if LIMIT.acquire(blocking=False):
            threading.Thread(target=relay, args=(client,), daemon=True).start()
        else:
            client.close()


if __name__ == '__main__':
    main()
