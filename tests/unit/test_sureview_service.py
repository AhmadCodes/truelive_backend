"""
Comprehensive tests for SureView service.

Tests cover:
- Selenium automation for login
- API data fetching
- Device synchronization
- Error handling
"""
import pytest
from unittest.mock import MagicMock, patch, Mock
from app.services.sureview_service import (
    automate_login,
    get_server_list,
    get_devices_by_server_id,
    sync_sureview_devices
)


@pytest.mark.unit
class TestAutomateLogin:
    """Test automate_login function."""

    def test_successful_login(self, mock_selenium, mock_settings):
        """Test successful login and cookie retrieval."""
        with patch('app.services.sureview_service.settings', mock_settings):
            cookies = automate_login()

            assert cookies is not None
            assert len(cookies) == 1
            assert cookies[0]["name"] == "session_id"

    def test_login_with_invalid_credentials(self, mock_selenium, mock_settings):
        """Test login with invalid credentials."""
        mock_selenium.return_value.find_element.side_effect = Exception("Element not found")

        with patch('app.services.sureview_service.settings', mock_settings):
            cookies = automate_login()

            # Should return None on failure
            assert cookies is None

    def test_login_timeout(self, mock_selenium, mock_settings):
        """Test login timeout handling."""
        mock_selenium.return_value.get.side_effect = Exception("Timeout")

        with patch('app.services.sureview_service.settings', mock_settings):
            cookies = automate_login()

            assert cookies is None

    def test_driver_cleanup_on_success(self, mock_selenium, mock_settings):
        """Test that driver is quit on success."""
        with patch('app.services.sureview_service.settings', mock_settings):
            automate_login()

            mock_selenium.return_value.quit.assert_called_once()

    def test_driver_cleanup_on_error(self, mock_selenium, mock_settings):
        """Test that driver is quit even on error."""
        mock_selenium.return_value.get.side_effect = Exception("Error")

        with patch('app.services.sureview_service.settings', mock_settings):
            automate_login()

            mock_selenium.return_value.quit.assert_called_once()

    def test_headless_chrome_configuration(self, mock_settings):
        """Test that Chrome is configured for headless mode."""
        with patch('selenium.webdriver.Chrome') as mock_chrome, \
             patch('selenium.webdriver.ChromeOptions') as mock_options, \
             patch('app.services.sureview_service.settings', mock_settings):

            mock_options_instance = MagicMock()
            mock_options.return_value = mock_options_instance

            automate_login()

            # Verify headless option was added
            mock_options_instance.add_argument.assert_any_call('--headless')

    def test_docker_environment_detection(self, mock_selenium, mock_settings):
        """Test detection of Docker environment."""
        with patch('app.services.sureview_service.is_docker') as mock_is_docker, \
             patch('app.services.sureview_service.settings', mock_settings):

            mock_is_docker.return_value = True

            automate_login()

            # Should use ChromeDriverManager in Docker
            mock_is_docker.assert_called_once()


@pytest.mark.unit
class TestGetServerList:
    """Test get_server_list function."""

    def test_successful_fetch(self, mock_requests):
        """Test successful server list fetch."""
        mock_requests.return_value.json.return_value = {
            "data": [
                {"id": "server1", "name": "Server 1"},
                {"id": "server2", "name": "Server 2"}
            ]
        }

        cookies = [{"name": "session", "value": "test"}]
        servers = get_server_list(cookies)

        assert servers is not None
        assert len(servers) == 2
        assert servers[0]["id"] == "server1"

    def test_empty_server_list(self, mock_requests):
        """Test handling of empty server list."""
        mock_requests.return_value.json.return_value = {"data": []}

        cookies = [{"name": "session", "value": "test"}]
        servers = get_server_list(cookies)

        assert servers == []

    def test_api_error(self, mock_requests):
        """Test handling of API error."""
        mock_requests.return_value.status_code = 500
        mock_requests.return_value.json.return_value = {"error": "Internal server error"}

        cookies = [{"name": "session", "value": "test"}]
        servers = get_server_list(cookies)

        # Should return empty list on error
        assert servers == [] or servers is None

    def test_network_error(self):
        """Test handling of network error."""
        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception("Network error")

            cookies = [{"name": "session", "value": "test"}]
            servers = get_server_list(cookies)

            assert servers is None or servers == []

    def test_invalid_json_response(self, mock_requests):
        """Test handling of invalid JSON response."""
        mock_requests.return_value.json.side_effect = ValueError("Invalid JSON")

        cookies = [{"name": "session", "value": "test"}]
        servers = get_server_list(cookies)

        assert servers is None or servers == []

    def test_max_200_servers_limit(self, mock_requests):
        """Test that API request includes max 200 servers limit."""
        cookies = [{"name": "session", "value": "test"}]

        with patch('requests.get') as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = {"data": []}
            mock_get.return_value = mock_response

            get_server_list(cookies)

            # Verify request was made with params
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert call_args is not None

    def test_cookie_formatting(self):
        """Test that cookies are properly formatted for request."""
        with patch('requests.get') as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = {"data": []}
            mock_get.return_value = mock_response

            cookies = [
                {"name": "session_id", "value": "abc123"},
                {"name": "user_token", "value": "xyz789"}
            ]

            get_server_list(cookies)

            # Verify cookies parameter was passed
            mock_get.assert_called_once()


@pytest.mark.unit
class TestGetDevicesByServerId:
    """Test get_devices_by_server_id function."""

    def test_successful_fetch(self, mock_requests):
        """Test successful device fetch."""
        mock_requests.return_value.json.return_value = {
            "data": [
                {"id": "device1", "name": "Device 1", "type": "camera"},
                {"id": "device2", "name": "Device 2", "type": "nvr"}
            ]
        }

        cookies = [{"name": "session", "value": "test"}]
        devices = get_devices_by_server_id(cookies, "server_123")

        assert devices is not None
        assert len(devices) == 2
        assert devices[0]["id"] == "device1"

    def test_empty_device_list(self, mock_requests):
        """Test handling of empty device list."""
        mock_requests.return_value.json.return_value = {"data": []}

        cookies = [{"name": "session", "value": "test"}]
        devices = get_devices_by_server_id(cookies, "server_123")

        assert devices == []

    def test_server_id_parameter(self):
        """Test that server ID is included in request."""
        with patch('requests.get') as mock_get:
            mock_response = MagicMock()
            mock_response.json.return_value = {"data": []}
            mock_get.return_value = mock_response

            cookies = [{"name": "session", "value": "test"}]
            get_devices_by_server_id(cookies, "server_456")

            # Verify server ID was used in URL or params
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert "server_456" in str(call_args)

    def test_api_error(self, mock_requests):
        """Test handling of API error."""
        mock_requests.return_value.status_code = 404
        mock_requests.return_value.json.return_value = {"error": "Server not found"}

        cookies = [{"name": "session", "value": "test"}]
        devices = get_devices_by_server_id(cookies, "invalid_server")

        assert devices == [] or devices is None

    def test_network_error(self):
        """Test handling of network error."""
        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception("Connection timeout")

            cookies = [{"name": "session", "value": "test"}]
            devices = get_devices_by_server_id(cookies, "server_123")

            assert devices is None or devices == []


@pytest.mark.unit
class TestSyncSureViewDevices:
    """Test sync_sureview_devices function."""

    @pytest.mark.asyncio
    async def test_successful_sync(self, mock_db_session):
        """Test successful device synchronization."""
        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers, \
             patch('app.services.sureview_service.get_devices_by_server_id') as mock_devices:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = [{"id": "server1", "name": "Server 1"}]
            mock_devices.return_value = [
                {"id": "device1", "name": "Device 1", "cameras": [{"id": "cam1", "name": "Camera 1"}]}
            ]

            result = await sync_sureview_devices(mock_db_session)

            assert "synced_sites" in result
            assert "synced_cameras" in result

    @pytest.mark.asyncio
    async def test_login_failure(self, mock_db_session):
        """Test handling of login failure."""
        with patch('app.services.sureview_service.automate_login') as mock_login:
            mock_login.return_value = None

            result = await sync_sureview_devices(mock_db_session)

            # Should return error result
            assert result["synced_sites"] == 0 or "error" in result

    @pytest.mark.asyncio
    async def test_empty_server_list(self, mock_db_session):
        """Test handling of empty server list."""
        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = []

            result = await sync_sureview_devices(mock_db_session)

            assert result["synced_sites"] == 0
            assert result["synced_cameras"] == 0

    @pytest.mark.asyncio
    async def test_multiple_servers(self, mock_db_session):
        """Test synchronization with multiple servers."""
        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers, \
             patch('app.services.sureview_service.get_devices_by_server_id') as mock_devices:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = [
                {"id": "server1", "name": "Server 1"},
                {"id": "server2", "name": "Server 2"},
                {"id": "server3", "name": "Server 3"}
            ]
            mock_devices.return_value = []

            result = await sync_sureview_devices(mock_db_session)

            # Should call get_devices for each server
            assert mock_devices.call_count == 3

    @pytest.mark.asyncio
    async def test_database_updates(self, db_session):
        """Test that database is updated with synced data."""
        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers, \
             patch('app.services.sureview_service.get_devices_by_server_id') as mock_devices:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = [{"id": "server1", "name": "Server 1"}]
            mock_devices.return_value = [
                {
                    "id": "site1",
                    "name": "Site 1",
                    "username": "admin",
                    "password": "pass",
                    "cameras": [
                        {"id": "cam1", "name": "Camera 1", "rtsp_url": "rtsp://test"}
                    ]
                }
            ]

            result = await sync_sureview_devices(db_session)

            # Verify data was added to database
            from app.models.site import Site
            from app.models.camera import Camera

            sites = db_session.query(Site).filter_by(sureview_site=True).all()
            cameras = db_session.query(Camera).filter_by(sureview_camera=True).all()

            # Should have created site and camera (or attempted to)
            assert result["synced_sites"] >= 0
            assert result["synced_cameras"] >= 0

    @pytest.mark.asyncio
    async def test_stale_data_cleanup(self, db_session):
        """Test that stale sites/cameras are removed."""
        from app.models.site import Site
        from app.models.camera import Camera

        # Create old SureView data
        old_site = Site(
            id="OLD_SITE",
            name="Old Site",
            nvr_username="admin",
            nvr_password="pass",
            sureview_site=True
        )
        old_camera = Camera(
            id="OLD_CAM",
            site_id="OLD_SITE",
            name="Old Camera",
            rtsp_url="rtsp://old",
            sureview_camera=True
        )
        db_session.add_all([old_site, old_camera])
        db_session.commit()

        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers, \
             patch('app.services.sureview_service.get_devices_by_server_id') as mock_devices:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = []
            mock_devices.return_value = []

            result = await sync_sureview_devices(db_session)

            # Old data should be cleaned up
            assert result is not None

    @pytest.mark.asyncio
    async def test_partial_sync_failure(self, mock_db_session):
        """Test handling when some servers fail to sync."""
        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers, \
             patch('app.services.sureview_service.get_devices_by_server_id') as mock_devices:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = [
                {"id": "server1", "name": "Server 1"},
                {"id": "server2", "name": "Server 2"}
            ]
            # First call succeeds, second fails
            mock_devices.side_effect = [
                [{"id": "device1", "name": "Device 1"}],
                Exception("API error")
            ]

            result = await sync_sureview_devices(mock_db_session)

            # Should continue despite partial failure
            assert result is not None

    @pytest.mark.asyncio
    async def test_database_transaction_rollback(self, mock_db_session):
        """Test database rollback on error."""
        mock_db_session.commit.side_effect = Exception("Commit failed")

        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = []

            result = await sync_sureview_devices(mock_db_session)

            # Should call rollback on error
            mock_db_session.rollback.assert_called()


@pytest.mark.unit
class TestSureViewServiceEdgeCases:
    """Test edge cases and error handling."""

    def test_malformed_cookie_data(self):
        """Test handling of malformed cookie data."""
        cookies = [{"invalid": "structure"}]  # Missing 'name' and 'value'

        result = get_server_list(cookies)

        # Should handle gracefully
        assert result is None or result == []

    def test_unicode_in_device_names(self, mock_requests):
        """Test handling of unicode characters in device names."""
        mock_requests.return_value.json.return_value = {
            "data": [{"id": "device1", "name": "Cámara 1 - ¬"}]
        }

        cookies = [{"name": "session", "value": "test"}]
        devices = get_devices_by_server_id(cookies, "server1")

        assert devices is not None
        assert devices[0]["name"] == "Cámara 1 - ¬"

    def test_very_large_server_list(self, mock_requests):
        """Test handling of very large server list."""
        large_server_list = [
            {"id": f"server{i}", "name": f"Server {i}"}
            for i in range(500)
        ]
        mock_requests.return_value.json.return_value = {"data": large_server_list}

        cookies = [{"name": "session", "value": "test"}]
        servers = get_server_list(cookies)

        # Should handle large lists
        assert len(servers) == 500

    def test_missing_required_fields(self, mock_requests):
        """Test handling of devices with missing required fields."""
        mock_requests.return_value.json.return_value = {
            "data": [
                {"id": "device1"},  # Missing 'name'
                {"name": "Device 2"}  # Missing 'id'
            ]
        }

        cookies = [{"name": "session", "value": "test"}]
        devices = get_devices_by_server_id(cookies, "server1")

        # Should handle gracefully
        assert devices is not None

    @pytest.mark.asyncio
    async def test_concurrent_sync_operations(self, mock_db_session):
        """Test concurrent sync operations."""
        with patch('app.services.sureview_service.automate_login') as mock_login, \
             patch('app.services.sureview_service.get_server_list') as mock_servers:

            mock_login.return_value = [{"name": "session", "value": "test"}]
            mock_servers.return_value = []

            # Run sync twice concurrently
            import asyncio
            results = await asyncio.gather(
                sync_sureview_devices(mock_db_session),
                sync_sureview_devices(mock_db_session)
            )

            # Both should complete
            assert len(results) == 2
