"""Tests for Traccar client (pure functions)."""

import pytest

from server.traccar_client import TraccarClient


def test_haversine_distance_same_point():
    d = TraccarClient.haversine_distance(52.37, 4.89, 52.37, 4.89)
    assert d == 0


def test_haversine_distance_known():
    # Amsterdam to Rotterdam ~57km
    d = TraccarClient.haversine_distance(52.37, 4.89, 51.92, 4.48)
    assert 50000 < d < 65000  # Between 50km and 65km


def test_haversine_distance_antipodal():
    # North pole to south pole ~20,000 km
    d = TraccarClient.haversine_distance(90, 0, -90, 0)
    assert 19_900_000 < d < 20_100_000
