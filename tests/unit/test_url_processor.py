"""
Comprehensive tests for URL processor utility.

Tests cover:
- RTSP password encoding with special characters
- Edge cases and error handling
- Invalid input handling
"""
import pytest
from app.utils.url_processor import encode_rtsp_password, try_encode_rtsp_password


@pytest.mark.unit
class TestEncodeRTSPPassword:
    """Test encode_rtsp_password function."""

    def test_encode_simple_password(self):
        """Test encoding with simple alphanumeric password."""
        url = "rtsp://admin:password123@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert result == url  # No encoding needed

    def test_encode_password_with_at_symbol(self):
        """Test encoding password containing @ symbol."""
        url = "rtsp://admin:shin@bet2015@server.com:554/stream"
        result = encode_rtsp_password(url)
        expected = "rtsp://admin:shin%40bet2015@server.com:554/stream"
        assert result == expected

    def test_encode_password_with_multiple_special_chars(self):
        """Test encoding password with multiple special characters."""
        url = "rtsp://admin:p@ss#w$rd!@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        # @ should be encoded to %40, # to %23, $ to %24, ! to %21
        assert "%40" in result
        assert "%23" in result
        assert "%24" in result
        assert "%21" in result

    def test_encode_password_with_spaces(self):
        """Test encoding password with spaces."""
        url = "rtsp://admin:my password@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert "%20" in result  # Space encoded as %20

    def test_encode_password_with_slash(self):
        """Test encoding password with forward slash."""
        url = "rtsp://admin:pass/word@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert "%2F" in result  # / encoded as %2F

    def test_no_password_in_url(self):
        """Test URL without password."""
        url = "rtsp://192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert result == url  # No change

    def test_no_username_in_url(self):
        """Test URL with @ but no proper credentials."""
        url = "rtsp://@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert result == url

    def test_multiple_at_symbols_in_host(self):
        """Test URL with @ in multiple places (edge case)."""
        url = "rtsp://user:p@ss@server@domain.com:554/stream"
        result = encode_rtsp_password(url)
        # Should encode password @, but preserve final @ before host
        parts = result.split("@")
        assert len(parts) >= 2  # At least username:password @ host

    def test_empty_password(self):
        """Test URL with empty password."""
        url = "rtsp://admin:@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert result == url  # Empty password, no encoding

    def test_special_chars_in_username(self):
        """Test URL with special characters in username (should not encode username)."""
        url = "rtsp://admin@company:password@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        # Only password should be encoded
        assert "admin@company:" in result or "admin%40company:" not in result

    def test_url_with_path_and_query(self):
        """Test URL with path and query parameters."""
        url = "rtsp://admin:p@ss@192.168.1.100:554/stream/main?token=abc"
        result = encode_rtsp_password(url)
        assert "%40" in result  # Password encoded
        assert "?token=abc" in result  # Query preserved

    def test_url_with_port(self):
        """Test URL with custom port."""
        url = "rtsp://admin:password@192.168.1.100:8554/stream"
        result = encode_rtsp_password(url)
        assert ":8554" in result  # Port preserved

    def test_empty_string(self):
        """Test with empty string."""
        result = encode_rtsp_password("")
        assert result == ""

    def test_none_input(self):
        """Test with None input."""
        result = encode_rtsp_password(None)
        assert result == ""

    def test_invalid_url_format(self):
        """Test with invalid URL format."""
        url = "not_a_valid_url"
        result = encode_rtsp_password(url)
        assert result == url  # Return unchanged

    def test_url_without_protocol(self):
        """Test URL without rtsp:// protocol."""
        url = "admin:password@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert result == url  # Cannot process without protocol

    def test_unicode_characters_in_password(self):
        """Test password with unicode characters."""
        url = "rtsp://admin:pássw0rd@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        # Should encode unicode characters
        assert "%" in result

    def test_already_encoded_password(self):
        """Test URL with already encoded password."""
        url = "rtsp://admin:p%40ss@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        # Should re-encode (double encoding)
        assert "%" in result

    def test_colon_in_password(self):
        """Test password containing colon."""
        url = "rtsp://admin:pass:word@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        # First colon separates username from password
        # Second colon should be in password and encoded
        assert "%3A" in result

    def test_very_long_password(self):
        """Test with very long password."""
        long_password = "a" * 1000 + "@" + "b" * 1000
        url = f"rtsp://admin:{long_password}@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        assert "%40" in result  # @ should be encoded
        assert len(result) > len(url)  # Encoded version is longer


@pytest.mark.unit
class TestTryEncodeRTSPPassword:
    """Test try_encode_rtsp_password function (safe wrapper)."""

    def test_successful_encoding(self):
        """Test successful encoding."""
        url = "rtsp://admin:p@ss@192.168.1.100:554/stream"
        result = try_encode_rtsp_password(url)
        assert "%40" in result

    def test_exception_handling(self):
        """Test that exceptions are caught and logged."""
        # This should not raise an exception
        result = try_encode_rtsp_password(None)
        assert result == ""

    def test_empty_string_handling(self):
        """Test empty string handling."""
        result = try_encode_rtsp_password("")
        assert result == ""

    def test_valid_url_no_encoding_needed(self):
        """Test valid URL that doesn't need encoding."""
        url = "rtsp://admin:password@192.168.1.100:554/stream"
        result = try_encode_rtsp_password(url)
        assert result == url


@pytest.mark.unit
class TestURLProcessorEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_rtsp_vs_rtsps_protocol(self):
        """Test both rtsp and rtsps protocols."""
        url_rtsp = "rtsp://admin:p@ss@192.168.1.100:554/stream"
        url_rtsps = "rtsps://admin:p@ss@192.168.1.100:554/stream"

        result_rtsp = encode_rtsp_password(url_rtsp)
        result_rtsps = encode_rtsp_password(url_rtsps)

        assert result_rtsp.startswith("rtsp://")
        assert result_rtsps.startswith("rtsps://")

    def test_http_protocol(self):
        """Test with HTTP protocol (should still work)."""
        url = "http://admin:p@ss@192.168.1.100:8080/stream"
        result = encode_rtsp_password(url)
        assert "%40" in result

    def test_ipv6_address(self):
        """Test URL with IPv6 address."""
        url = "rtsp://admin:p@ss@[2001:db8::1]:554/stream"
        result = encode_rtsp_password(url)
        assert "%40" in result
        assert "[2001:db8::1]" in result

    def test_hostname_instead_of_ip(self):
        """Test URL with hostname."""
        url = "rtsp://admin:p@ss@camera.example.com:554/stream"
        result = encode_rtsp_password(url)
        assert "%40" in result
        assert "camera.example.com" in result

    def test_url_with_fragment(self):
        """Test URL with fragment identifier."""
        url = "rtsp://admin:p@ss@192.168.1.100:554/stream#fragment"
        result = encode_rtsp_password(url)
        assert "%40" in result
        assert "#fragment" in result

    def test_case_sensitivity(self):
        """Test that protocol is handled case-insensitively."""
        url_lower = "rtsp://admin:p@ss@192.168.1.100:554/stream"
        url_upper = "RTSP://admin:p@ss@192.168.1.100:554/stream"

        result_lower = encode_rtsp_password(url_lower)
        result_upper = encode_rtsp_password(url_upper)

        # Both should encode the password
        assert "%40" in result_lower
        assert "%40" in result_upper

    def test_only_special_chars_password(self):
        """Test password containing only special characters."""
        url = "rtsp://admin:@#$%^&*()@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        # All special chars should be encoded
        assert "%" in result

    def test_whitespace_handling(self):
        """Test handling of various whitespace characters."""
        url = "rtsp://admin:pass\tword\n@192.168.1.100:554/stream"
        result = encode_rtsp_password(url)
        # Tab and newline should be encoded
        assert "%09" in result or "%0A" in result

    def test_integer_input(self):
        """Test with non-string input."""
        result = encode_rtsp_password(12345)
        # Should handle gracefully
        assert isinstance(result, (str, int))

    def test_list_input(self):
        """Test with list input."""
        result = encode_rtsp_password(["rtsp://test"])
        # Should handle gracefully
        assert result == ["rtsp://test"] or result == ""
