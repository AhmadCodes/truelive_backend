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


class WsgiFakeRelay:
    """The same sender-side fake on a threaded WSGI server (python-socketio
    in ``threading`` mode, wsgiref on 127.0.0.1, ephemeral port).

    python-engineio's aiohttp driver reads a polling POST with
    ``StreamReader.read(n)``, which returns only what is buffered, so the
    aiohttp FakeRelay cannot carry a production-sized layout (about 216 KB);
    a WSGI server reads the body exactly, as the real eventlet relay does.
    ``max_http_buffer_size`` defaults to python-engineio's own default
    (1 000 000), like a relay that was never told otherwise.
    """

    def __init__(self, max_http_buffer_size: Optional[int] = None):
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

        kwargs = {} if max_http_buffer_size is None else {"max_http_buffer_size": max_http_buffer_size}
        self.requests: List[Dict[str, Any]] = []
        self.post_statuses: List[int] = []
        self.sio = socketio.Server(async_mode="threading", logger=False, engineio_logger=False, **kwargs)
        self.sio.on("deploy_config", self._on_deploy_config)
        app = socketio.WSGIApp(self.sio)
        statuses = self.post_statuses

        def recording_app(environ, start_response):
            if environ.get("REQUEST_METHOD") != "POST":
                return app(environ, start_response)

            def recording_start(status, headers, exc_info=None):
                statuses.append(int(status.split()[0]))
                return start_response(status, headers, exc_info)

            return app(environ, recording_start)

        class _Server(ThreadingMixIn, WSGIServer):
            daemon_threads = True

        class _QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        self._server = make_server("127.0.0.1", 0, recording_app, server_class=_Server, handler_class=_QuietHandler)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, name="wsgi-fake-relay", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _on_deploy_config(self, sid, data):
        self.requests.append(data)
        config_id = data.get("config_id") if isinstance(data, dict) else None
        return {"status": "delivered", "config_id": config_id, "confirmable": False}

    def start(self) -> "WsgiFakeRelay":
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("WSGI fake relay thread did not stop")
