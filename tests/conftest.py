"""
Pytest configuration and fixtures for all tests.
"""
import pytest
import sys
import os
from unittest.mock import Mock, MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
import numpy as np
from datetime import datetime
import time

# Add app to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.site import Site, Base
from app.models.camera import Camera
from app.models.screen import Screen, View, ScreenMapping
from app.models.pc import PC


# ============================================================================
# Database Fixtures
# ============================================================================

@pytest.fixture(scope="function")
def engine():
    """Create in-memory SQLite engine for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(engine):
    """Create a new database session for a test."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    mock_session = MagicMock(spec=Session)
    mock_session.query = MagicMock()
    mock_session.add = MagicMock()
    mock_session.commit = MagicMock()
    mock_session.rollback = MagicMock()
    mock_session.close = MagicMock()
    return mock_session


# ============================================================================
# Model Fixtures
# ============================================================================

@pytest.fixture
def sample_site(db_session):
    """Create a sample site."""
    site = Site(
        id="SITE_test123",
        name="Test Site",
        nvr_username="admin",
        nvr_password="password123",
        sureview_site=False,
        new=False,
        use_tcp=False
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture
def sample_camera(db_session, sample_site):
    """Create a sample camera."""
    camera = Camera(
        id="CAM_test456",
        site_id=sample_site.id,
        name="Front Door Camera",
        rtsp_url="rtsp://admin:password@192.168.1.100:554/stream",
        main_stream_url="rtsp://admin:password@192.168.1.100:554/main",
        sureview_camera=False,
        new=False,
        use_tcp=False
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)
    return camera


@pytest.fixture
def sample_pc(db_session):
    """Create a sample PC."""
    pc = PC(
        id="pc_test789",
        name="Test PC 1",
        ip_address="192.168.1.50",
        gpu_type="NVIDIA RTX 3060",
        role="controller",
        auth_token="test_token_123",
        token_expiry=int(time.time()) + 86400,
        last_connected=int(time.time()),
        last_applied=None
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)
    return pc


@pytest.fixture
def sample_screen(db_session, sample_pc):
    """Create a sample screen."""
    screen = Screen(
        id=f"{sample_pc.id}_screen_abc",
        pc_id=sample_pc.id,
        name="Monitor 1",
        rows=3,
        columns=3,
        switching_interval=10
    )
    db_session.add(screen)
    db_session.commit()
    db_session.refresh(screen)
    return screen


@pytest.fixture
def sample_view(db_session, sample_screen):
    """Create a sample view."""
    view = View(
        id=f"{sample_screen.id}_view1",
        screen_id=sample_screen.id,
        name="view_1",
        layout_rows=3,
        layout_columns=3,
        view_number=1
    )
    db_session.add(view)
    db_session.commit()
    db_session.refresh(view)
    return view


@pytest.fixture
def sample_screen_mapping(db_session, sample_pc, sample_screen, sample_view, sample_site, sample_camera):
    """Create a sample screen mapping."""
    mapping = ScreenMapping(
        pc_id=sample_pc.id,
        screen_id=sample_screen.id,
        view_id=sample_view.id,
        slot_row=1,
        slot_col=1,
        site_id=sample_site.id,
        camera_id=sample_camera.id,
        playing_state=False
    )
    db_session.add(mapping)
    db_session.commit()
    db_session.refresh(mapping)
    return mapping


# ============================================================================
# Config Fixtures
# ============================================================================

@pytest.fixture
def sample_camera_config():
    """Sample camera configuration."""
    return {
        "sites": {
            "SITE_123": {
                "name": "Main Office",
                "nvr_username": "admin",
                "nvr_password": "password123",
                "cameras": {
                    "CAM_456": {
                        "name": "Front Door",
                        "rtsp_url": "rtsp://admin:password@192.168.1.100:554/stream",
                        "main_stream_url": "rtsp://admin:password@192.168.1.100:554/main"
                    },
                    "CAM_789": {
                        "name": "Lobby",
                        "rtsp_url": "rtsp://admin:pass@word@192.168.1.101:554/stream",
                        "main_stream_url": None
                    }
                }
            }
        }
    }


@pytest.fixture
def sample_pc_config():
    """Sample PC configuration."""
    return {
        "pcs": {
            "pc_123": {
                "name": "Office PC 1",
                "screens": {
                    "pc_123_screen_abc": {
                        "name": "Monitor 1",
                        "layout": {"rows": 2, "columns": 2},
                        "switching_interval": 10
                    }
                }
            }
        },
        "mappings": {
            "screen_to_cameras": {
                "pc_123": {
                    "pc_123_screen_abc": {
                        "view_1": {
                            "slot_1_1": {
                                "slot_row": 1,
                                "slot_col": 1,
                                "site_id": "SITE_123",
                                "camera_id": "CAM_456",
                                "site_name": "Main Office",
                                "camera_name": "Front Door",
                                "rtsp_url": "rtsp://admin:password@192.168.1.100:554/stream",
                                "use_tcp": False,
                                "playing_state": False
                            }
                        }
                    }
                }
            }
        }
    }


@pytest.fixture
def sample_device_config():
    """Sample device configuration JSON."""
    return {
        "width": 640,
        "height": 480,
        "screens": [
            {
                "id": "pc_123_screen_abc",
                "display_idx": 0,
                "switchInterval": 10,
                "title": "Monitor 1",
                "source_groups": [
                    [
                        {
                            "id": "SITE_123_CAM_456",
                            "osd_text": "Front Door (Main Office)",
                            "url": "rtsp://admin:password@192.168.1.100:554/stream",
                            "osd_color": "0xFFFFFFFF",
                            "LocationUris": ["rtsp://admin:password@192.168.1.100:554/stream"],
                            "use_tcp": False
                        }
                    ]
                ]
            }
        ]
    }


# ============================================================================
# Mock External Dependencies
# ============================================================================

@pytest.fixture
def mock_cv2():
    """Mock OpenCV."""
    with patch('cv2.VideoCapture') as mock_cap:
        mock_instance = MagicMock()
        mock_instance.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
        mock_instance.release.return_value = None
        mock_cap.return_value = mock_instance
        yield mock_cap


@pytest.fixture
def mock_selenium():
    """Mock Selenium WebDriver."""
    with patch('selenium.webdriver.Chrome') as mock_driver:
        mock_instance = MagicMock()
        mock_instance.get_cookies.return_value = [
            {"name": "session_id", "value": "test_session_123"}
        ]
        mock_driver.return_value = mock_instance
        yield mock_driver


@pytest.fixture
def mock_socketio_client():
    """Mock Socket.IO client."""
    with patch('socketio.Client') as mock_client:
        mock_instance = MagicMock()
        mock_instance.connected = True
        mock_instance.connect.return_value = None
        mock_instance.emit.return_value = None
        mock_instance.disconnect.return_value = None
        mock_client.return_value = mock_instance
        yield mock_client


@pytest.fixture
def mock_socketio_server():
    """Mock Socket.IO server."""
    with patch('socketio.Server') as mock_server:
        mock_instance = MagicMock()
        mock_instance.emit.return_value = None
        mock_server.return_value = mock_instance
        yield mock_server


@pytest.fixture
def mock_jwt():
    """Mock JWT encode/decode."""
    with patch('jwt.encode') as mock_encode, patch('jwt.decode') as mock_decode:
        mock_encode.return_value = "test_jwt_token_123"
        mock_decode.return_value = {
            "pc_id": "pc_123",
            "name": "Test PC",
            "exp": int(time.time()) + 86400
        }
        yield {"encode": mock_encode, "decode": mock_decode}


@pytest.fixture
def mock_requests():
    """Mock requests library."""
    with patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}
        mock_get.return_value = mock_response
        yield mock_get


# ============================================================================
# Settings Fixtures
# ============================================================================

@pytest.fixture
def mock_settings():
    """Mock application settings."""
    settings = MagicMock()
    settings.JWT_SECRET = "test_secret_key"
    settings.ALGORITHM = "HS256"
    settings.WEBSOCKET_URL = "http://localhost:8080"
    settings.WEBSOCKET_HOST = "0.0.0.0"
    settings.WEBSOCKET_PORT = 8080
    settings.SUREVIEW_USERNAME = "test_user"
    settings.SUREVIEW_PASSWORD = "test_pass"
    settings.SCREENSHOT_CAPTURE_TIMEOUT = 10
    settings.SCREENSHOT_MAX_WORKERS = 5
    settings.SCREENSHOT_MAX_AGE_HOURS = 24
    settings.BACKGROUND_TASK_INTERVAL = 600
    return settings


# ============================================================================
# Time Fixtures
# ============================================================================

@pytest.fixture
def mock_time():
    """Mock time.time()."""
    with patch('time.time') as mock_t:
        mock_t.return_value = 1704153600.0  # 2024-01-02 00:00:00 UTC
        yield mock_t


@pytest.fixture
def mock_datetime():
    """Mock datetime.now()."""
    with patch('datetime.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2024, 1, 2, 0, 0, 0)
        yield mock_dt
