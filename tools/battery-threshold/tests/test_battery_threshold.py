import importlib.util
import logging
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "battery_threshold", Path(__file__).parent.parent / "battery_threshold.py"
)
bt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bt  # dataclasses look the module up by name
_spec.loader.exec_module(bt)


def make_sysfs(root: Path, capacity: int = 80, mode: str = "Long_Life", ac: str = "1") -> Path:
    bat = root / "BAT0"
    bat.mkdir(parents=True)
    (bat / "type").write_text("Battery\n")
    (bat / "capacity").write_text(f"{capacity}\n")
    (bat / "status").write_text("Not charging\n")
    choices = " ".join(f"[{m}]" if m == mode else m for m in ("Fast", "Standard", "Long_Life"))
    (bat / "charge_types").write_text(choices + "\n")
    adp = root / "ADP0"
    adp.mkdir()
    (adp / "type").write_text("Mains\n")
    (adp / "online").write_text(f"{ac}\n")
    return root


@pytest.mark.parametrize(
    ("text", "active"),
    [
        ("Fast Standard [Long_Life]", "Long_Life"),
        ("[Standard] Long_Life\n", "Standard"),
    ],
)
def test_parse_charge_types(text, active):
    mode, modes = bt.parse_charge_types(text)
    assert mode == active
    assert active in modes


def test_parse_charge_types_without_active_raises():
    with pytest.raises(bt.BatteryError):
        bt.parse_charge_types("Fast Standard Long_Life")


@pytest.mark.parametrize(
    ("capacity", "current", "target", "expected"),
    [
        (50, "Standard", 80, "Long_Life"),  # 80 is the firmware limit itself
        (99, "Long_Life", 100, "Standard"),  # 100 means never stop
        (85, "Standard", 85, "Long_Life"),  # reached target
        (90, "Standard", 85, "Long_Life"),  # above target
        (83, "Long_Life", 85, "Standard"),  # fell below hysteresis band
        (84, "Long_Life", 85, "Long_Life"),  # inside band: keep holding
        (84, "Standard", 85, "Standard"),  # inside band: keep charging
        (84, "Fast", 85, "Long_Life"),  # unknown current mode inside band
    ],
)
def test_decide_mode(capacity, current, target, expected):
    assert bt.decide_mode(capacity, current, target, hysteresis=2) == expected


def test_status_reads_fake_sysfs(tmp_path):
    s = bt.Battery(make_sysfs(tmp_path, capacity=77)).status()
    assert s.capacity == 77
    assert s.mode == "Long_Life"
    assert s.ac_online is True


def test_ac_offline(tmp_path):
    assert bt.Battery(make_sysfs(tmp_path, ac="0")).ac_online() is False


def test_missing_charge_types_raises(tmp_path):
    (tmp_path / "BAT0").mkdir()
    with pytest.raises(bt.BatteryError, match="charge_types"):
        bt.Battery(tmp_path)


def test_set_mode_writes_only_on_change(tmp_path):
    battery = bt.Battery(make_sysfs(tmp_path, mode="Long_Life"))
    assert battery.set_mode("Long_Life") is False
    assert battery.set_mode("Standard") is True
    assert (tmp_path / "BAT0" / "charge_types").read_text() == "Standard"


def test_set_mode_dry_run_does_not_write(tmp_path):
    battery = bt.Battery(make_sysfs(tmp_path))
    before = (tmp_path / "BAT0" / "charge_types").read_text()
    assert battery.set_mode("Standard", dry_run=True) is True
    assert (tmp_path / "BAT0" / "charge_types").read_text() == before


def test_set_mode_rejects_unsupported(tmp_path):
    with pytest.raises(bt.BatteryError, match="not supported"):
        bt.Battery(make_sysfs(tmp_path)).set_mode("Turbo")


def test_hold_once_dry_run(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    root = make_sysfs(tmp_path, capacity=82, mode="Long_Life")
    rc = bt.main(["--sysfs-root", str(root), "hold", "--target", "90", "--once", "--dry-run"])
    assert rc == 0
    assert "would change mode Long_Life -> Standard" in caplog.text
    assert "[Long_Life]" in (root / "BAT0" / "charge_types").read_text()


@pytest.mark.parametrize("target", ["79", "101", "50"])
def test_target_out_of_range_rejected(tmp_path, target):
    root = make_sysfs(tmp_path)
    with pytest.raises(SystemExit):
        bt.main(["--sysfs-root", str(root), "hold", "--target", target, "--once"])


def test_mode_without_root_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(bt.os, "geteuid", lambda: 1000)
    assert bt.main(["--sysfs-root", str(make_sysfs(tmp_path)), "mode", "standard"]) == 1
