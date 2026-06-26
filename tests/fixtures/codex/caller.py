"""Caller module — calls functions from callee.py."""

from callee import add, Calculator


def run(x, y):
    """Run a computation using callee functions."""
    result = add(x, y)
    calc = Calculator()
    return calc.compute(result, x)


class Runner:
    """Runner class that orchestrates calls."""

    def execute(self, x, y):
        return run(x, y)
