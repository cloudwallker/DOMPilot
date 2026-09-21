"""Shared live-browser fixtures for the local HTML site."""

import pytest

from tests.support import fixture_server


@pytest.fixture(scope="session")
def site():
    with fixture_server() as url:
        yield url


@pytest.fixture
def browser():
    from dompilot.browser import BrowserSession

    with BrowserSession(headless=True) as session:
        yield session
