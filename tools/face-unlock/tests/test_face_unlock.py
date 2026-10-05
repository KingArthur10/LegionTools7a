import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "face_unlock", Path(__file__).parent.parent / "face_unlock.py"
)
fu = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fu
_spec.loader.exec_module(fu)

SUDO = """#%PAM-1.0
auth       include      system-auth
account    include      system-auth
session    optional     pam_keyinit.so revoke
"""

POLKIT_VENDOR = """#%PAM-1.0

auth       include      system-auth
account    include      system-auth
"""


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    etc, vendor, backup = tmp_path / "etc", tmp_path / "vendor", tmp_path / "backup"
    etc.mkdir()
    vendor.mkdir()
    (etc / "sudo").write_text(SUDO)
    (etc / "kde").write_text("auth substack password-auth\n")
    (vendor / "polkit-1").write_text(POLKIT_VENDOR)
    monkeypatch.setattr(fu.os, "geteuid", lambda: 0)
    return etc, vendor, backup


def run(dirs, *args):
    etc, vendor, backup = dirs
    common = ["--etc-dir", str(etc), "--vendor-dir", str(vendor), "--backup-dir", str(backup)]
    return fu.main([*common, *args])


def test_add_inserts_before_first_auth_rule():
    lines = fu.add_howdy(SUDO).splitlines()
    assert lines[0] == "#%PAM-1.0"
    assert lines[1] == fu.MARKER
    assert lines[2] == fu.HOWDY_LINE
    assert lines[3].startswith("auth")


def test_add_is_idempotent():
    once = fu.add_howdy(SUDO)
    assert fu.add_howdy(once) == once


def test_add_respects_existing_manual_rule():
    text = "auth sufficient pam_howdy.so\n" + SUDO
    assert fu.add_howdy(text) == text


def test_commented_rule_does_not_count():
    assert not fu.has_howdy("# auth sufficient pam_howdy.so\n" + SUDO)


def test_add_without_auth_rules_refuses():
    with pytest.raises(fu.PamError):
        fu.add_howdy("account include system-auth\n")


def test_remove_round_trips():
    assert fu.remove_howdy(fu.add_howdy(SUDO)) == SUDO


def test_dash_auth_counts_as_auth_rule():
    text = "-auth optional pam_foo.so\nauth include system-auth\n"
    assert fu.add_howdy(text).splitlines()[1] == fu.HOWDY_LINE


def test_enable_and_disable_etc_service(dirs):
    etc, _, backup = dirs
    assert run(dirs, "enable", "sudo") == 0
    assert fu.HOWDY_LINE in (etc / "sudo").read_text()
    assert (backup / "sudo.orig").read_text() == SUDO
    assert run(dirs, "disable", "sudo") == 0
    assert (etc / "sudo").read_text() == SUDO


def test_enable_vendor_service_copies_then_disable_removes_copy(dirs):
    etc, vendor, _ = dirs
    assert run(dirs, "enable", "polkit-1") == 0
    assert fu.HOWDY_LINE in (etc / "polkit-1").read_text()
    assert (vendor / "polkit-1").read_text() == POLKIT_VENDOR  # vendor file untouched
    assert run(dirs, "disable", "polkit-1") == 0
    assert not (etc / "polkit-1").exists()


def test_disable_keeps_locally_customised_copy(dirs):
    etc, _, _ = dirs
    run(dirs, "enable", "polkit-1")
    custom = (etc / "polkit-1").read_text() + "session include system-auth\n"
    (etc / "polkit-1").write_text(custom)
    run(dirs, "disable", "polkit-1")
    assert (etc / "polkit-1").exists()
    assert fu.HOWDY_LINE not in (etc / "polkit-1").read_text()


def test_enable_all_by_default(dirs):
    etc, _, _ = dirs
    assert run(dirs, "enable") == 0
    for name in fu.SERVICES:
        assert fu.has_howdy((etc / name).read_text())


def test_dry_run_changes_nothing(dirs, capsys):
    etc, _, backup = dirs
    assert run(dirs, "enable", "--dry-run") == 0
    assert (etc / "sudo").read_text() == SUDO
    assert not (etc / "polkit-1").exists()
    assert not backup.exists()
    assert "+" + fu.HOWDY_LINE in capsys.readouterr().out


def test_no_temp_files_left_behind(dirs):
    etc, _, _ = dirs
    run(dirs, "enable")
    assert sorted(p.name for p in etc.iterdir()) == ["kde", "polkit-1", "sudo"]


def test_unknown_service_rejected(dirs):
    with pytest.raises(SystemExit):
        run(dirs, "enable", "login")


def test_requires_root(dirs, monkeypatch):
    monkeypatch.setattr(fu.os, "geteuid", lambda: 1000)
    assert run(dirs, "enable", "sudo") == 1
