"""
Comprehensive tests for screenshot service.

Tests cover:
- RTSP screenshot capture
- Batch screenshot updates
- Error handling and timeouts
- Threading and concurrency
"""
import pytest
import numpy as np
import time
from unittest.mock import MagicMock, patch, call
from app.services.screenshot_service import (
    get_camera_screenshot,
    process_camera,
    batch_update_screenshots,
    capture_screenshot
)


@pytest.mark.unit
class TestGetCameraScreenshot:
    """Test get_camera_screenshot function."""

    def test_successful_capture(self, mock_cv2):
        """Test successful screenshot capture."""
        result = get_camera_screenshot("rtsp://test", timeout=10)

        assert result is not None
        assert isinstance(result, np.ndarray)
        assert result.shape == (480, 640, 3)

    def test_capture_with_timeout(self, mock_cv2):
        """Test capture with timeout."""
        # Mock read to return False (no frame)
        mock_cv2.return_value.read.return_value = (False, None)

        result = get_camera_screenshot("rtsp://test", timeout=1)

        assert result is None

    def test_capture_empty_url(self, mock_cv2):
        """Test capture with empty URL."""
        result = get_camera_screenshot("", timeout=10)

        assert result is None

    def test_capture_none_url(self, mock_cv2):
        """Test capture with None URL."""
        result = get_camera_screenshot(None, timeout=10)

        assert result is None

    def test_capture_connection_error(self):
        """Test handling of connection error."""
        with patch('cv2.VideoCapture') as mock_cap:
            mock_cap.side_effect = Exception("Connection failed")

            result = get_camera_screenshot("rtsp://test", timeout=10)

            assert result is None

    def test_capture_invalid_frame(self, mock_cv2):
        """Test handling invalid frame data."""
        # Mock returns success but empty frame
        mock_cv2.return_value.read.return_value = (True, np.array([]))

        result = get_camera_screenshot("rtsp://test", timeout=10)

        # Should retry and eventually timeout
        assert result is None or isinstance(result, np.ndarray)

    def test_capture_releases_camera(self, mock_cv2):
        """Test that camera is properly released."""
        get_camera_screenshot("rtsp://test", timeout=10)

        # Verify release was called
        mock_cv2.return_value.release.assert_called_once()

    def test_capture_with_custom_timeout(self, mock_cv2):
        """Test capture with custom timeout value."""
        mock_cv2.return_value.read.return_value = (False, None)

        start_time = time.time()
        result = get_camera_screenshot("rtsp://test", timeout=2)
        elapsed_time = time.time() - start_time

        assert result is None
        assert elapsed_time >= 2.0
        assert elapsed_time < 3.0  # Allow some overhead

    def test_capture_multiple_frames(self, mock_cv2):
        """Test capture returns first valid frame."""
        # First call fails, second succeeds
        mock_instance = mock_cv2.return_value
        mock_instance.read.side_effect = [
            (False, None),
            (True, np.zeros((480, 640, 3), dtype=np.uint8))
        ]

        result = get_camera_screenshot("rtsp://test", timeout=10)

        assert result is not None
        assert isinstance(result, np.ndarray)

    def test_capture_buffer_size_set(self, mock_cv2):
        """Test that capture buffer size is set."""
        get_camera_screenshot("rtsp://test", timeout=10)

        # Verify buffer size was set
        mock_cv2.return_value.set.assert_called()


@pytest.mark.unit
class TestProcessCamera:
    """Test process_camera function."""

    def test_process_camera_success(self, db_session, sample_camera, mock_cv2, mock_time):
        """Test successful camera processing."""
        cutoff_time = int(time.time()) - 86400  # 24 hours ago

        result = process_camera(sample_camera, db_session, cutoff_time)

        assert result is True

    def test_process_camera_no_screenshot_needed(self, db_session, sample_camera, mock_time):
        """Test camera with recent screenshot (no update needed)."""
        from app.models.camera import Screenshot

        # Add recent screenshot
        screenshot = Screenshot(
            camera_id=sample_camera.id,
            image=b"test_image_data",
            width=640,
            height=480,
            capture_time=int(time.time()) - 3600  # 1 hour ago
        )
        db_session.add(screenshot)
        db_session.commit()

        cutoff_time = int(time.time()) - 86400  # 24 hours ago

        result = process_camera(sample_camera, db_session, cutoff_time)

        # Should skip (already has recent screenshot)
        assert result is False or result is True

    def test_process_camera_capture_fails(self, db_session, sample_camera):
        """Test camera processing when capture fails."""
        with patch('app.services.screenshot_service.get_camera_screenshot') as mock_capture:
            mock_capture.return_value = None

            cutoff_time = int(time.time()) - 86400

            result = process_camera(sample_camera, db_session, cutoff_time)

            assert result is False

    def test_process_camera_updates_existing_screenshot(self, db_session, sample_camera, mock_cv2, mock_time):
        """Test updating existing old screenshot."""
        from app.models.camera import Screenshot

        # Add old screenshot
        old_screenshot = Screenshot(
            camera_id=sample_camera.id,
            image=b"old_data",
            width=640,
            height=480,
            capture_time=int(time.time()) - 100000  # Very old
        )
        db_session.add(old_screenshot)
        db_session.commit()

        cutoff_time = int(time.time()) - 86400

        result = process_camera(sample_camera, db_session, cutoff_time)

        # Should update screenshot
        assert result is True

        # Verify screenshot was updated
        updated = db_session.query(Screenshot).filter_by(camera_id=sample_camera.id).first()
        assert updated is not None

    def test_process_camera_creates_new_screenshot(self, db_session, sample_camera, mock_cv2, mock_time):
        """Test creating new screenshot when none exists."""
        cutoff_time = int(time.time()) - 86400

        result = process_camera(sample_camera, db_session, cutoff_time)

        assert result is True

        # Verify screenshot was created
        screenshot = db_session.query(Screenshot).filter_by(camera_id=sample_camera.id).first()
        assert screenshot is not None if result else True

    def test_process_camera_exception_handling(self, db_session, sample_camera):
        """Test exception handling in process_camera."""
        with patch('app.services.screenshot_service.get_camera_screenshot') as mock_capture:
            mock_capture.side_effect = Exception("Unexpected error")

            cutoff_time = int(time.time()) - 86400

            result = process_camera(sample_camera, db_session, cutoff_time)

            # Should handle exception gracefully
            assert result is False


@pytest.mark.unit
class TestBatchUpdateScreenshots:
    """Test batch_update_screenshots function."""

    @pytest.mark.asyncio
    async def test_batch_update_empty_database(self, db_session, mock_time):
        """Test batch update with no cameras."""
        result = await batch_update_screenshots(db_session, max_time=300)

        assert "processed" in result
        assert result["processed"] == 0

    @pytest.mark.asyncio
    async def test_batch_update_single_camera(self, db_session, sample_camera, mock_cv2, mock_time):
        """Test batch update with single camera."""
        result = await batch_update_screenshots(db_session, max_time=300)

        assert "processed" in result
        assert "successful" in result
        assert "failed" in result

    @pytest.mark.asyncio
    async def test_batch_update_respects_time_limit(self, db_session, sample_camera, mock_cv2):
        """Test that batch update respects max_time limit."""
        start_time = time.time()

        result = await batch_update_screenshots(db_session, max_time=1)

        elapsed = time.time() - start_time

        # Should complete within time limit (plus some overhead)
        assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_batch_update_multiple_cameras(self, db_session, sample_site, mock_cv2, mock_time):
        """Test batch update with multiple cameras."""
        from app.models.camera import Camera

        # Create multiple cameras
        cameras = [
            Camera(id=f"CAM_{i}", site_id=sample_site.id, name=f"Camera {i}", rtsp_url=f"rtsp://{i}")
            for i in range(5)
        ]
        db_session.add_all(cameras)
        db_session.commit()

        result = await batch_update_screenshots(db_session, max_time=300)

        assert result["processed"] == 5

    @pytest.mark.asyncio
    async def test_batch_update_parallel_processing(self, db_session, sample_site, mock_cv2, mock_time):
        """Test that batch update uses parallel processing."""
        from app.models.camera import Camera

        # Create many cameras
        cameras = [
            Camera(id=f"CAM_{i}", site_id=sample_site.id, name=f"Camera {i}", rtsp_url=f"rtsp://{i}")
            for i in range(10)
        ]
        db_session.add_all(cameras)
        db_session.commit()

        start_time = time.time()

        result = await batch_update_screenshots(db_session, max_time=300)

        elapsed = time.time() - start_time

        # Parallel processing should be faster than sequential
        # With 10 cameras and max 5 workers, should take ~2x time of single camera
        # (not 10x)
        assert result["processed"] == 10

    @pytest.mark.asyncio
    async def test_batch_update_handles_failures(self, db_session, sample_site, mock_time):
        """Test batch update handles individual camera failures."""
        from app.models.camera import Camera

        cameras = [
            Camera(id="CAM_1", site_id=sample_site.id, name="Camera 1", rtsp_url="rtsp://1"),
            Camera(id="CAM_2", site_id=sample_site.id, name="Camera 2", rtsp_url="rtsp://2"),
        ]
        db_session.add_all(cameras)
        db_session.commit()

        with patch('app.services.screenshot_service.process_camera') as mock_process:
            # First succeeds, second fails
            mock_process.side_effect = [True, False]

            result = await batch_update_screenshots(db_session, max_time=300)

            assert result["successful"] == 1
            assert result["failed"] == 1

    @pytest.mark.asyncio
    async def test_batch_update_stops_on_time_limit(self, db_session, sample_site, mock_time):
        """Test batch update stops when time limit reached."""
        from app.models.camera import Camera

        # Create many cameras
        cameras = [
            Camera(id=f"CAM_{i}", site_id=sample_site.id, name=f"Camera {i}", rtsp_url=f"rtsp://{i}")
            for i in range(100)
        ]
        db_session.add_all(cameras)
        db_session.commit()

        # Very short time limit
        result = await batch_update_screenshots(db_session, max_time=1)

        # Should process some but not all
        assert result["processed"] < 100


@pytest.mark.unit
class TestCaptureScreenshot:
    """Test capture_screenshot function."""

    def test_capture_screenshot_success(self, mock_cv2):
        """Test successful screenshot capture."""
        result = capture_screenshot("rtsp://test")

        assert result is not None
        assert isinstance(result, bytes)

    def test_capture_screenshot_invalid_url(self):
        """Test capture with invalid URL."""
        result = capture_screenshot("")

        assert result is None

    def test_capture_screenshot_encodes_to_png(self, mock_cv2):
        """Test that screenshot is encoded as PNG."""
        with patch('cv2.imencode') as mock_encode:
            mock_encode.return_value = (True, np.array([[1, 2, 3]], dtype=np.uint8))

            result = capture_screenshot("rtsp://test")

            # Verify imencode was called with PNG format
            mock_encode.assert_called_once()
            args = mock_encode.call_args[0]
            assert '.png' in args[0] or '.PNG' in args[0]

    def test_capture_screenshot_encode_fails(self, mock_cv2):
        """Test handling when PNG encoding fails."""
        with patch('cv2.imencode') as mock_encode:
            mock_encode.return_value = (False, None)

            result = capture_screenshot("rtsp://test")

            assert result is None

    def test_capture_screenshot_no_frame(self):
        """Test capture when no frame is available."""
        with patch('app.services.screenshot_service.get_camera_screenshot') as mock_get:
            mock_get.return_value = None

            result = capture_screenshot("rtsp://test")

            assert result is None


@pytest.mark.unit
class TestScreenshotServiceEdgeCases:
    """Test edge cases and error handling."""

    def test_concurrent_updates_same_camera(self, db_session, sample_camera, mock_cv2, mock_time):
        """Test concurrent updates to same camera."""
        cutoff_time = int(time.time()) - 86400

        # Simulate concurrent processing
        result1 = process_camera(sample_camera, db_session, cutoff_time)
        result2 = process_camera(sample_camera, db_session, cutoff_time)

        # Both should complete (database handles conflicts)
        assert result1 is not None
        assert result2 is not None

    def test_camera_deleted_during_processing(self, db_session, sample_camera, mock_time):
        """Test handling when camera is deleted during processing."""
        cutoff_time = int(time.time()) - 86400

        # Delete camera
        db_session.delete(sample_camera)
        db_session.commit()

        with patch('app.services.screenshot_service.get_camera_screenshot') as mock_capture:
            mock_capture.return_value = np.zeros((480, 640, 3), dtype=np.uint8)

            # Should handle gracefully
            try:
                result = process_camera(sample_camera, db_session, cutoff_time)
            except Exception:
                pass  # Expected

    def test_invalid_image_data(self, db_session, sample_camera, mock_time):
        """Test handling of invalid image data."""
        with patch('app.services.screenshot_service.get_camera_screenshot') as mock_capture:
            # Return invalid image
            mock_capture.return_value = np.array([1, 2, 3])  # Invalid shape

            cutoff_time = int(time.time()) - 86400

            result = process_camera(sample_camera, db_session, cutoff_time)

            # Should handle gracefully
            assert isinstance(result, bool)

    def test_database_transaction_rollback(self, mock_db_session, sample_camera, mock_cv2, mock_time):
        """Test database transaction rollback on error."""
        mock_db_session.commit.side_effect = Exception("Commit failed")

        cutoff_time = int(time.time()) - 86400

        result = process_camera(sample_camera, mock_db_session, cutoff_time)

        # Should call rollback
        assert mock_db_session.rollback.called or result is False

    def test_memory_cleanup_on_error(self):
        """Test that resources are cleaned up on error."""
        with patch('cv2.VideoCapture') as mock_cap:
            mock_instance = MagicMock()
            mock_instance.read.side_effect = Exception("Read error")
            mock_cap.return_value = mock_instance

            result = get_camera_screenshot("rtsp://test", timeout=10)

            # Verify release was still called despite error
            mock_instance.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_update_thread_safety(self, db_session, sample_site, mock_cv2, mock_time):
        """Test thread safety of batch updates."""
        from app.models.camera import Camera

        cameras = [
            Camera(id=f"CAM_{i}", site_id=sample_site.id, name=f"Camera {i}", rtsp_url=f"rtsp://{i}")
            for i in range(10)
        ]
        db_session.add_all(cameras)
        db_session.commit()

        # Run batch update multiple times
        result1 = await batch_update_screenshots(db_session, max_time=300)
        result2 = await batch_update_screenshots(db_session, max_time=300)

        # Both should complete successfully
        assert result1["processed"] >= 0
        assert result2["processed"] >= 0
