from pathlib import Path

import pytest

from incubator.core.lock import LockManager


@pytest.fixture
def lm(tmp_path: Path) -> LockManager:
    return LockManager(lock_dir=tmp_path / "locks")


def test_acquire_and_release(lm: LockManager):
    assert lm.acquire("test", "lock1", executor="pytest")
    assert lm.is_locked("test", "lock1")
    lm.release("test", "lock1")
    assert not lm.is_locked("test", "lock1")


def test_double_acquire_fails(lm: LockManager):
    assert lm.acquire("test", "lock1")
    assert not lm.acquire("test", "lock1")


def test_lock_data(lm: LockManager):
    lm.acquire("test", "lock1", executor="pytest")
    data = lm.get_lock_data("test", "lock1")
    assert data is not None
    assert data["namespace"] == "test"
    assert data["executor"] == "pytest"
    assert "pid" in data


def test_separate_namespaces(lm: LockManager):
    assert lm.acquire("ns1", "lock1")
    assert lm.acquire("ns2", "lock1")  # Different namespace, should succeed


def test_nonexistent_lock_data(lm: LockManager):
    assert lm.get_lock_data("test", "nonexistent") is None
    assert not lm.is_locked("test", "nonexistent")
