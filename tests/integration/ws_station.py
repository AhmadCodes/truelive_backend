"""
A websocket station for relay integration tests (M2 review).

Run by the Client's Python (python-socketio 5.10 with websocket-client, as
Client 1.1.1 ships it) with ``-I``, so the relay sees its socket close the
moment the process ends, unlike a polling client (whose disconnect the
eventlet relay only notices at the ping timeout).

stdin: one JSON spec line ``{"url": ...}``, then JSON commands:

* ``{"cmd": "emit", "event": e, "data": d}``: emit without an ACK;
* ``{"cmd": "call", "event": e, "data": d, "tag": t, "timeout": s}``: emit
  with an ACK, printed as ``{"event": "ack", "tag": t, "data": ...}``;
* ``{"cmd": "exit"}``: end the process at once (a hard drop: the socket
  closes without a Socket.IO disconnect);
* ``{"cmd": "disconnect"}``: a clean disconnect.

stdout: JSON events (connected with the session id, registered, error,
config with its root config_id and size, ack, disconnected). The ``config``
handler returns nothing, so the relay gets the plain ACK Client 1.1.x sends.
Nothing printed ever holds a token or a layout.
"""

import json
import os
import sys

import socketio


def out(**event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    spec = json.loads(sys.stdin.readline())
    sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
    sio.on("registered", lambda data: out(event="registered", features=(data or {}).get("features")))
    sio.on("error", lambda data: out(event="error", message=(data or {}).get("message")))
    sio.on("disconnect", lambda: out(event="disconnected"))

    def on_config(data):
        content = (data or {}).get("content") or {}
        out(event="config", config_id=content.get("config_id"), size=len(json.dumps(data)))

    sio.on("config", on_config)
    sio.connect(spec["url"], transports=["websocket"], wait_timeout=10)
    out(event="connected", sid=sio.get_sid())
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["cmd"] == "emit":
            sio.emit(cmd["event"], cmd.get("data"))
            out(event="emitted", name=cmd["event"])
        elif cmd["cmd"] == "call":
            try:
                ack = sio.call(cmd["event"], cmd.get("data"), timeout=cmd.get("timeout", 5))
                out(event="ack", tag=cmd.get("tag"), data=ack)
            except Exception as exc:
                out(event="ack_error", tag=cmd.get("tag"), error=type(exc).__name__)
        elif cmd["cmd"] == "exit":
            os._exit(0)
        elif cmd["cmd"] == "disconnect":
            sio.disconnect()
            out(event="closed")
    os._exit(0)


if __name__ == "__main__":
    main()
