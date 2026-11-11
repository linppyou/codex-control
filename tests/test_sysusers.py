import os
import pwd

import pytest

from codexctl.sysusers import UserError, list_users, lookup_user

ME = pwd.getpwuid(os.getuid())


def test_lookup_current_user():
    u = lookup_user(ME.pw_name, min_uid=0)
    assert u.uid == os.getuid()
    assert str(u.home) == ME.pw_dir


def test_rejects_uid_below_minimum():
    with pytest.raises(UserError, match="不在允许范围内"):
        lookup_user(ME.pw_name, min_uid=ME.pw_uid + 1)


def test_rejects_bad_name():
    with pytest.raises(UserError, match="非法用户名"):
        lookup_user("../etc", min_uid=0)


def test_rejects_missing_user():
    with pytest.raises(UserError, match="不存在"):
        lookup_user("no_such_user_cx", min_uid=0)


def test_list_users_includes_current_user():
    assert ME.pw_name in {u.name for u in list_users(0)}


def test_rejects_trailing_newline_name():
    with pytest.raises(UserError, match="非法用户名"):
        lookup_user(ME.pw_name + "\n", min_uid=0)
