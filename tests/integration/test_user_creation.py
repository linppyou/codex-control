import os
import pwd
import shutil
import subprocess

import pytest

import codexctl.users.sysusers as sysusers
from codexctl.users.sysusers import UserError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.geteuid() != 0 or shutil.which("useradd") is None or shutil.which("chpasswd") is None,
        reason="需要 root 与 useradd/chpasswd",
    ),
]

NAME = "cxcreate"
PW = "it-long-password"


def _userdel():
    subprocess.run(["userdel", "-r", NAME], capture_output=True, check=False)


@pytest.fixture
def clean_user():
    _userdel()
    yield
    _userdel()


def test_create_real_user_with_password(clean_user):
    user = sysusers.create_user(NAME, PW, min_uid=1000)
    assert user.name == NAME and user.uid >= 1000
    entry = pwd.getpwnam(NAME)
    assert entry.pw_dir == f"/home/{NAME}"
    assert os.path.isdir(entry.pw_dir)
    shadow = subprocess.run(["getent", "shadow", NAME], capture_output=True, text=True)
    assert shadow.returncode == 0, shadow.stderr
    password_hash = shadow.stdout.strip().split(":")[1]
    assert password_hash and password_hash[0] not in "!*"

    with pytest.raises(UserError, match="已存在"):
        sysusers.create_user(NAME, PW, min_uid=1000)
