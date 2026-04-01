"""Pytest configuration."""


def pytest_configure(config):
    config.addinivalue_line("markers", "network: tests that call Polymarket HTTP APIs")
