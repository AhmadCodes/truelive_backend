"""
Run an unmodified Client ConnectionManager as a PC against a local relay.

Used by tests/integration/test_relay_phase0.py (plan 06 D12 "Phase 0
compatibility"). Started with the Client's Python (PySide6, python-socketio
5.10) and ``-I``:

    <client python> -I station_client_driver.py <client source root>

``<client source root>`` holds ``src/`` exported from a Client release commit
(``git archive``), so ConnectionManager is exactly what that release ships.
The first stdin line is JSON ``{"url", "pc_id", "token", "run_seconds"}``; a
later ``quit`` line stops it. It does what the Client app does: connect, and
on ``connected`` call ``register(pc_id, token)``. It prints one JSON line per
observed signal (never the token): connected, registered (ConnectionManager
stores the pc_id from ``registered``), config, error, warning,
clients_update, message, state, stopped. The caller isolates the profile
(USERPROFILE, HOME, APPDATA, LOCALAPPDATA, PROGRAMDATA).
"""

import json
import queue
import sys
import threading
import time


def main() -> int:
    sys.path.insert(0, sys.argv[1])
    cfg = json.loads(sys.stdin.readline())

    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication([])
    from src.core.connection_manager import ConnectionManager

    def out(obj):
        print(json.dumps(obj), flush=True)

    events: "queue.Queue" = queue.Queue()
    commands: "queue.Queue" = queue.Queue()

    def read_commands():
        for line in sys.stdin:
            commands.put(line.strip())
        commands.put("quit")

    threading.Thread(target=read_commands, daemon=True).start()

    manager = ConnectionManager(cfg["url"])
    manager.connected.connect(lambda: events.put(("connected", None)))
    manager.config_received.connect(lambda content: events.put(("config", content)))
    manager.error_occurred.connect(lambda message: events.put(("error", message)))
    manager.warning_received.connect(lambda message: events.put(("warning", message)))
    manager.clients_updated.connect(lambda clients: events.put(("clients_update", len(clients))))
    manager.message_received.connect(lambda data: events.put(("message", data)))
    manager.state_changed.connect(lambda state, _name: events.put(("state", state.value)))

    manager.connect()
    announced_pc_id = None
    deadline = time.monotonic() + float(cfg.get("run_seconds", 120))
    while time.monotonic() < deadline:
        app.processEvents()
        try:
            if commands.get_nowait() == "quit":
                break
        except queue.Empty:
            pass
        if manager.pc_id and manager.pc_id != announced_pc_id:
            announced_pc_id = manager.pc_id
            out({"event": "registered", "value": announced_pc_id})
        try:
            name, value = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if name == "connected":
            out({"event": "connected", "value": bool(manager.register(cfg["pc_id"], cfg["token"]))})
        else:
            out({"event": name, "value": value})

    manager.disconnect()
    out({"event": "stopped", "value": None})
    return 0


if __name__ == "__main__":
    sys.exit(main())
