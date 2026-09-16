"""Weighted round-robin load balancer used for read-replica routing."""

from __future__ import annotations

import itertools
import threading

from krossdb.core.exceptions import ConfigurationError


class LoadBalancer:
    """Weighted round-robin selection over a fixed set of keys.

    Higher ``weight`` means a key is chosen proportionally more often. Not
    tied to SQLAlchemy or any adapter — it just hands back the next key in
    the expanded, shuffled-free weighted sequence.
    """

    def __init__(self, keys: list[str], weights: list[int]) -> None:
        if len(keys) != len(weights):
            raise ConfigurationError("keys and weights must be the same length")
        if not keys:
            raise ConfigurationError("LoadBalancer requires at least one key")
        if any(w < 1 for w in weights):
            raise ConfigurationError("weights must all be >= 1")

        expanded: list[str] = []
        for key, weight in zip(keys, weights, strict=True):
            expanded.extend([key] * weight)

        self._sequence = expanded
        self._cycle = itertools.cycle(expanded)
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            return next(self._cycle)

    @property
    def keys(self) -> list[str]:
        return list(dict.fromkeys(self._sequence))


__all__ = ["LoadBalancer"]
