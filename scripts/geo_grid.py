#!/usr/bin/env python3
"""
City-to-grid conversion — SiteScout (2026-08-31)

Google's Places API (New) Text Search caps out around 60 results for a
single broad "niche in city" query, regardless of how many real
businesses exist in that market — a documented API limitation, not
something reachable by paginating harder. This module subdivides a
city's real geographic extent into a grid of small search circles so
Lead Discovery can issue one Text Search per cell (each with its own
~60-result ceiling) and accumulate real coverage across a whole metro
area, instead of stopping at one query's cap.

Uses the Geocoding API once per city to get its real viewport bounds,
then covers that bounding box with non-overlapping circles of a
configurable radius. Deliberately bounded to the city's own geocoded
extent — Cyril's explicit call (2026-08-31): stop once the whole metro
grid has been covered, never expand the search radius outward past
that looking for more leads.
"""

import math

import requests

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Meters per degree of latitude is constant everywhere; longitude varies
# with latitude (converges toward the poles), so it's computed per-city
# using the city's own center latitude rather than assumed as a constant.
METERS_PER_DEGREE_LATITUDE = 111320


def get_city_bounds(location, api_key):
    """Returns {"north", "south", "east", "west"} (degrees) for the given
    location string, or None if geocoding failed (unknown place, network
    error, no API key) — callers should fall back to a single ungridded
    search rather than fail the whole run over one geocoding miss."""
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"address": location, "key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if data.get("status") != "OK" or not data.get("results"):
        return None

    geometry = data["results"][0]["geometry"]
    # "bounds" (a real viewport rectangle) is only present when Google
    # actually knows the place's extent (a city, a region). A point-like
    # result (a specific address) only has "viewport", which is still
    # usable but tighter — fall back to it rather than fail.
    box = geometry.get("bounds") or geometry.get("viewport")
    if not box:
        return None

    return {
        "north": box["northeast"]["lat"],
        "south": box["southwest"]["lat"],
        "east": box["northeast"]["lng"],
        "west": box["southwest"]["lng"],
    }


def generate_grid_points(bounds, cell_radius_meters):
    """Covers `bounds` with a grid of (lat, lng) circle centers of the
    given radius, packed so adjacent circles just touch (spacing =
    2 * radius) — dense enough for full coverage without wastefully
    overlapping. Returns a list of (lat, lng) tuples, south-to-north,
    west-to-east. Longitude spacing is computed from the bounding box's
    own center latitude, since a degree of longitude covers less real
    distance the further from the equator a city is."""
    lat_step_degrees = (2 * cell_radius_meters) / METERS_PER_DEGREE_LATITUDE

    center_lat = (bounds["north"] + bounds["south"]) / 2
    meters_per_degree_longitude = METERS_PER_DEGREE_LATITUDE * math.cos(math.radians(center_lat))
    # A city exactly at the pole would divide by ~0 — not a real case for
    # this project's US-city inputs, but guard against a degenerate grid.
    meters_per_degree_longitude = max(meters_per_degree_longitude, 1)
    lng_step_degrees = (2 * cell_radius_meters) / meters_per_degree_longitude

    points = []
    lat = bounds["south"] + lat_step_degrees / 2
    while lat <= bounds["north"]:
        lng = bounds["west"] + lng_step_degrees / 2
        while lng <= bounds["east"]:
            points.append((lat, lng))
            lng += lng_step_degrees
        lat += lat_step_degrees

    # A city/area smaller than a single cell (common for a small town)
    # must still search once, centered on the bounds — never return zero
    # points for a real, valid geocoded area.
    if not points:
        points.append(((bounds["north"] + bounds["south"]) / 2, (bounds["east"] + bounds["west"]) / 2))

    return points


def cell_to_rectangle(lat, lng, radius_meters):
    """Converts a grid cell (center + radius) into the low/high lat-lng
    box Places API (New) Text Search's locationRestriction actually
    accepts. Found live 2026-08-31: locationRestriction only supports a
    "rectangle" shape for Text Search — "circle" is valid for the
    separate, soft locationBias field, but a 400 INVALID_ARGUMENT
    ("Unknown name 'circle' at 'location_restriction'") for a hard
    restriction. A rectangle is used instead of switching to the softer
    locationBias specifically because bias doesn't guarantee results stay
    within the cell — the whole point of grid search is genuine coverage
    of every cell, not an approximate preference Google can ignore."""
    lat_delta = radius_meters / METERS_PER_DEGREE_LATITUDE
    meters_per_degree_longitude = max(METERS_PER_DEGREE_LATITUDE * math.cos(math.radians(lat)), 1)
    lng_delta = radius_meters / meters_per_degree_longitude
    return {
        "low": {"latitude": lat - lat_delta, "longitude": lng - lng_delta},
        "high": {"latitude": lat + lat_delta, "longitude": lng + lng_delta},
    }
