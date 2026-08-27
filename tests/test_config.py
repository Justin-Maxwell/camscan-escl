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


def test_adjustments_round_trip_and_override_the_toml(tmp_path):
    # The GUI's settings are written separately from the hand-authored TOML:
    # generated output must never overwrite a file whose comments carry the
    # reasoning.
    from dataclasses import replace

    cfg = replace(config_mod.Config(),
                  rig=config_mod.RigConfig(coverage_mm=(301.0, 402.0)))
    path = tmp_path / "adjustments.json"
    config_mod.save_adjustments(cfg, path)

    loaded = config_mod.load_adjustments(config_mod.Config(), path)
    assert loaded.rig.coverage_mm == (301.0, 402.0)


def test_corrupt_adjustments_do_not_stop_the_daemon(tmp_path):
    # A calibration file is not worth refusing to scan over.
    path = tmp_path / "adjustments.json"
    path.write_text("{ this is not json")
    cfg = config_mod.load_adjustments(config_mod.Config(), path)
    assert cfg.rig.coverage_mm == config_mod.RigConfig().coverage_mm


def test_apply_adjustments_ignores_unknown_keys(tmp_path):
    cfg = config_mod.apply_adjustments(config_mod.Config(),
                                       {"nonsense": 1, "coverage_mm": [200, 300]})
    assert cfg.rig.coverage_mm == (200.0, 300.0)


def test_mismatched_coverage_aspect_is_warned_about(caplog):
    # Silent failure mode: the units contract is still satisfied, the
    # dimensions are still right, and every scan is stretched. Measured at
    # 2.05x on the development rig before anyone noticed.
    import logging
    from dataclasses import replace

    cfg = replace(config_mod.Config(),
                  rig=replace(config_mod.Config().rig, coverage_mm=(245.8, 336.4)))
    with caplog.at_level(logging.WARNING):
        config_mod.validate(cfg)
    assert any("stretched" in r.getMessage() for r in caplog.records)


def test_matching_coverage_aspect_is_quiet(caplog):
    import logging
    from dataclasses import replace

    # 2304x1536 is 3:2, so a 3:2 coverage is correct.
    cfg = replace(config_mod.Config(),
                  rig=replace(config_mod.Config().rig, coverage_mm=(300.0, 200.0)))
    with caplog.at_level(logging.WARNING):
        config_mod.validate(cfg)
    assert not [r for r in caplog.records if "stretched" in r.getMessage()]


def test_landscape_marks_do_not_change_the_required_coverage_shape(caplog):
    # The camera sees the same physical area whichever way the marks are
    # drawn, so preview.landscape must not enter the aspect rule. It did, and
    # made the coverage portrait-shaped against a landscape frame: a 2.25x
    # stretch on every scan, with the GUI reporting no problem because it had
    # made the same mistake. Only a physically turned camera counts.
    import logging
    from dataclasses import replace

    base = config_mod.Config()
    good = replace(base, rig=replace(base.rig, coverage_mm=(300.0, 200.0)))

    for landscape in (False, True):
        cfg = replace(good, preview=replace(good.preview, landscape=landscape))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            config_mod.validate(cfg)
        assert not [r for r in caplog.records if "stretched" in r.getMessage()], \
            f"landscape={landscape} changed the verdict on a correct coverage"


def test_a_turned_camera_does_change_it(caplog):
    # capture.rotate_deg is the real thing: with the camera on its side the
    # frame genuinely is portrait, so a portrait coverage is then correct.
    import logging
    from dataclasses import replace

    base = config_mod.Config()
    cfg = replace(base,
                  rig=replace(base.rig, coverage_mm=(200.0, 300.0)),
                  capture=replace(base.capture, rotate_deg=90))
    with caplog.at_level(logging.WARNING):
        config_mod.validate(cfg)
    assert not [r for r in caplog.records if "stretched" in r.getMessage()]
