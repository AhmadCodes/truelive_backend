"""
A small fake Socket.IO relay for backend tests (the backend's counterpart of
the Client's D3 fake relay): ``socketio.AsyncServer(async_mode="aiohttp")`` on
127.0.0.1 with an ephemeral port, run on its own event loop in a daemon
thread.

It serves the sender event ``deploy_config`` the way the real relay answers
it, after ``ack_delay_s`` (D14's ``test_health_responsive_during_deploy``),
with ``answer`` = delivered (default), offline or error, or ``silent`` (no
answer, the caller times out). Every request is recorded in ``requests``;
``first_request`` is set when the first one arrives. ``stop()`` shuts the
server and its thread down.
"""

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional

import socketio
from aiohttp import web


class FakeRelay:
    def __init__(self, ack_delay_s: float = 0.0, answer: str = "delivered"):
        self.ack_delay_s = ack_delay_s
        self.answer = answer
        self.requests: List[Dict[str, Any]] = []
        self.request_times: List[float] = []
        self.connects: List[Optional[dict]] = []
        self.first_request = threading.Event()
        self.port: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-relay", daemon=True)

        self.sio = socketio.AsyncServer(async_mode="aiohttp", logger=False, engineio_logger=False)
        self.sio.on("connect", self._on_connect)
        self.sio.on("deploy_config", self._on_deploy_config)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeRelay":
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("fake relay did not start")
        return self

    async def _on_connect(self, sid, environ, auth=None):
        self.connects.append(auth)

    async def _on_deploy_config(self, sid, data):
        self.requests.append(data)
        self.request_times.append(time.monotonic())
        self.first_request.set()
        if self.ack_delay_s:
            await asyncio.sleep(self.ack_delay_s)
        if self.answer == "silent":
            await asyncio.sleep(3600)
        config_id = data.get("config_id") if isinstance(data, dict) else None
        return {"status": self.answer, "config_id": config_id, "confirmable": False}

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        app = web.Application()
        self.sio.attach(app)

        async def _start():
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0, shutdown_timeout=1.0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self._ready.set()

        loop.run_until_complete(_start())
        try:
            loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._stopped.set()

    async def _shutdown(self):
        if self._runner is not None:
            await self._runner.cleanup()

    def stop(self):
        if self._loop is not None and self._thread.is_alive():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("fake relay thread did not stop")
