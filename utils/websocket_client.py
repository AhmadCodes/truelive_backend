import asyncio
import websockets
import json

class StreamAppClient:
    def __init__(self, uri):
        self.uri = uri
        self.websocket = None

    async def connect(self):
        self.websocket = await websockets.connect(self.uri)

    async def send_site_screen_mapping(self, mapping):
        if not self.websocket:
            await self.connect()
        await self.websocket.send(json.dumps({
            "type": "site_screen_mapping",
            "data": mapping
        }))

    async def send_camera_config(self, config):
        if not self.websocket:
            await self.connect()
        await self.websocket.send(json.dumps({
            "type": "camera_config",
            "data": config
        }))