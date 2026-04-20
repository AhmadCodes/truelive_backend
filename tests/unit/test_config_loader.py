"""
Comprehensive tests for config loader service.

Tests cover:
- Loading camera configurations
- Loading PC configurations
- Loading site configurations
- Edge cases and error handling
"""
import pytest
from unittest.mock import MagicMock, patch
from app.services.config_loader import (
    load_camera_config,
    load_pc_config,
    load_site_config
)


@pytest.mark.unit
class TestLoadCameraConfig:
    """Test load_camera_config function."""

    def test_load_empty_database(self, db_session):
        """Test loading from empty database."""
        result = load_camera_config(db_session)
        assert result == {"sites": {}}

    def test_load_single_site_no_cameras(self, db_session, sample_site):
        """Test loading site without cameras."""
        result = load_camera_config(db_session)

        assert "sites" in result
        assert sample_site.id in result["sites"]
        assert result["sites"][sample_site.id]["name"] == sample_site.name
        assert result["sites"][sample_site.id]["nvr_username"] == sample_site.nvr_username
        assert result["sites"][sample_site.id]["cameras"] == {}

    def test_load_site_with_cameras(self, db_session, sample_site, sample_camera):
        """Test loading site with cameras."""
        result = load_camera_config(db_session)

        assert sample_site.id in result["sites"]
        site_config = result["sites"][sample_site.id]

        assert sample_camera.id in site_config["cameras"]
        camera_config = site_config["cameras"][sample_camera.id]

        assert camera_config["name"] == sample_camera.name
        assert camera_config["rtsp_url"] == sample_camera.rtsp_url
        assert camera_config["main_stream_url"] == sample_camera.main_stream_url

    def test_load_multiple_sites(self, db_session):
        """Test loading multiple sites."""
        from app.models.site import Site

        # Create multiple sites
        site1 = Site(id="SITE_1", name="Site 1", nvr_username="admin1", nvr_password="pass1")
        site2 = Site(id="SITE_2", name="Site 2", nvr_username="admin2", nvr_password="pass2")

        db_session.add(site1)
        db_session.add(site2)
        db_session.commit()

        result = load_camera_config(db_session)

        assert len(result["sites"]) == 2
        assert "SITE_1" in result["sites"]
        assert "SITE_2" in result["sites"]

    def test_load_site_with_multiple_cameras(self, db_session, sample_site):
        """Test loading site with multiple cameras."""
        from app.models.camera import Camera

        # Create multiple cameras
        cam1 = Camera(id="CAM_1", site_id=sample_site.id, name="Camera 1", rtsp_url="rtsp://1")
        cam2 = Camera(id="CAM_2", site_id=sample_site.id, name="Camera 2", rtsp_url="rtsp://2")
        cam3 = Camera(id="CAM_3", site_id=sample_site.id, name="Camera 3", rtsp_url="rtsp://3")

        db_session.add_all([cam1, cam2, cam3])
        db_session.commit()

        result = load_camera_config(db_session)

        site_config = result["sites"][sample_site.id]
        assert len(site_config["cameras"]) == 3
        assert "CAM_1" in site_config["cameras"]
        assert "CAM_2" in site_config["cameras"]
        assert "CAM_3" in site_config["cameras"]

    def test_database_error_handling(self, mock_db_session):
        """Test error handling when database query fails."""
        mock_db_session.query.side_effect = Exception("Database error")

        result = load_camera_config(mock_db_session)

        # Should return empty structure on error
        assert result == {"sites": {}}

    def test_nvr_password_included(self, db_session, sample_site):
        """Test that NVR password is included in config."""
        result = load_camera_config(db_session)

        site_config = result["sites"][sample_site.id]
        assert "nvr_password" in site_config
        assert site_config["nvr_password"] == sample_site.nvr_password

    def test_camera_with_null_main_stream(self, db_session, sample_site):
        """Test camera with NULL main_stream_url."""
        from app.models.camera import Camera

        camera = Camera(
            id="CAM_NULL",
            site_id=sample_site.id,
            name="Camera Null",
            rtsp_url="rtsp://test",
            main_stream_url=None
        )
        db_session.add(camera)
        db_session.commit()

        result = load_camera_config(db_session)

        camera_config = result["sites"][sample_site.id]["cameras"]["CAM_NULL"]
        assert camera_config["main_stream_url"] is None


@pytest.mark.unit
class TestLoadPCConfig:
    """Test load_pc_config function."""

    def test_load_nonexistent_pc(self, db_session):
        """Test loading configuration for non-existent PC."""
        result = load_pc_config("nonexistent_pc", db_session)

        assert "pcs" in result
        assert "mappings" in result
        assert result["pcs"] == {}
        assert result["mappings"]["screen_to_cameras"] == {}

    def test_load_pc_without_screens(self, db_session, sample_pc):
        """Test loading PC without screens."""
        result = load_pc_config(sample_pc.id, db_session)

        assert sample_pc.id in result["pcs"]
        pc_config = result["pcs"][sample_pc.id]

        assert pc_config["name"] == sample_pc.name
        assert pc_config["screens"] == {}

    def test_load_pc_with_screen(self, db_session, sample_pc, sample_screen):
        """Test loading PC with screen."""
        result = load_pc_config(sample_pc.id, db_session)

        pc_config = result["pcs"][sample_pc.id]
        assert sample_screen.id in pc_config["screens"]

        screen_config = pc_config["screens"][sample_screen.id]
        assert screen_config["name"] == sample_screen.name
        assert screen_config["layout"]["rows"] == sample_screen.rows
        assert screen_config["layout"]["columns"] == sample_screen.columns
        assert screen_config["switching_interval"] == sample_screen.switching_interval

    def test_load_pc_with_views(self, db_session, sample_pc, sample_screen, sample_view):
        """Test loading PC with views."""
        result = load_pc_config(sample_pc.id, db_session)

        # Views should be in mappings section
        mappings = result["mappings"]["screen_to_cameras"]
        assert sample_pc.id in mappings
        assert sample_screen.id in mappings[sample_pc.id]

    def test_load_complete_configuration(self, db_session, sample_pc, sample_screen,
                                         sample_view, sample_screen_mapping,
                                         sample_site, sample_camera):
        """Test loading complete PC configuration with all components."""
        result = load_pc_config(sample_pc.id, db_session)

        # Check PC
        assert sample_pc.id in result["pcs"]

        # Check screen
        assert sample_screen.id in result["pcs"][sample_pc.id]["screens"]

        # Check mappings
        mappings = result["mappings"]["screen_to_cameras"][sample_pc.id][sample_screen.id]
        assert sample_view.name in mappings

        # Check slot mapping
        slot_key = f"slot_{sample_screen_mapping.slot_row}_{sample_screen_mapping.slot_col}"
        assert slot_key in mappings[sample_view.name]

        slot_data = mappings[sample_view.name][slot_key]
        assert slot_data["site_id"] == sample_site.id
        assert slot_data["camera_id"] == sample_camera.id
        assert slot_data["site_name"] == sample_site.name
        assert slot_data["camera_name"] == sample_camera.name
        assert slot_data["rtsp_url"] == sample_camera.rtsp_url
        assert slot_data["use_tcp"] is False
        assert slot_data["playing_state"] == sample_screen_mapping.playing_state

    def _build_use_tcp_scenario(self, db_session, sample_pc, sample_screen, sample_view,
                                 sample_site, camera_use_tcp, site_use_tcp):
        """Helper: create a camera + screen mapping and set both site.use_tcp and camera.use_tcp."""
        from app.models.camera import Camera
        from app.models.screen import ScreenMapping

        sample_site.use_tcp = site_use_tcp
        camera = Camera(
            id=f"CAM_USETCP_{camera_use_tcp}_{site_use_tcp}",
            site_id=sample_site.id,
            name="Scenario Camera",
            rtsp_url="rtsp://test",
            use_tcp=camera_use_tcp
        )
        mapping = ScreenMapping(
            pc_id=sample_pc.id, screen_id=sample_screen.id, view_id=sample_view.id,
            slot_row=1, slot_col=1, site_id=sample_site.id, camera_id=camera.id
        )
        db_session.add_all([camera, mapping])
        db_session.commit()

    def test_use_tcp_camera_override_wins_over_site(self, db_session, sample_pc, sample_screen,
                                                     sample_view, sample_site):
        """camera.use_tcp=True must override site.use_tcp=False."""
        self._build_use_tcp_scenario(db_session, sample_pc, sample_screen, sample_view, sample_site,
                                     camera_use_tcp=True, site_use_tcp=False)

        result = load_pc_config(sample_pc.id, db_session)
        slot_data = result["mappings"]["screen_to_cameras"][sample_pc.id][sample_screen.id][sample_view.name]["slot_1_1"]
        assert slot_data["use_tcp"] is True

    def test_use_tcp_inherits_site_when_camera_null(self, db_session, sample_pc, sample_screen,
                                                     sample_view, sample_site):
        """camera.use_tcp=None must inherit site.use_tcp=True."""
        self._build_use_tcp_scenario(db_session, sample_pc, sample_screen, sample_view, sample_site,
                                     camera_use_tcp=None, site_use_tcp=True)

        result = load_pc_config(sample_pc.id, db_session)
        slot_data = result["mappings"]["screen_to_cameras"][sample_pc.id][sample_screen.id][sample_view.name]["slot_1_1"]
        assert slot_data["use_tcp"] is True

    def test_use_tcp_camera_false_overrides_site_true(self, db_session, sample_pc, sample_screen,
                                                       sample_view, sample_site):
        """camera.use_tcp=False must override site.use_tcp=True (mixed-site scenario)."""
        self._build_use_tcp_scenario(db_session, sample_pc, sample_screen, sample_view, sample_site,
                                     camera_use_tcp=False, site_use_tcp=True)

        result = load_pc_config(sample_pc.id, db_session)
        slot_data = result["mappings"]["screen_to_cameras"][sample_pc.id][sample_screen.id][sample_view.name]["slot_1_1"]
        assert slot_data["use_tcp"] is False

    def test_load_multiple_views(self, db_session, sample_pc, sample_screen):
        """Test loading screen with multiple views."""
        from app.models.screen import View

        view1 = View(id=f"{sample_screen.id}_v1", screen_id=sample_screen.id,
                    name="view_1", layout_rows=2, layout_columns=2, view_number=1)
        view2 = View(id=f"{sample_screen.id}_v2", screen_id=sample_screen.id,
                    name="view_2", layout_rows=3, layout_columns=3, view_number=2)

        db_session.add_all([view1, view2])
        db_session.commit()

        result = load_pc_config(sample_pc.id, db_session)

        mappings = result["mappings"]["screen_to_cameras"][sample_pc.id][sample_screen.id]
        assert "view_1" in mappings
        assert "view_2" in mappings

    def test_load_multiple_slots_in_view(self, db_session, sample_pc, sample_screen,
                                         sample_view, sample_site, sample_camera):
        """Test loading view with multiple slot mappings."""
        from app.models.screen import ScreenMapping

        # Create multiple slot mappings
        mapping1 = ScreenMapping(
            pc_id=sample_pc.id, screen_id=sample_screen.id, view_id=sample_view.id,
            slot_row=1, slot_col=1, site_id=sample_site.id, camera_id=sample_camera.id
        )
        mapping2 = ScreenMapping(
            pc_id=sample_pc.id, screen_id=sample_screen.id, view_id=sample_view.id,
            slot_row=1, slot_col=2, site_id=sample_site.id, camera_id=sample_camera.id
        )
        mapping3 = ScreenMapping(
            pc_id=sample_pc.id, screen_id=sample_screen.id, view_id=sample_view.id,
            slot_row=2, slot_col=1, site_id=sample_site.id, camera_id=sample_camera.id
        )

        db_session.add_all([mapping1, mapping2, mapping3])
        db_session.commit()

        result = load_pc_config(sample_pc.id, db_session)

        view_mappings = result["mappings"]["screen_to_cameras"][sample_pc.id][sample_screen.id][sample_view.name]
        assert len(view_mappings) == 3
        assert "slot_1_1" in view_mappings
        assert "slot_1_2" in view_mappings
        assert "slot_2_1" in view_mappings

    def test_missing_camera_in_mapping(self, db_session, sample_pc, sample_screen, sample_view, sample_site):
        """Test handling when camera is deleted but mapping exists."""
        from app.models.screen import ScreenMapping

        # Create mapping with non-existent camera
        mapping = ScreenMapping(
            pc_id=sample_pc.id, screen_id=sample_screen.id, view_id=sample_view.id,
            slot_row=1, slot_col=1, site_id=sample_site.id, camera_id="NONEXISTENT_CAM"
        )
        db_session.add(mapping)
        db_session.commit()

        result = load_pc_config(sample_pc.id, db_session)

        # Mapping should not be included if camera doesn't exist
        view_mappings = result["mappings"]["screen_to_cameras"][sample_pc.id][sample_screen.id][sample_view.name]
        assert "slot_1_1" not in view_mappings

    def test_database_error_handling(self, mock_db_session):
        """Test error handling when database query fails."""
        mock_db_session.query.side_effect = Exception("Database error")

        result = load_pc_config("pc_123", mock_db_session)

        # Should return empty structure on error
        assert result["pcs"] == {}
        assert result["mappings"]["screen_to_cameras"] == {}


@pytest.mark.unit
class TestLoadSiteConfig:
    """Test load_site_config function."""

    def test_load_empty_database(self, db_session):
        """Test loading from empty database."""
        result = load_site_config(db_session)

        assert "pcs" in result
        assert "mappings" in result
        assert result["pcs"] == {}

    def test_load_single_pc(self, db_session, sample_pc, sample_screen):
        """Test loading single PC configuration."""
        result = load_site_config(db_session)

        assert sample_pc.id in result["pcs"]
        assert sample_screen.id in result["pcs"][sample_pc.id]["screens"]

    def test_load_multiple_pcs(self, db_session):
        """Test loading multiple PCs."""
        from app.models.pc import PC
        from app.models.screen import Screen

        pc1 = PC(id="pc_1", name="PC 1", role="controller")
        pc2 = PC(id="pc_2", name="PC 2", role="controller")

        screen1 = Screen(id="pc_1_screen", pc_id="pc_1", name="Screen 1", rows=2, columns=2, switching_interval=10)
        screen2 = Screen(id="pc_2_screen", pc_id="pc_2", name="Screen 2", rows=3, columns=3, switching_interval=15)

        db_session.add_all([pc1, pc2, screen1, screen2])
        db_session.commit()

        result = load_site_config(db_session)

        assert len(result["pcs"]) == 2
        assert "pc_1" in result["pcs"]
        assert "pc_2" in result["pcs"]

    def test_aggregates_all_mappings(self, db_session, sample_pc, sample_screen, sample_view,
                                     sample_screen_mapping):
        """Test that all mappings are aggregated."""
        result = load_site_config(db_session)

        assert sample_pc.id in result["mappings"]["screen_to_cameras"]
        assert sample_screen.id in result["mappings"]["screen_to_cameras"][sample_pc.id]

    def test_database_error_handling(self, mock_db_session):
        """Test error handling when database query fails."""
        mock_db_session.query.side_effect = Exception("Database error")

        result = load_site_config(mock_db_session)

        # Should return empty structure on error
        assert result["pcs"] == {}
        assert result["mappings"]["screen_to_cameras"] == {}

    def test_pc_without_screens(self, db_session):
        """Test PC without any screens."""
        from app.models.pc import PC

        pc = PC(id="pc_empty", name="Empty PC", role="controller")
        db_session.add(pc)
        db_session.commit()

        result = load_site_config(db_session)

        assert "pc_empty" in result["pcs"]
        assert result["pcs"]["pc_empty"]["screens"] == {}


@pytest.mark.unit
class TestConfigLoaderIntegration:
    """Integration tests for config loader functions."""

    def test_full_stack_configuration(self, db_session):
        """Test loading complete multi-PC, multi-screen configuration."""
        from app.models.pc import PC
        from app.models.screen import Screen, View, ScreenMapping
        from app.models.site import Site
        from app.models.camera import Camera

        # Create sites and cameras
        site1 = Site(id="SITE_1", name="Site 1", nvr_username="admin", nvr_password="pass")
        site2 = Site(id="SITE_2", name="Site 2", nvr_username="admin", nvr_password="pass")

        cam1 = Camera(id="CAM_1", site_id="SITE_1", name="Camera 1", rtsp_url="rtsp://1")
        cam2 = Camera(id="CAM_2", site_id="SITE_2", name="Camera 2", rtsp_url="rtsp://2")

        # Create PCs and screens
        pc1 = PC(id="pc_1", name="PC 1", role="controller")
        pc2 = PC(id="pc_2", name="PC 2", role="controller")

        screen1 = Screen(id="pc_1_s1", pc_id="pc_1", name="Screen 1", rows=2, columns=2, switching_interval=10)
        screen2 = Screen(id="pc_2_s1", pc_id="pc_2", name="Screen 1", rows=3, columns=3, switching_interval=15)

        # Create views
        view1 = View(id="pc_1_s1_v1", screen_id="pc_1_s1", name="view_1", layout_rows=2, layout_columns=2, view_number=1)
        view2 = View(id="pc_2_s1_v1", screen_id="pc_2_s1", name="view_1", layout_rows=3, layout_columns=3, view_number=1)

        # Create mappings
        mapping1 = ScreenMapping(
            pc_id="pc_1", screen_id="pc_1_s1", view_id="pc_1_s1_v1",
            slot_row=1, slot_col=1, site_id="SITE_1", camera_id="CAM_1"
        )
        mapping2 = ScreenMapping(
            pc_id="pc_2", screen_id="pc_2_s1", view_id="pc_2_s1_v1",
            slot_row=1, slot_col=1, site_id="SITE_2", camera_id="CAM_2"
        )

        db_session.add_all([site1, site2, cam1, cam2, pc1, pc2, screen1, screen2, view1, view2, mapping1, mapping2])
        db_session.commit()

        # Load all configs
        camera_config = load_camera_config(db_session)
        site_config = load_site_config(db_session)

        # Verify camera config
        assert len(camera_config["sites"]) == 2
        assert "SITE_1" in camera_config["sites"]
        assert "SITE_2" in camera_config["sites"]

        # Verify site config
        assert len(site_config["pcs"]) == 2
        assert "pc_1" in site_config["pcs"]
        assert "pc_2" in site_config["pcs"]

        # Verify mappings
        assert "pc_1" in site_config["mappings"]["screen_to_cameras"]
        assert "pc_2" in site_config["mappings"]["screen_to_cameras"]
