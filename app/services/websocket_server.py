"""
WebSocket server for real-time communication with PC applications.

This Socket.IO server handles:
- PC client registration and authentication
- Configuration deployment to specific PCs
- Online/offline status tracking
- Message routing between portal and PCs
"""
import socketio
import eventlet
import logging
from typing import Dict, Any
import jwt
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)

# Create Socket.IO server
sio = socketio.Server(
    async_mode='eventlet',
    cors_allowed_origins='*',
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25
)

# Create WSGI app
app = socketio.WSGIApp(sio)

# PC client tracking: {pc_id: sid}
connected_pcs: Dict[str, str] = {}

# Session data tracking: {sid: {pc_id, name, connected_at}}
session_data: Dict[str, Dict[str, Any]] = {}


def validate_token(auth_token: str) -> Dict[str, Any]:
    """
    Validate JWT token and extract payload.

    Args:
        auth_token: JWT token string

    Returns:
        Token payload dict

    Raises:
        jwt.InvalidTokenError: If token is invalid or expired
    """
    try:
        payload = jwt.decode(
            auth_token,
            settings.JWT_SECRET,
            algorithms=[settings.ALGORITHM]
        )
        return payload

    except jwt.ExpiredSignatureError:
        logger.error("Token has expired")
        raise

    except jwt.InvalidTokenError as e:
        logger.error(f"Invalid token: {e}")
        raise


@sio.event
def connect(sid, environ):
    """
    Handle new client connection.

    Args:
        sid: Session ID
        environ: WSGI environment dict
    """
    logger.info(f"Client connected: {sid}")

    # Initialize session data
    session_data[sid] = {
        'pc_id': None,
        'name': None,
        'connected_at': datetime.utcnow().isoformat()
    }


@sio.event
def disconnect(sid):
    """
    Handle client disconnection.

    Args:
        sid: Session ID
    """
    logger.info(f"Client disconnected: {sid}")

    # Get PC ID before removing
    pc_id = None
    if sid in session_data:
        pc_id = session_data[sid].get('pc_id')
        del session_data[sid]

    # Remove from connected PCs
    if pc_id and pc_id in connected_pcs:
        if connected_pcs[pc_id] == sid:
            del connected_pcs[pc_id]
            logger.info(f"PC {pc_id} removed from connected list")

            # Broadcast updated client list
            broadcast_client_list()


@sio.event
def register(sid, data):
    """
    Register PC client with authentication.

    Expected data format:
        {
            'pc_id': str,
            'auth_token': str,
            'name': str (optional)
        }

    Args:
        sid: Session ID
        data: Registration data dict
    """
    try:
        pc_id = data.get('pc_id')
        auth_token = data.get('auth_token')
        name = data.get('name', '')

        if not pc_id or not auth_token:
            logger.error(f"Registration failed for {sid}: missing pc_id or auth_token")
            sio.emit('error', {
                'message': 'Missing pc_id or auth_token'
            }, room=sid)
            return

        # Validate JWT token
        try:
            payload = validate_token(auth_token)
            token_pc_id = payload.get('pc_id')

            # Verify PC ID matches token
            if token_pc_id != pc_id:
                logger.warning(
                    f"PC ID mismatch: provided={pc_id}, token={token_pc_id}"
                )
                # Use token's PC ID as authoritative
                pc_id = token_pc_id

        except jwt.InvalidTokenError as e:
            logger.error(f"Token validation failed for {sid}: {e}")
            sio.emit('error', {
                'message': 'Invalid or expired authentication token'
            }, room=sid)
            return

        # Register PC
        connected_pcs[pc_id] = sid
        session_data[sid]['pc_id'] = pc_id
        session_data[sid]['name'] = name

        logger.info(f"PC registered: {pc_id} (sid: {sid}, name: {name})")

        # Confirm registration to client
        sio.emit('registered', {
            'pc_id': pc_id,
            'message': 'Successfully registered'
        }, room=sid)

        # Broadcast updated client list
        broadcast_client_list()

    except Exception as e:
        logger.error(f"Error during registration for {sid}: {e}")
        sio.emit('error', {
            'message': f'Registration failed: {str(e)}'
        }, room=sid)


@sio.event
def message(sid, data):
    """
    Handle message routing to target PC.

    Expected data format:
        {
            'type': str (e.g., 'config', 'command'),
            'targetId': str (PC ID),
            'content': dict (message content)
        }

    Args:
        sid: Session ID
        data: Message data dict
    """
    try:
        message_type = data.get('type')
        target_id = data.get('targetId')
        content = data.get('content')

        if not target_id:
            logger.error(f"Message from {sid} missing targetId")
            sio.emit('error', {
                'message': 'Missing targetId'
            }, room=sid)
            return

        # Check if target PC is connected
        if target_id not in connected_pcs:
            logger.warning(f"Target PC {target_id} is not connected")
            sio.emit('error', {
                'message': f'Target PC {target_id} is not online'
            }, room=sid)
            return

        target_sid = connected_pcs[target_id]

        logger.info(
            f"Routing {message_type} message from {sid} to PC {target_id} (sid: {target_sid})"
        )

        # Forward message to target PC
        sio.emit(message_type or 'message', {
            'type': message_type,
            'content': content,
            'from': session_data.get(sid, {}).get('pc_id', 'unknown')
        }, room=target_sid)

        # Send acknowledgment to sender
        sio.emit('message_sent', {
            'targetId': target_id,
            'status': 'delivered'
        }, room=sid)

    except Exception as e:
        logger.error(f"Error routing message from {sid}: {e}")
        sio.emit('error', {
            'message': f'Message routing failed: {str(e)}'
        }, room=sid)


@sio.event
def get_clients(sid):
    """
    Request list of connected clients.

    Args:
        sid: Session ID
    """
    try:
        clients = []

        for pc_id, pc_sid in connected_pcs.items():
            client_data = session_data.get(pc_sid, {})
            clients.append({
                'pc_id': pc_id,
                'name': client_data.get('name', ''),
                'connected_at': client_data.get('connected_at')
            })

        sio.emit('clients_list', {
            'clients': clients
        }, room=sid)

    except Exception as e:
        logger.error(f"Error getting client list for {sid}: {e}")
        sio.emit('error', {
            'message': f'Failed to get client list: {str(e)}'
        }, room=sid)


def broadcast_client_list():
    """
    Broadcast updated client list to all connected clients.
    """
    try:
        clients = []

        for pc_id, pc_sid in connected_pcs.items():
            client_data = session_data.get(pc_sid, {})
            clients.append({
                'pc_id': pc_id,
                'name': client_data.get('name', ''),
                'connected_at': client_data.get('connected_at')
            })

        sio.emit('clients_update', {
            'clients': clients
        })

        logger.debug(f"Broadcasted client list: {len(clients)} clients")

    except Exception as e:
        logger.error(f"Error broadcasting client list: {e}")


def run_server(host: str = '0.0.0.0', port: int = 8080):
    """
    Run the WebSocket server.

    Args:
        host: Host to bind to
        port: Port to listen on
    """
    logger.info(f"Starting WebSocket server on {host}:{port}")

    try:
        eventlet.wsgi.server(
            eventlet.listen((host, port)),
            app,
            log_output=True
        )
    except Exception as e:
        logger.error(f"WebSocket server error: {e}")
        raise


if __name__ == '__main__':
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Run server
    run_server(
        host=settings.WEBSOCKET_HOST,
        port=settings.WEBSOCKET_PORT
    )
