"""
A minimal Redis (RESP2) server for tests that need a real Redis client over
TCP (plan 06 D17: the catch-up rate-limit key) on a machine without Redis.

Binds 127.0.0.1 on an ephemeral port, one thread per connection. Commands:
PING, ECHO, SET (NX, XX, EX, PX), GET, DEL, EXISTS, TTL, PTTL, EXPIRE, KEYS
(``*`` and ``prefix*``), FLUSHALL, FLUSHDB, SELECT, CLIENT, INFO, QUIT. Any
other command answers ``-ERR``. ``commands`` records every command name and
first argument, so a test can tell whether Redis was touched at all.

It is not a broker: Celery tests use kombu's SQLAlchemy transport instead.
"""

import socket
import threading
import time
from typing import Dict, List, Optional, Tuple


class FakeRedis:
    def __init__(self):
        self.data: Dict[bytes, Tuple[bytes, Optional[float]]] = {}
        self.commands: List[Tuple[str, Optional[str]]] = []
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.url = f"redis://127.0.0.1:{self.port}/0"
        self._stopped = threading.Event()
        self._conns: List[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    # -- server ---------------------------------------------------------------

    def _accept(self):
        while not self._stopped.is_set():
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        buf = b""
        try:
            while not self._stopped.is_set():
                parsed = self._parse(buf)
                if parsed is None:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    continue
                args, buf = parsed
                if not args:
                    continue
                reply, close = self._dispatch(args)
                conn.sendall(reply)
                if close:
                    return
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _parse(buf: bytes):
        """``(args, rest)`` for one complete command in ``buf``, else None."""
        if not buf:
            return None
        if buf[:1] != b"*":  # inline command
            if b"\r\n" not in buf:
                return None
            line, rest = buf.split(b"\r\n", 1)
            return line.split(), rest
        if b"\r\n" not in buf:
            return None
        head, rest = buf.split(b"\r\n", 1)
        count = int(head[1:])
        args = []
        for _ in range(count):
            if b"\r\n" not in rest:
                return None
            size_line, rest = rest.split(b"\r\n", 1)
            size = int(size_line[1:])
            if len(rest) < size + 2:
                return None
            args.append(rest[:size])
            rest = rest[size + 2:]
        return args, rest

    # -- commands -------------------------------------------------------------

    @staticmethod
    def _bulk(value: Optional[bytes]) -> bytes:
        if value is None:
            return b"$-1\r\n"
        return b"$%d\r\n%s\r\n" % (len(value), value)

    @staticmethod
    def _int(value: int) -> bytes:
        return b":%d\r\n" % value

    def _alive(self, key: bytes) -> Optional[bytes]:
        item = self.data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and time.monotonic() >= expires:
            del self.data[key]
            return None
        return value

    def _dispatch(self, args: List[bytes]):
        name = args[0].decode("ascii", "replace").upper()
        first = args[1].decode("utf-8", "replace") if len(args) > 1 else None
        with self.lock:
            self.commands.append((name, first))
            if name == "PING":
                return b"+PONG\r\n", False
            if name == "ECHO":
                return self._bulk(args[1]), False
            if name in ("SELECT", "CLIENT", "FLUSHALL", "FLUSHDB"):
                if name in ("FLUSHALL", "FLUSHDB"):
                    self.data.clear()
                return b"+OK\r\n", False
            if name == "QUIT":
                return b"+OK\r\n", True
            if name == "INFO":
                return self._bulk(b"# Server\r\nredis_version:7.0.0-fake\r\n"), False
            if name == "SET":
                key, value, opts = args[1], args[2], [a.upper() for a in args[3:]]
                expires = None
                if b"EX" in opts:
                    expires = time.monotonic() + int(opts[opts.index(b"EX") + 1])
                if b"PX" in opts:
                    expires = time.monotonic() + int(opts[opts.index(b"PX") + 1]) / 1000.0
                exists = self._alive(key) is not None
                if (b"NX" in opts and exists) or (b"XX" in opts and not exists):
                    return b"$-1\r\n", False
                self.data[key] = (value, expires)
                return b"+OK\r\n", False
            if name == "GET":
                return self._bulk(self._alive(args[1])), False
            if name == "DEL":
                removed = sum(1 for k in args[1:] if self._alive(k) is not None and self.data.pop(k, None))
                return self._int(removed), False
            if name == "EXISTS":
                return self._int(sum(1 for k in args[1:] if self._alive(k) is not None)), False
            if name in ("TTL", "PTTL"):
                if self._alive(args[1]) is None:
                    return self._int(-2), False
                expires = self.data[args[1]][1]
                if expires is None:
                    return self._int(-1), False
                left = expires - time.monotonic()
                return self._int(int(left) if name == "TTL" else int(left * 1000)), False
            if name == "EXPIRE":
                value = self._alive(args[1])
                if value is None:
                    return self._int(0), False
                self.data[args[1]] = (value, time.monotonic() + int(args[2]))
                return self._int(1), False
            if name == "KEYS":
                pattern = args[1]
                keys = [k for k in list(self.data) if self._alive(k) is not None]
                if pattern != b"*":
                    prefix = pattern.rstrip(b"*")
                    keys = [k for k in keys if k.startswith(prefix)]
                return b"*%d\r\n" % len(keys) + b"".join(self._bulk(k) for k in keys), False
            return b"-ERR unknown command '%s'\r\n" % name.encode(), False

    # -- helpers --------------------------------------------------------------

    def ttl(self, key: str) -> Optional[float]:
        with self.lock:
            if self._alive(key.encode()) is None:
                return None
            expires = self.data[key.encode()][1]
            return None if expires is None else expires - time.monotonic()

    def touched(self) -> bool:
        with self.lock:
            return any(name not in ("PING",) for name, _ in self.commands)

    def stop(self):
        self._stopped.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
