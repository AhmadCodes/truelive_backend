"""
WebSocket client for sending configurations to PC applications.

This is the CLIENT side - used by the portal to send configs to PCs.
The server side is in websocket_server.py.
"""
import socketio
import logging
import jwt
import uuid
from typing import Dict, Any, Optional
import time

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_config_sync(
    config: Dict[str, Any],
    pc_id: str,
    auth_token: str,
    timeout: int = 10
) -> bool:
    """
    Send configuration to PC application via WebSocket.

    This function:
    1. Validates the JWT token
    2. Connects to the WebSocket server
    3. Registers as a sender
    4. Sends the configuration message
    5. Waits for acknowledgment

    Args:
        config: Generated configuration dict
        pc_id: Target PC identifier
        auth_token: JWT authentication token
        timeout: Timeout in seconds (default: 10)

    Returns:
        True if configuration was successfully sent and acknowledged, False otherwise
    """
    sio = None

    try:
        # Validate JWT token
        try:
            payload = jwt.decode(
                auth_token,
                settings.JWT_SECRET,
                algorithms=[settings.ALGORITHM]
            )
            token_pc_id = payload.get('pc_id')

            if token_pc_id != pc_id:
                logger.warning(
                    f"Token PC ID ({token_pc_id}) doesn't match target PC ID ({pc_id})"
                )
                # Use token's PC ID as authoritative
                pc_id = token_pc_id

        except jwt.ExpiredSignatureError:
            logger.error("JWT token has expired")
            return False

        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid JWT token: {e}")
            return False

        # Create Socket.IO client
        sio = socketio.Client(
            logger=False,
            engineio_logger=False,
            reconnection=False
        )

        # Connection tracking
        connected = False
        registered = False
        clients_received = False
        config_sent = False
        acknowledged = False

        # Event handlers
        @sio.event
        def connect():
            nonlocal connected
            connected = True
            logger.info("Connected to WebSocket server")

        @sio.event
        def disconnect():
            logger.info("Disconnected from WebSocket server")

        @sio.event
        def clients_update(data):
            nonlocal clients_received
            clients_received = True
            logger.debug(f"Received client list update: {data}")

        @sio.event
        def message_sent(data):
            nonlocal acknowledged
            acknowledged = True
            logger.info(f"Configuration acknowledged: {data}")

        @sio.event
        def error(data):
            logger.error(f"WebSocket error: {data}")

        # Connect to server
        logger.info(f"Connecting to WebSocket server at {settings.WEBSOCKET_URL}")

        try:
            sio.connect(
                settings.WEBSOCKET_URL,
                wait_timeout=timeout
            )
        except Exception as e:
            logger.error(f"Failed to connect to WebSocket server: {e}")
            return False

        if not connected:
            logger.error("Failed to establish WebSocket connection")
            return False

        # Generate unique sender ID
        sender_id = f"sender_{uuid.uuid4().hex[:8]}"

        # Register as sender (not as a PC)
        logger.info(f"Registering as sender: {sender_id}")

        try:
            # For sender, we don't register like a PC does
            # We just wait a moment for any client list updates
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"Error during registration: {e}")
            return False

        # Send configuration message
        logger.info(f"Sending configuration to PC {pc_id}")

        message_data = {
            'type': 'config',
            'targetId': pc_id,
            'content': config
        }

        try:
            sio.emit('message', message_data)
            config_sent = True
            logger.info("Configuration message sent")

        except Exception as e:
            logger.error(f"Failed to send configuration: {e}")
            return False

        # Wait for acknowledgment
        start_time = time.time()
        while time.time() - start_time < timeout:
            if acknowledged:
                logger.info("Configuration delivery confirmed")
                return True

            time.sleep(0.1)

        logger.warning(
            f"Timeout waiting for acknowledgment from PC {pc_id}"
        )
        return False

    except Exception as e:
        logger.error(f"Error in send_config_sync: {e}")
        return False

    finally:
        # Disconnect
        if sio and sio.connected:
            try:
                sio.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting: {e}")


async def send_config_async(
    config: Dict[str, Any],
    pc_id: str,
    auth_token: str,
    timeout: int = 10
) -> bool:
    """
    Async version of send_config_sync.

    This wraps the synchronous Socket.IO client in an async function.

    Args:
        config: Generated configuration dict
        pc_id: Target PC identifier
        auth_token: JWT authentication token
        timeout: Timeout in seconds (default: 10)

    Returns:
        True if successful, False otherwise
    """
    import asyncio

    # Run sync function in thread pool
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        send_config_sync,
        config,
        pc_id,
        auth_token,
        timeout
    )

    return result
