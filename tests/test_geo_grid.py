#!/usr/bin/env python3
"""
Tests for scripts/geo_grid.py — subdividing a city into a grid of search
circles so Lead Discovery can get real coverage of a large market past
Google Places Text Search's ~60-result-per-query cap.
"""

import os
import sys

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import geo_grid  # noqa: E402


# --- get_city_bounds() ---

def test_get_city_bounds_returns_bounds_for_valid_city(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": "OK",
                "results": [{
                    "geometry": {
                        "bounds": {
                            "northeast": {"lat": 40.9176, "lng": -73.7004},
                            "southwest": {"lat": 40.4774, "lng": -74.2591},
                        }
                    }
                }],
            }

    monkeypatch.setattr(geo_grid.requests, "get", lambda url, params=None, timeout=None: FakeResponse())
    bounds = geo_grid.get_city_bounds("New York, New York", "fake-key")
    assert bounds == {"north": 40.9176, "south": 40.4774, "east": -73.7004, "west": -74.2591}


def test_get_city_bounds_falls_back_to_viewport_when_no_bounds(monkeypatch):
    """A point-like geocode result (a specific address) only has
    'viewport', not 'bounds' — still usable, must not be treated as a
    total failure."""
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": "OK",
                "results": [{
                    "geometry": {
                        "viewport": {
                            "northeast": {"lat": 41.0, "lng": -73.5},
                            "southwest": {"lat": 40.4, "lng": -74.3},
                        }
                    }
                }],
            }

    monkeypatch.setattr(geo_grid.requests, "get", lambda url, params=None, timeout=None: FakeResponse())
    bounds = geo_grid.get_city_bounds("Some Address", "fake-key")
    assert bounds is not None
    assert bounds["north"] == 41.0


def test_get_city_bounds_none_on_unknown_place(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "ZERO_RESULTS", "results": []}

    monkeypatch.setattr(geo_grid.requests, "get", lambda url, params=None, timeout=None: FakeResponse())
    assert geo_grid.get_city_bounds("Nonexistent Place XYZ", "fake-key") is None


def test_get_city_bounds_none_on_network_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise geo_grid.requests.exceptions.ConnectionError("simulated")

    monkeypatch.setattr(geo_grid.requests, "get", fake_get)
    assert geo_grid.get_city_bounds("New York, New York", "fake-key") is None


# --- generate_grid_points() ---

def test_generate_grid_points_covers_small_bounds_with_at_least_one_point():
    bounds = {"north": 40.42, "south": 40.40, "east": -74.00, "west": -74.02}
    points = geo_grid.generate_grid_points(bounds, cell_radius_meters=3000)
    assert len(points) >= 1
    for lat, lng in points:
        assert bounds["south"] <= lat <= bounds["north"] + 0.1
        assert bounds["west"] <= lng <= bounds["east"] + 0.1


def test_generate_grid_points_produces_more_points_for_larger_area():
    small_bounds = {"north": 40.42, "south": 40.40, "east": -74.00, "west": -74.02}
    large_bounds = {"north": 40.9176, "south": 40.4774, "east": -73.7004, "west": -74.2591}
    small_points = geo_grid.generate_grid_points(small_bounds, cell_radius_meters=3000)
    large_points = geo_grid.generate_grid_points(large_bounds, cell_radius_meters=3000)
    assert len(large_points) > len(small_points)


def test_generate_grid_points_smaller_radius_yields_more_points():
    bounds = {"north": 40.9176, "south": 40.4774, "east": -73.7004, "west": -74.2591}
    coarse = geo_grid.generate_grid_points(bounds, cell_radius_meters=6000)
    fine = geo_grid.generate_grid_points(bounds, cell_radius_meters=1500)
    assert len(fine) > len(coarse)


# --- cell_to_rectangle(): the shape Text Search's locationRestriction
# actually accepts, added 2026-09-01 after a real 400 INVALID_ARGUMENT
# ("Unknown name 'circle' at 'location_restriction'") against the live
# API — circle is only valid for the separate, soft locationBias field.

def test_cell_to_rectangle_center_lies_inside_the_box():
    rect = geo_grid.cell_to_rectangle(40.7, -74.0, 3000)
    assert rect["low"]["latitude"] < 40.7 < rect["high"]["latitude"]
    assert rect["low"]["longitude"] < -74.0 < rect["high"]["longitude"]


def test_cell_to_rectangle_has_the_expected_low_high_shape():
    rect = geo_grid.cell_to_rectangle(40.7, -74.0, 3000)
    assert set(rect.keys()) == {"low", "high"}
    assert set(rect["low"].keys()) == {"latitude", "longitude"}
    assert set(rect["high"].keys()) == {"latitude", "longitude"}


def test_cell_to_rectangle_larger_radius_yields_larger_box():
    small = geo_grid.cell_to_rectangle(40.7, -74.0, 1000)
    large = geo_grid.cell_to_rectangle(40.7, -74.0, 5000)
    small_width = small["high"]["longitude"] - small["low"]["longitude"]
    large_width = large["high"]["longitude"] - large["low"]["longitude"]
    assert large_width > small_width


def test_generate_grid_points_all_within_bounds():
    """No grid point should land meaningfully outside the city's own
    geocoded extent — the whole point is bounded metro coverage, not an
    unbounded search radius."""
    bounds = {"north": 40.42, "south": 40.40, "east": -74.00, "west": -74.02}
    points = geo_grid.generate_grid_points(bounds, cell_radius_meters=1000)
    for lat, lng in points:
        assert lat >= bounds["south"]
        assert lng >= bounds["west"]
