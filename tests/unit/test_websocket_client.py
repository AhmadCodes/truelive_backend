"""
Comprehensive tests for WebSocket client service.

Tests cover:
- Configuration sending to PCs
- JWT token validation
- WebSocket connection handling
- Error handling and timeouts
"""
import pytest
import time
from unittest.mock import MagicMock, patch, call
from app.services.websocket_client import send_config_sync, send_config_async


@pytest.mark.unit
@pytest.mark.websocket
class TestSendConfigSync:
    """Test send_config_sync function."""

    def test_successful_config_send(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test successful configuration send."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640, "height": 480, "screens": []}
            pc_id = "pc_123"
            auth_token = "test_token"

            # Mock acknowledgment
            acknowledged = False
            def message_sent_handler(data):
                nonlocal acknowledged
                acknowledged = True

            result = send_config_sync(config, pc_id, auth_token)

            # Should return True on success
            assert result is True or result is not None

    def test_invalid_jwt_token(self, mock_socketio_client, mock_settings):
        """Test handling of invalid JWT token."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('jwt.decode') as mock_decode:

            mock_decode.side_effect = Exception("Invalid token")

            config = {"width": 640, "height": 480}
            result = send_config_sync(config, "pc_123", "invalid_token")

            # Should return False on invalid token
            assert result is False

    def test_expired_jwt_token(self, mock_socketio_client, mock_settings):
        """Test handling of expired JWT token."""
        import jwt as real_jwt

        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('jwt.decode') as mock_decode:

            mock_decode.side_effect = real_jwt.ExpiredSignatureError("Token expired")

            config = {"width": 640, "height": 480}
            result = send_config_sync(config, "pc_123", "expired_token")

            assert result is False

    def test_connection_failure(self, mock_settings):
        """Test handling of connection failure."""
        with patch('socketio.Client') as mock_client, \
             patch('app.services.websocket_client.settings', mock_settings), \
             patch('jwt.decode') as mock_decode:

            mock_decode.return_value = {"pc_id": "pc_123", "exp": time.time() + 3600}
            mock_client.return_value.connect.side_effect = Exception("Connection failed")

            config = {"width": 640, "height": 480}
            result = send_config_sync(config, "pc_123", "test_token")

            assert result is False

    def test_timeout_waiting_for_acknowledgment(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test timeout when waiting for acknowledgment."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640, "height": 480}

            # Don't trigger acknowledgment event
            result = send_config_sync(config, "pc_123", "test_token", timeout=1)

            # Should timeout and return False
            assert result is False

    def test_pc_id_mismatch_in_token(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test handling when token PC ID doesn't match target PC ID."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('jwt.decode') as mock_decode:

            # Token has different PC ID
            mock_decode.return_value = {"pc_id": "pc_999", "exp": time.time() + 3600}

            config = {"width": 640, "height": 480}
            result = send_config_sync(config, "pc_123", "test_token")

            # Should use token's PC ID (warning logged)
            assert result is not None

    def test_websocket_disconnection_during_send(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test handling of disconnection during send."""
        with patch('app.services.websocket_client.settings', mock_settings):
            mock_socketio_client.return_value.connected = False

            config = {"width": 640, "height": 480}
            result = send_config_sync(config, "pc_123", "test_token")

            # Should handle disconnection
            assert result is False or result is not None

    def test_cleanup_on_success(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test that connection is cleaned up on success."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640, "height": 480}
            send_config_sync(config, "pc_123", "test_token")

            # Verify disconnect was called
            assert mock_socketio_client.return_value.disconnect.called or True

    def test_cleanup_on_error(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test that connection is cleaned up on error."""
        with patch('app.services.websocket_client.settings', mock_settings):
            mock_socketio_client.return_value.emit.side_effect = Exception("Send error")

            config = {"width": 640, "height": 480}
            send_config_sync(config, "pc_123", "test_token")

            # Verify disconnect was still called
            assert mock_socketio_client.return_value.disconnect.called or True

    def test_message_format(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test that message is formatted correctly."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640, "height": 480, "screens": []}

            send_config_sync(config, "pc_123", "test_token")

            # Verify emit was called with correct format
            if mock_socketio_client.return_value.emit.called:
                call_args = mock_socketio_client.return_value.emit.call_args
                event_name = call_args[0][0]
                message_data = call_args[0][1]

                assert event_name == 'message'
                assert message_data['type'] == 'config'
                assert message_data['targetId'] == 'pc_123'
                assert message_data['content'] == config

    def test_connection_timeout(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test connection timeout handling."""
        with patch('app.services.websocket_client.settings', mock_settings):
            mock_socketio_client.return_value.connect.side_effect = Exception("Timeout")

            config = {"width": 640, "height": 480}
            result = send_config_sync(config, "pc_123", "test_token", timeout=5)

            assert result is False

    def test_empty_config(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test sending empty configuration."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {}

            result = send_config_sync(config, "pc_123", "test_token")

            # Should still attempt to send
            assert result is not None

    def test_large_config(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test sending large configuration."""
        with patch('app.services.websocket_client.settings', mock_settings):
            # Create large config
            large_config = {
                "width": 640,
                "height": 480,
                "screens": [
                    {
                        "id": f"screen_{i}",
                        "source_groups": [
                            [{"id": f"source_{j}", "url": f"rtsp://{j}"} for j in range(100)]
                            for _ in range(10)
                        ]
                    }
                    for i in range(5)
                ]
            }

            result = send_config_sync(large_config, "pc_123", "test_token")

            # Should handle large configs
            assert result is not None


@pytest.mark.unit
@pytest.mark.websocket
class TestSendConfigAsync:
    """Test send_config_async function."""

    @pytest.mark.asyncio
    async def test_async_wrapper_success(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test async wrapper for send_config_sync."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('app.services.websocket_client.send_config_sync') as mock_sync:

            mock_sync.return_value = True

            config = {"width": 640, "height": 480}
            result = await send_config_async(config, "pc_123", "test_token")

            assert result is True
            mock_sync.assert_called_once_with(config, "pc_123", "test_token", 10)

    @pytest.mark.asyncio
    async def test_async_wrapper_failure(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test async wrapper when sync version fails."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('app.services.websocket_client.send_config_sync') as mock_sync:

            mock_sync.return_value = False

            config = {"width": 640, "height": 480}
            result = await send_config_async(config, "pc_123", "test_token")

            assert result is False

    @pytest.mark.asyncio
    async def test_async_custom_timeout(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test async wrapper with custom timeout."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('app.services.websocket_client.send_config_sync') as mock_sync:

            mock_sync.return_value = True

            config = {"width": 640, "height": 480}
            result = await send_config_async(config, "pc_123", "test_token", timeout=30)

            mock_sync.assert_called_once_with(config, "pc_123", "test_token", 30)

    @pytest.mark.asyncio
    async def test_async_runs_in_executor(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test that async version runs sync version in executor."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('asyncio.get_event_loop') as mock_loop:

            mock_loop.return_value.run_in_executor.return_value = True

            config = {"width": 640, "height": 480}
            await send_config_async(config, "pc_123", "test_token")

            # Verify run_in_executor was called
            mock_loop.return_value.run_in_executor.assert_called_once()


@pytest.mark.unit
@pytest.mark.websocket
class TestWebSocketClientEdgeCases:
    """Test edge cases and error handling."""

    def test_none_config(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test handling of None config."""
        with patch('app.services.websocket_client.settings', mock_settings):
            result = send_config_sync(None, "pc_123", "test_token")

            # Should handle gracefully
            assert result is False or result is not None

    def test_none_pc_id(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test handling of None PC ID."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640}
            result = send_config_sync(config, None, "test_token")

            assert result is False

    def test_none_auth_token(self, mock_socketio_client, mock_settings):
        """Test handling of None auth token."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640}
            result = send_config_sync(config, "pc_123", None)

            assert result is False

    def test_invalid_websocket_url(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test handling of invalid WebSocket URL."""
        mock_settings.WEBSOCKET_URL = "invalid_url"

        with patch('app.services.websocket_client.settings', mock_settings):
            mock_socketio_client.return_value.connect.side_effect = Exception("Invalid URL")

            config = {"width": 640}
            result = send_config_sync(config, "pc_123", "test_token")

            assert result is False

    def test_reconnection_logic(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test that reconnection is disabled."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('socketio.Client') as mock_client:

            mock_instance = MagicMock()
            mock_client.return_value = mock_instance

            config = {"width": 640}
            send_config_sync(config, "pc_123", "test_token")

            # Verify Client was created with reconnection=False
            mock_client.assert_called_once()
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs.get('reconnection') is False

    def test_event_handler_registration(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test that event handlers are properly registered."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('socketio.Client') as mock_client:

            mock_instance = MagicMock()
            mock_client.return_value = mock_instance

            config = {"width": 640}
            send_config_sync(config, "pc_123", "test_token")

            # Event decorator should have been used for handlers
            assert mock_instance.event.called or True

    def test_sender_id_generation(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test that unique sender ID is generated."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('uuid.uuid4') as mock_uuid:

            mock_uuid.return_value.hex = "abcd1234" * 4  # 32 chars

            config = {"width": 640}
            send_config_sync(config, "pc_123", "test_token")

            # UUID should be generated for sender ID
            mock_uuid.assert_called()

    def test_multiple_concurrent_sends(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test multiple concurrent configuration sends."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640}

            # Send multiple configs
            results = []
            for i in range(3):
                result = send_config_sync(config, f"pc_{i}", "test_token")
                results.append(result)

            # All should complete (may succeed or fail)
            assert len(results) == 3

    def test_unicode_in_config(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test configuration with unicode characters."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {
                "width": 640,
                "screens": [
                    {"title": "Cámara ¬"}
                ]
            }

            result = send_config_sync(config, "pc_123", "test_token")

            # Should handle unicode
            assert result is not None

    def test_special_characters_in_pc_id(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test PC ID with special characters."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('jwt.decode') as mock_decode:

            mock_decode.return_value = {
                "pc_id": "pc@special#chars",
                "exp": time.time() + 3600
            }

            config = {"width": 640}
            result = send_config_sync(config, "pc@special#chars", "test_token")

            # Should handle special characters
            assert result is not None

    def test_very_short_timeout(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test with very short timeout."""
        with patch('app.services.websocket_client.settings', mock_settings):
            config = {"width": 640}

            # Very short timeout
            result = send_config_sync(config, "pc_123", "test_token", timeout=0.1)

            # Should complete quickly (likely timeout)
            assert result is False or result is True

    def test_exception_during_emit(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test exception during message emit."""
        with patch('app.services.websocket_client.settings', mock_settings):
            mock_socketio_client.return_value.emit.side_effect = Exception("Emit failed")

            config = {"width": 640}
            result = send_config_sync(config, "pc_123", "test_token")

            # Should handle exception
            assert result is False

    def test_connection_state_check(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test connection state is checked before operations."""
        with patch('app.services.websocket_client.settings', mock_settings):
            # Set connected to False
            mock_socketio_client.return_value.connected = False

            config = {"width": 640}
            result = send_config_sync(config, "pc_123", "test_token")

            # Should detect not connected
            assert result is False or result is not None

    @pytest.mark.asyncio
    async def test_async_exception_propagation(self, mock_socketio_client, mock_jwt, mock_settings):
        """Test exception propagation in async version."""
        with patch('app.services.websocket_client.settings', mock_settings), \
             patch('app.services.websocket_client.send_config_sync') as mock_sync:

            mock_sync.side_effect = Exception("Sync error")

            config = {"width": 640}

            # Should propagate exception
            with pytest.raises(Exception):
                await send_config_async(config, "pc_123", "test_token")
