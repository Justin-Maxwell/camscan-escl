"""Config loading, including the example file shipped in the repo."""

from pathlib import Path

import pytest

from camscan_escl import config as config_mod

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_missing_file_yields_defaults():
    cfg = config_mod.load(Path("/nonexistent/camscan.toml"))
    assert cfg.server.port == 8090
    assert cfg.rig.coverage_mm == (210.0, 297.0)
    assert cfg.source_path is None


def test_example_config_parses_and_validates():
    cfg = config_mod.load(EXAMPLE)
    assert cfg.server.port == 8090
    assert cfg.capture.native_width == 2304
    assert cfg.capture.focus.disable_autofocus is False
    assert cfg.rig.coverage_mm == (210.0, 297.0)
    assert cfg.source_path == EXAMPLE


def test_nested_focus_table_is_read(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[capture]\ncommand = "true %f"\n[capture.focus]\nabsolute = 77\n'
    )
    assert config_mod.load(path).capture.focus.absolute == 77


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[server]\nport = 9000\nfuture_option = "x"\n')
    assert config_mod.load(path).server.port == 9000


@pytest.mark.parametrize(
    "body",
    [
        '[capture]\ncommand = "fswebcam out.jpg"\n',   # no %f
        "[rig]\ncoverage_mm = [0.0, 297.0]\n",          # zero coverage
        '[capture]\ncommand = "true %f"\nrotate_deg = 45\n',
        "[scanner]\njpeg_quality = 0\n",
    ],
)
def test_invalid_configs_are_rejected(tmp_path, body):
    path = tmp_path / "c.toml"
    path.write_text(body)
    with pytest.raises(ValueError):
        config_mod.load(path)


def test_exposure_lock_is_off_by_default_and_parses_when_set(tmp_path):
    # A wrong pinned exposure is worse than a variable one, so the shipped
    # default must stay off; but when set it has to reach CaptureConfig.
    assert config_mod.Config().capture.exposure.lock is False
    assert config_mod.Config().capture.exposure.time_absolute is None

    path = tmp_path / "config.toml"
    path.write_text(
        "[capture.exposure]\n"
        "lock = true\n"
        "time_absolute = 120\n"
        "white_balance_temperature = 4000\n"
    )
    exposure = config_mod.load(path).capture.exposure
    assert exposure.lock is True
    assert exposure.time_absolute == 120
    assert exposure.white_balance_temperature == 4000


def test_autofocus_is_the_default():
    # A wrong fixed focus is unconditionally bad; autofocus hunting is only a
    # risk. The value that used to ship here was measurably softer than what
    # the camera picks for itself, and nothing had ever checked.
    assert config_mod.Config().capture.focus.disable_autofocus is False
