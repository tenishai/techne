"""Callee module — functions that are called by caller.py."""


def add(a, b):
    """Return a + b."""
    return a + b


def multiply(a, b):
    """Return a * b."""
    return a * b


class Calculator:
    """Simple calculator class."""

    def compute(self, a, b):
        return add(a, b)
