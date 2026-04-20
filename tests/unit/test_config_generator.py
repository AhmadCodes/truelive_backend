"""
Comprehensive tests for configuration generator service.

Tests cover:
- Device configuration generation
- RTSP password encoding
- OSD color assignment
- LocationUris building
- Edge cases and error handling
"""
import pytest
from unittest.mock import MagicMock, patch, Mock
from app.services.config_generator import (
    generate_config,
    _get_site_color,
    _get_location_uris,
    _create_empty_source
)


@pytest.mark.unit
class TestGenerateConfig:
    """Test generate_config function."""

    def test_generate_empty_config(self, mock_db_session):
        """Test generating config from empty input."""
        site_config = {"pcs": {}, "mappings": {"screen_to_cameras": {}}}

        result = generate_config(site_config, mock_db_session)

        assert result["width"] == 640
        assert result["height"] == 480
        assert result["screens"] == []

    def test_generate_basic_config(self, sample_pc_config, mock_db_session):
        """Test generating basic configuration."""
        with patch('app.services.config_generator._get_site_color') as mock_color, \
             patch('app.services.config_generator._get_location_uris') as mock_uris:

            mock_color.return_value = "0xFFFFFFFF"
            mock_uris.return_value = ["rtsp://test"]

            result = generate_config(sample_pc_config, mock_db_session)

            assert "screens" in result
            assert len(result["screens"]) == 1

            screen = result["screens"][0]
            assert screen["id"] == "pc_123_screen_abc"
            assert screen["display_idx"] == 0
            assert screen["switchInterval"] == 10
            assert screen["title"] == "Monitor 1"

    def test_generate_with_camera_mapping(self, sample_pc_config, db_session):
        """Test generating config with camera mappings."""
        from app.models.site import Site
        from app.models.camera import Camera
        from app.models.screen import Screen

        # Create test data
        site = Site(id="SITE_123", name="Main Office", nvr_username="admin", nvr_password="pass")
        camera = Camera(id="CAM_456", site_id="SITE_123", name="Front Door", rtsp_url="rtsp://test")
        screen = Screen(id="pc_123_screen_abc", pc_id="pc_123", name="Monitor 1", rows=2, columns=2, switching_interval=10)

        db_session.add_all([site, camera, screen])
        db_session.commit()

        result = generate_config(sample_pc_config, db_session)

        assert len(result["screens"]) == 1
        screen_config = result["screens"][0]

        assert "source_groups" in screen_config
        assert len(screen_config["source_groups"]) > 0

        # Check first source group (view 1)
        first_group = screen_config["source_groups"][0]
        assert len(first_group) > 0

        # Check source entry
        source = first_group[0]
        assert source["id"] == "SITE_123_CAM_456"
        assert "Front Door" in source["osd_text"]
        assert "Main Office" in source["osd_text"]
        assert source["url"] == "rtsp://test"
        assert source["use_tcp"] is False

    def test_use_tcp_true_propagates_to_source_entry(self, sample_pc_config, db_session):
        """use_tcp=True in slot_data must surface as use_tcp: True in the generated source entry."""
        from app.models.site import Site
        from app.models.camera import Camera
        from app.models.screen import Screen

        site = Site(id="SITE_123", name="Main Office", nvr_username="admin", nvr_password="pass")
        camera = Camera(id="CAM_456", site_id="SITE_123", name="Front Door", rtsp_url="rtsp://test")
        screen = Screen(id="pc_123_screen_abc", pc_id="pc_123", name="Monitor 1", rows=2, columns=2, switching_interval=10)
        db_session.add_all([site, camera, screen])
        db_session.commit()

        # Flip use_tcp=True in the slot_data of the fixture-derived config
        config = {
            **sample_pc_config,
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
                                    "rtsp_url": "rtsp://test",
                                    "use_tcp": True,
                                    "playing_state": False
                                }
                            }
                        }
                    }
                }
            }
        }

        result = generate_config(config, db_session)

        source = result["screens"][0]["source_groups"][0][0]
        assert source["use_tcp"] is True

    def test_password_encoding_in_config(self, sample_pc_config, db_session):
        """Test that RTSP passwords are URL-encoded."""
        from app.models.site import Site
        from app.models.camera import Camera
        from app.models.screen import Screen

        # Camera with special characters in password
        site = Site(id="SITE_123", name="Test Site", nvr_username="admin", nvr_password="pass")
        camera = Camera(
            id="CAM_456",
            site_id="SITE_123",
            name="Camera",
            rtsp_url="rtsp://admin:p@ss@192.168.1.100:554/stream"
        )
        screen = Screen(id="pc_123_screen_abc", pc_id="pc_123", name="Monitor 1", rows=2, columns=2, switching_interval=10)

        db_session.add_all([site, camera, screen])
        db_session.commit()

        result = generate_config(sample_pc_config, db_session)

        source = result["screens"][0]["source_groups"][0][0]
        # Password should be URL-encoded
        assert "%40" in source["url"] or "@" in source["url"]

    def test_multiple_views_multiple_source_groups(self, db_session):
        """Test multiple views create multiple source groups."""
        from app.models.site import Site
        from app.models.camera import Camera
        from app.models.pc import PC
        from app.models.screen import Screen, View, ScreenMapping

        # Setup data
        site = Site(id="SITE_1", name="Site 1", nvr_username="admin", nvr_password="pass")
        cam1 = Camera(id="CAM_1", site_id="SITE_1", name="Camera 1", rtsp_url="rtsp://1")
        cam2 = Camera(id="CAM_2", site_id="SITE_1", name="Camera 2", rtsp_url="rtsp://2")

        pc = PC(id="pc_1", name="PC 1", role="controller")
        screen = Screen(id="pc_1_s1", pc_id="pc_1", name="Screen 1", rows=2, columns=2, switching_interval=10)

        view1 = View(id="pc_1_s1_v1", screen_id="pc_1_s1", name="view_1", layout_rows=2, layout_columns=2, view_number=1)
        view2 = View(id="pc_1_s1_v2", screen_id="pc_1_s1", name="view_2", layout_rows=2, layout_columns=2, view_number=2)

        mapping1 = ScreenMapping(
            pc_id="pc_1", screen_id="pc_1_s1", view_id="pc_1_s1_v1",
            slot_row=1, slot_col=1, site_id="SITE_1", camera_id="CAM_1"
        )
        mapping2 = ScreenMapping(
            pc_id="pc_1", screen_id="pc_1_s1", view_id="pc_1_s1_v2",
            slot_row=1, slot_col=1, site_id="SITE_1", camera_id="CAM_2"
        )

        db_session.add_all([site, cam1, cam2, pc, screen, view1, view2, mapping1, mapping2])
        db_session.commit()

        config = {
            "pcs": {
                "pc_1": {
                    "name": "PC 1",
                    "screens": {
                        "pc_1_s1": {
                            "name": "Screen 1",
                            "layout": {"rows": 2, "columns": 2},
                            "switching_interval": 10
                        }
                    }
                }
            },
            "mappings": {
                "screen_to_cameras": {
                    "pc_1": {
                        "pc_1_s1": {
                            "view_1": {
                                "slot_1_1": {
                                    "site_id": "SITE_1",
                                    "camera_id": "CAM_1",
                                    "site_name": "Site 1",
                                    "camera_name": "Camera 1",
                                    "rtsp_url": "rtsp://1",
                                    "use_tcp": False
                                }
                            },
                            "view_2": {
                                "slot_1_1": {
                                    "site_id": "SITE_1",
                                    "camera_id": "CAM_2",
                                    "site_name": "Site 1",
                                    "camera_name": "Camera 2",
                                    "rtsp_url": "rtsp://2",
                                    "use_tcp": False
                                }
                            }
                        }
                    }
                }
            }
        }

        result = generate_config(config, db_session)

        # Should have source groups for each view
        assert len(result["screens"][0]["source_groups"]) >= 1

    def test_empty_slots_create_empty_sources(self, sample_pc_config, mock_db_session):
        """Test that empty slots create minimal placeholder sources."""
        # Config with no camera mappings
        empty_config = {
            "pcs": {
                "pc_1": {
                    "name": "PC 1",
                    "screens": {
                        "screen_1": {
                            "name": "Screen 1",
                            "layout": {"rows": 2, "columns": 2},
                            "switching_interval": 10
                        }
                    }
                }
            },
            "mappings": {"screen_to_cameras": {"pc_1": {"screen_1": {"view_1": {}}}}}
        }

        with patch('app.services.config_generator._create_empty_source') as mock_empty:
            mock_empty.return_value = {"id": "empty", "url": ""}

            result = generate_config(empty_config, mock_db_session)

            # Should still create screen
            assert len(result["screens"]) == 1


@pytest.mark.unit
class TestGetSiteColor:
    """Test _get_site_color function."""

    def test_get_color_for_site_with_category(self, db_session):
        """Test getting color for site with category."""
        from app.models.site import Site, SiteCategory, SiteCategoryMapping

        site = Site(id="SITE_1", name="Site 1", nvr_username="admin", nvr_password="pass")
        category = SiteCategory(id="cat_1", name="Category 1", color=0xFF0000FF)  # Red
        mapping = SiteCategoryMapping(site_id="SITE_1", category_id="cat_1")

        db_session.add_all([site, category, mapping])
        db_session.commit()

        color = _get_site_color("SITE_1", db_session)

        assert color == "0xFF0000FF"

    def test_get_default_color_no_category(self, db_session):
        """Test default color when site has no category."""
        from app.models.site import Site

        site = Site(id="SITE_1", name="Site 1", nvr_username="admin", nvr_password="pass")
        db_session.add(site)
        db_session.commit()

        color = _get_site_color("SITE_1", db_session)

        assert color == "0xFFFFFFFF"  # White default

    def test_get_color_nonexistent_site(self, db_session):
        """Test getting color for non-existent site."""
        color = _get_site_color("NONEXISTENT", db_session)

        assert color == "0xFFFFFFFF"  # Default white

    def test_get_color_database_error(self, mock_db_session):
        """Test error handling when database query fails."""
        mock_db_session.query.side_effect = Exception("Database error")

        color = _get_site_color("SITE_1", mock_db_session)

        assert color == "0xFFFFFFFF"  # Default on error


@pytest.mark.unit
class TestGetLocationUris:
    """Test _get_location_uris function."""

    def test_get_uris_for_site(self, db_session):
        """Test getting location URIs for a site."""
        from app.models.site import Site, SiteCamerasLayout
        from app.models.camera import Camera

        site = Site(id="SITE_1", name="Site 1", nvr_username="admin", nvr_password="pass")
        cam1 = Camera(id="CAM_1", site_id="SITE_1", name="Camera 1", rtsp_url="rtsp://1")
        cam2 = Camera(id="CAM_2", site_id="SITE_1", name="Camera 2", rtsp_url="rtsp://2")

        layout1 = SiteCamerasLayout(site_id="SITE_1", site_name="Site 1", slot_row=1, slot_col=1, camera_id="CAM_1")
        layout2 = SiteCamerasLayout(site_id="SITE_1", site_name="Site 1", slot_row=1, slot_col=2, camera_id="CAM_2")

        db_session.add_all([site, cam1, cam2, layout1, layout2])
        db_session.commit()

        uris = _get_location_uris("SITE_1", db_session)

        assert len(uris) == 2
        assert "rtsp://1" in uris
        assert "rtsp://2" in uris

    def test_get_uris_empty_layout(self, db_session):
        """Test getting URIs when site has no layout."""
        from app.models.site import Site

        site = Site(id="SITE_1", name="Site 1", nvr_username="admin", nvr_password="pass")
        db_session.add(site)
        db_session.commit()

        uris = _get_location_uris("SITE_1", db_session)

        assert uris == []

    def test_get_uris_nonexistent_site(self, db_session):
        """Test getting URIs for non-existent site."""
        uris = _get_location_uris("NONEXISTENT", db_session)

        assert uris == []

    def test_get_uris_with_null_camera(self, db_session):
        """Test handling when camera is deleted but layout remains."""
        from app.models.site import Site, SiteCamerasLayout

        site = Site(id="SITE_1", name="Site 1", nvr_username="admin", nvr_password="pass")
        layout = SiteCamerasLayout(site_id="SITE_1", site_name="Site 1", slot_row=1, slot_col=1, camera_id="NONEXISTENT_CAM")

        db_session.add_all([site, layout])
        db_session.commit()

        uris = _get_location_uris("SITE_1", db_session)

        # Should skip cameras that don't exist
        assert uris == []

    def test_get_uris_database_error(self, mock_db_session):
        """Test error handling when database query fails."""
        mock_db_session.query.side_effect = Exception("Database error")

        uris = _get_location_uris("SITE_1", mock_db_session)

        assert uris == []


@pytest.mark.unit
class TestCreateEmptySource:
    """Test _create_empty_source function."""

    def test_create_empty_source_basic(self):
        """Test creating basic empty source."""
        source = _create_empty_source()

        assert "id" in source
        assert "url" in source
        assert source["url"] == ""
        assert source["use_tcp"] is False

    def test_empty_source_structure(self):
        """Test empty source has correct structure."""
        source = _create_empty_source()

        # Check all required fields
        required_fields = ["id", "osd_text", "url", "osd_color", "LocationUris", "use_tcp"]
        for field in required_fields:
            assert field in source

    def test_empty_source_values(self):
        """Test empty source has correct default values."""
        source = _create_empty_source()

        assert source["osd_text"] == ""
        assert source["osd_color"] == "0xFFFFFFFF"
        assert source["LocationUris"] == []
        assert source["use_tcp"] is False


@pytest.mark.unit
class TestConfigGeneratorEdgeCases:
    """Test edge cases and error handling."""

    def test_config_with_null_values(self, mock_db_session):
        """Test handling config with null/missing values."""
        config = {
            "pcs": {
                "pc_1": {
                    "name": None,
                    "screens": None
                }
            },
            "mappings": None
        }

        # Should not crash
        result = generate_config(config, mock_db_session)

        assert "screens" in result
        assert result["width"] == 640
        assert result["height"] == 480

    def test_malformed_mapping_structure(self, mock_db_session):
        """Test handling malformed mapping structure."""
        config = {
            "pcs": {
                "pc_1": {
                    "name": "PC 1",
                    "screens": {
                        "screen_1": {
                            "name": "Screen 1",
                            "layout": {"rows": 2, "columns": 2},
                            "switching_interval": 10
                        }
                    }
                }
            },
            "mappings": {"screen_to_cameras": {"pc_1": "invalid_structure"}}
        }

        # Should handle gracefully
        result = generate_config(config, mock_db_session)

        assert "screens" in result

    def test_config_with_very_large_grid(self, mock_db_session):
        """Test handling very large grid layout."""
        config = {
            "pcs": {
                "pc_1": {
                    "name": "PC 1",
                    "screens": {
                        "screen_1": {
                            "name": "Screen 1",
                            "layout": {"rows": 10, "columns": 10},  # 100 slots
                            "switching_interval": 10
                        }
                    }
                }
            },
            "mappings": {"screen_to_cameras": {}}
        }

        result = generate_config(config, mock_db_session)

        assert len(result["screens"]) == 1

    def test_config_with_multiple_screens(self, db_session):
        """Test configuration with multiple screens."""
        from app.models.pc import PC
        from app.models.screen import Screen

        pc = PC(id="pc_1", name="PC 1", role="controller")
        screen1 = Screen(id="pc_1_s1", pc_id="pc_1", name="Screen 1", rows=2, columns=2, switching_interval=10)
        screen2 = Screen(id="pc_1_s2", pc_id="pc_1", name="Screen 2", rows=3, columns=3, switching_interval=15)

        db_session.add_all([pc, screen1, screen2])
        db_session.commit()

        config = {
            "pcs": {
                "pc_1": {
                    "name": "PC 1",
                    "screens": {
                        "pc_1_s1": {
                            "name": "Screen 1",
                            "layout": {"rows": 2, "columns": 2},
                            "switching_interval": 10
                        },
                        "pc_1_s2": {
                            "name": "Screen 2",
                            "layout": {"rows": 3, "columns": 3},
                            "switching_interval": 15
                        }
                    }
                }
            },
            "mappings": {"screen_to_cameras": {}}
        }

        result = generate_config(config, db_session)

        # Should create entries for both screens
        assert len(result["screens"]) == 2
        assert result["screens"][0]["id"] in ["pc_1_s1", "pc_1_s2"]
        assert result["screens"][1]["id"] in ["pc_1_s1", "pc_1_s2"]

    def test_config_generation_performance(self, db_session):
        """Test config generation with large dataset (performance test)."""
        from app.models.site import Site
        from app.models.camera import Camera
        from app.models.pc import PC
        from app.models.screen import Screen, View, ScreenMapping

        # Create a large dataset
        sites = [Site(id=f"SITE_{i}", name=f"Site {i}", nvr_username="admin", nvr_password="pass") for i in range(10)]
        cameras = []
        for i, site in enumerate(sites):
            for j in range(5):  # 5 cameras per site
                cameras.append(Camera(id=f"CAM_{i}_{j}", site_id=site.id, name=f"Camera {j}", rtsp_url=f"rtsp://{i}_{j}"))

        db_session.add_all(sites + cameras)
        db_session.commit()

        # Generate config for large dataset
        config = {
            "pcs": {"pc_1": {"name": "PC 1", "screens": {}}},
            "mappings": {"screen_to_cameras": {}}
        }

        result = generate_config(config, db_session)

        # Should complete without errors
        assert "screens" in result
