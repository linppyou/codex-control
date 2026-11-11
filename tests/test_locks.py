import pytest

from codexctl.locks import LockBusy, account_lock


def test_second_nonblocking_lock_raises(tmp_path):
    with account_lock(tmp_path, "acc1"):
        with pytest.raises(LockBusy):
            with account_lock(tmp_path, "acc1", blocking=False):
                pass
    with account_lock(tmp_path, "acc1", blocking=False):
        pass
    assert oct((tmp_path / "locks").stat().st_mode & 0o777) == "0o700"
