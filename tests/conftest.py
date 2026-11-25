import pytest

from aidlc_g.testkit import World


@pytest.fixture
def world() -> World:
    return World()
