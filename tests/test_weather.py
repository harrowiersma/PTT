"""Tests for weather functionality (pure functions, no API calls)."""

import pytest

from server.weather_bot import degrees_to_compass, format_weather_report, WMO_CODES


def test_degrees_to_compass():
    assert degrees_to_compass(0) == "north"
    assert degrees_to_compass(90) == "east"
    assert degrees_to_compass(180) == "south"
    assert degrees_to_compass(270) == "west"
    assert degrees_to_compass(45) == "northeast"
    assert degrees_to_compass(315) == "northwest"
    assert degrees_to_compass(360) == "north"


def test_degrees_to_compass_approx():
    # Should pick closest
    assert degrees_to_compass(10) == "north"
    assert degrees_to_compass(80) == "east"
    assert degrees_to_compass(170) == "south"


def test_format_weather_report_basic():
    weather = {
        "current": {
            "temperature_2m": 15.3,
            "wind_speed_10m": 12.5,
            "wind_direction_10m": 270,
            "cloud_cover": 50,
            "precipitation": 0,
            "weather_code": 2,
        }
    }
    report = format_weather_report("harro", weather)
    assert "harro" in report
    assert "15 degrees" in report
    assert "west" in report
    assert "12 kilometers" in report or "13 kilometers" in report  # int(round(12.5)) is 12 in Python (banker's rounding)
    assert "50 percent" in report
    assert "Partly cloudy" in report
    assert "Report ends" in report


def test_format_weather_report_with_location():
    weather = {
        "current": {
            "temperature_2m": 20,
            "wind_speed_10m": 5,
            "wind_direction_10m": 0,
            "cloud_cover": 0,
            "precipitation": 0,
            "weather_code": 0,
        }
    }
    report = format_weather_report("user1", weather, location_name="Paris, France")
    assert "Paris, France" in report
    assert "user1" not in report  # Location name replaces username


def test_format_weather_report_with_precipitation():
    weather = {
        "current": {
            "temperature_2m": 8,
            "wind_speed_10m": 20,
            "wind_direction_10m": 180,
            "cloud_cover": 100,
            "precipitation": 2.5,
            "weather_code": 63,
        }
    }
    report = format_weather_report("test", weather)
    assert "2.5 millimeters" in report
    assert "Moderate rain" in report


def test_wmo_codes_coverage():
    # Ensure all common codes are mapped
    assert WMO_CODES[0] == "Clear sky"
    assert WMO_CODES[3] == "Overcast"
    assert WMO_CODES[61] == "Slight rain"
    assert WMO_CODES[71] == "Slight snow"
    assert WMO_CODES[95] == "Thunderstorm"
