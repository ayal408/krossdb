"""Unit tests for the weighted round-robin replica load balancer."""

from __future__ import annotations

from collections import Counter

import pytest

from krossdb.adapters.relational.routing import LoadBalancer
from krossdb.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


def test_single_key_always_returns_itself():
    balancer = LoadBalancer(["a"], [1])

    assert [balancer.next() for _ in range(5)] == ["a"] * 5


def test_equal_weights_cycle_evenly():
    balancer = LoadBalancer(["a", "b"], [1, 1])

    picks = [balancer.next() for _ in range(6)]

    assert picks == ["a", "b", "a", "b", "a", "b"]


def test_weighted_distribution_matches_configured_ratio():
    balancer = LoadBalancer(["a", "b"], [3, 1])

    picks = [balancer.next() for _ in range(8)]  # two full cycles of length 4
    counts = Counter(picks)

    assert counts["a"] == 6
    assert counts["b"] == 2


def test_mismatched_keys_and_weights_raises():
    with pytest.raises(ConfigurationError):
        LoadBalancer(["a", "b"], [1])


def test_empty_keys_raises():
    with pytest.raises(ConfigurationError):
        LoadBalancer([], [])


def test_zero_weight_raises():
    with pytest.raises(ConfigurationError):
        LoadBalancer(["a"], [0])


def test_keys_property_returns_unique_keys_in_first_seen_order():
    balancer = LoadBalancer(["a", "b", "a"], [2, 1, 2])

    assert balancer.keys == ["a", "b"]
