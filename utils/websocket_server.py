import eventlet
import socketio
import os
from flask import Flask, jsonify
import jwt
import time
import logging
import json

# Set up logging
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('websocket_server')

# Initialize Flask app and Socket.IO server
app = Flask(__name__)
sio = socketio.Server(cors_allowed_origins='*', logger=True, engineio_logger=True, ping_timeout=60, ping_interval=25)
app = socketio.WSGIApp(sio, app)

# Track connected clients - store both PC ID and connection status
clients = {}
pc_token_map = {}  # Map PC IDs to their tokens for verification

# JWT secret key - should be set via environment variable in production
JWT_SECRET = os.getenv('JWT_SECRET', 'your-secret-key')
 
def verify_token(token):
    """
    Verify JWT token and return pc_id if valid
    
    Args:
        token: JWT token string
    
    Returns:
        dict: Payload if token is valid, None otherwise
    """
    try:
        # Decode and verify the token
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        
        # Check if token is expired
        if 'exp' in payload and payload['exp'] < time.time():
            logger.warning(f"Token expired: {payload}")
            return None
            
        logger.info(f"Token verified for pc_id: {payload.get('pc_id')}")
        return payload
    except jwt.InvalidTokenError as e:
        logger.error(f"Invalid token: {str(e)}")
        return None

@sio.event
def connect(sid, environ):
    logger.info(f'Client connected: {sid}')
    sio.save_session(sid, {'connected': True})

@sio.event
def register(sid, data):
    """
    Register a client with their PC ID and auth token
    
    Args:
        sid: Socket ID
        data: Dictionary containing pc_id and auth_token
    """
    logger.info(f'Registration attempt: {data}')
    pc_id = data.get('pc_id')
    auth_token = data.get('auth_token')
    
    if not pc_id or not auth_token:
        logger.warning(f'Missing pc_id or auth_token in registration: {data}')
        sio.emit('error', {'message': 'Missing pc_id or auth_token'}, room=sid)
        return
    
    # Verify the token
    payload = verify_token(auth_token)
    if not payload:
        logger.warning(f'Invalid token for pc_id: {pc_id}')
        sio.emit('error', {'message': 'Invalid authentication token'}, room=sid)
        return
    
    token_pc_id = payload.get('pc_id')
    
    # Check if this is a sender or a PC app
    is_sender = pc_id.startswith('sender_')
    
    if is_sender:
        # For senders, store the original PC ID from the token for later verification
        session_data = {'is_sender': True, 'token_pc_id': token_pc_id, 'sender_id': pc_id}
        sio.save_session(sid, session_data)
        logger.info(f'Sender {sid} (sender_id: {pc_id}) registered with token for PC {token_pc_id}')
        sio.emit('message', {'status': 'success', 'message': 'Sender registration successful'}, room=sid)
    else:
        # For PC apps, verify the PC ID matches the token
        if token_pc_id != pc_id:
            logger.warning(f'Token pc_id mismatch: token={token_pc_id}, requested={pc_id}')
            sio.emit('warning', {'message': 'PC ID mismatch in token'}, room=sid)
            # Use the PC ID from the token for consistency
            pc_id = token_pc_id
        
        # Store the PC's auth token for later verification
        pc_token_map[pc_id] = auth_token
    
        # If this PC is already registered, update its sid
        if pc_id in clients:
            old_sid = clients[pc_id]
            logger.info(f'PC {pc_id} already registered with sid {old_sid}, updating to {sid}')
            # Try to disconnect the old session
            try:
                sio.disconnect(old_sid)
            except Exception as e:
                logger.warning(f"Failed to disconnect old session {old_sid}: {e}")
    
        clients[pc_id] = sid
        session_data = {'pc_id': pc_id, 'auth_token': auth_token, 'is_sender': False}
        sio.save_session(sid, session_data)
        
        logger.info(f'Client {sid} registered as {pc_id}')
    
        # Notify everyone about connected clients
        broadcast_clients()
        
        # Send confirmation back to the client
        sio.emit('message', {'status': 'success', 'message': 'Registration successful'}, room=sid)

def broadcast_clients():
    """Broadcast the list of connected clients to all clients"""
    client_list = list(clients.keys())
    logger.info(f'Broadcasting connected clients: {client_list}')
    sio.emit('clients_update', client_list)

@sio.event
def message(sid, data):
    """
    Handle incoming messages and route them to specific PCs
    
    Args:
        sid: Socket ID
        data: Dictionary containing message data
    """
    try:
        # Store key data from the message before attempting to access session
        # This ensures we can still process the message if the session is gone
        msg_type = data.get('type')
        target_id = data.get('targetId')
        content = data.get('content')
        
        # Log raw incoming message to help with debugging
        logger.info(f'Raw message from {sid}: type={msg_type}, target={target_id}')
        
        # Try to get session, but handle the case where session might be gone
        try:
            session = sio.get_session(sid)
            is_sender = session.get('is_sender', False)
            
            if is_sender:
                # This is a message from a sender
                sender_id = session.get('sender_id', 'unknown')
                token_pc_id = session.get('token_pc_id', None)
                
                logger.info(f'Message from sender {sender_id} to {target_id}, type: {msg_type}')
                
                if not target_id:
                    logger.warning(f'Missing targetId in message from {sender_id}: {data}')
                    sio.emit('error', {'message': 'Missing targetId'}, room=sid)
                    return
                    
                # Verify that the sender is allowed to send to this target
                if token_pc_id != target_id:
                    logger.warning(f'Sender {sender_id} with token for {token_pc_id} tried to send to {target_id}')
                    sio.emit('error', {'message': 'You are not authorized to send to this target'}, room=sid)
                    return
            else:
                # This is a message from a PC app, not a sender
                pc_id = session.get('pc_id', 'unknown')
                logger.info(f'Received message from PC {pc_id}: {data}')
                
                # Handle any PC-to-PC communication or other message types here
                if isinstance(data, dict) and 'status' in data:
                    # This is a status update
                    logger.info(f"Status update from PC {pc_id}: {data['status']}")
                    return  # No further processing needed for status updates
                
                # For PC-to-PC messaging, we'd need the targetId
                if not target_id:
                    return
                
                # For simplicity, assume PC is authorized to send to the target
                sender_id = pc_id
        except Exception as e:
            # Session not found - this can happen if client disconnected
            logger.warning(f'Could not get session for {sid}, but continuing message processing: {e}')
            # For safety, assume this was a sender and continue with message processing
            # We'll rely on the message data itself without session validation
            sender_id = f"unknown_{sid}"

        # From this point forward, we don't need the session anymore
        # Process the message based on the data we have
        
        # Only process messages with target and type
        if not target_id or not msg_type:
            logger.warning(f'Invalid message format: missing targetId or type: {data}')
            return
            
        target_sid = clients.get(target_id)
        if target_sid:
            if msg_type == 'config':
                # Handle configuration message
                try:
                    logger.info(f'Routing config to {target_id}')
                    
                    # Log content type and sample
                    content_type = type(content).__name__
                    content_sample = str(content)[:100] + '...' if len(str(content)) > 100 else str(content)
                    logger.debug(f'Content type: {content_type}, sample: {content_sample}')
                    
                    # Try to send acknowledgment back to sender if they're still connected
                    try:
                        logger.info(f'Sending acknowledgment to sender')
                        sio.emit('message', {
                            'status': 'success',
                            'message': 'Configuration received and forwarded'
                        }, room=sid)
                    except Exception as ack_err:
                        logger.warning(f'Failed to send acknowledgment: {ack_err}')
                    
                    # Forward configuration to target PC - this is the critical part
                    logger.info(f'Forwarding config to {target_id} (sid: {target_sid})')
                    sio.emit('config', {
                        'from': sender_id,
                        'content': content
                    }, room=target_sid)
                    
                    logger.info(f'Config forwarded successfully to {target_id}')
                except Exception as e:
                    logger.error(f'Error processing config message: {str(e)}')
                    # Try to send error to sender if they're still connected
                    try:
                        sio.emit('error', {
                            'message': f'Error processing configuration: {str(e)}'
                        }, room=sid)
                    except:
                        pass
            else:
                # Handle other message types
                logger.info(f'Routing regular message to {target_id}')
                sio.emit('message', {
                    'from': sender_id,
                    'type': msg_type,
                    'content': content
                }, room=target_sid)
        else:
            # Target not connected, try to send error back to sender
            logger.warning(f'Target {target_id} not connected, message from {sender_id}')
            try:
                sio.emit('error', {
                    'message': f'Client {target_id} not connected',
                    'originalMessage': data
                }, room=sid)
            except:
                pass
    except Exception as e:
        logger.error(f'Error handling message: {str(e)}')
        import traceback
        logger.error(traceback.format_exc())
        # Try to send an error response if possible
        try:
            sio.emit('error', {'message': f'Server error: {str(e)}'}, room=sid)
        except:
            pass

@sio.event
def disconnect(sid):
    try:
        session = sio.get_session(sid)
        pc_id = session.get('pc_id')
        
        if pc_id and pc_id in clients and clients[pc_id] == sid:
            logger.info(f'Client disconnected: {pc_id} (sid: {sid})')
            del clients[pc_id]
            # Notify everyone about disconnection
            broadcast_clients()
        else:
            is_sender = session.get('is_sender', False)
            if is_sender:
                sender_id = session.get('sender_id', 'unknown')
                logger.info(f'Sender disconnected: {sender_id} (sid: {sid})')
            else:
                logger.info(f'Unregistered client disconnected: {sid}')
    except Exception as e:
        logger.warning(f'Error during disconnect event: {e}')
        logger.info(f'Unidentified client disconnected: {sid}')

# Health check endpoint
@app.wsgi_app.route('/')
def health_check():
    return 'Socket.IO server is running'

# List connected clients endpoint - with detailed info
@app.wsgi_app.route('/clients')
def list_clients():
    return jsonify({
        'clients': list(clients.keys()),
        'client_count': len(clients),
        'server_status': 'running'
    })

if __name__ == '__main__':
    # Get port from environment variable (for AWS compatibility)
    port = int(os.environ.get('PORT', 8080))
    
    logger.info(f'Starting Socket.IO server on port {port}')
    eventlet.wsgi.server(eventlet.listen(('0.0.0.0', port)), app)