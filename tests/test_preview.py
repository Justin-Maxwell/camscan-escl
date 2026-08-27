"""Preview geometry: the mapping between preview pixels and still pixels.

These numbers are not arbitrary. The relationship was measured on the rig by
cross-correlating a 1280x720 frame against the still scaled to 1280 wide: the
best match sat at row 67 where a centred crop predicts 66 (RMS 6.3), while
the "whole frame squashed" hypothesis scored 32.9. If these tests start
failing, the model of the camera is wrong, not the arithmetic.
"""

import pytest
from dataclasses import replace

from camscan_escl import preview, previewpage
from camscan_escl.config import Config, PreviewConfig, RigConfig, validate

STILL = (2304, 1536)
PREVIEW = (1280, 720)


def test_preview_shows_the_centre_of_the_still():
    top, bottom = preview.visible_still_rows(STILL, PREVIEW)
    # 1280/2304 scale means the preview covers 720/(1280/2304) = 1296 rows,
    # centred in 1536: 120 hidden above, 120 below.
    assert top == pytest.approx(120.0)
    assert bottom == pytest.approx(1416.0)
    assert (bottom - top) == pytest.approx(1296.0)


def test_hidden_rows_scale_to_the_measured_offset():
    # 120 still rows, expressed in preview pixels, is the 67 px offset that
    # cross-correlation found independently.
    top, _ = preview.visible_still_rows(STILL, PREVIEW)
    assert round(top * PREVIEW[0] / STILL[0]) == 67


def test_a4_fills_the_frame_when_coverage_is_a4():
    # With coverage_mm == A4, an A4 region is the whole sensor area by
    # definition, so the mark is full width and runs off both ends of a
    # preview that can only show the middle 84%.
    cfg = Config()
    a4 = next(m for m in preview.marks(cfg) if m.name == "A4")
    assert a4.x == 0
    assert a4.width == PREVIEW[0]
    assert a4.y == -67
    assert a4.clipped_top and a4.clipped_bottom
    assert not a4.clipped_right


def test_letter_does_not_fit_an_a4_coverage():
    # Letter is wider than A4, so with an A4 coverage it cannot be captured.
    # The mark has to say so rather than quietly drawing inside the frame.
    letter = next(m for m in preview.marks(Config()) if m.name == "Letter")
    assert letter.width > PREVIEW[0]
    assert letter.clipped_right


def test_marks_scale_with_coverage():
    # Doubling the area the frame covers halves the size of every mark.
    cfg = Config()
    wider = replace(cfg, rig=RigConfig(coverage_mm=(420.0, 594.0)))
    before = next(m for m in preview.marks(cfg) if m.name == "A5")
    after = next(m for m in preview.marks(wider) if m.name == "A5")
    assert after.width == pytest.approx(before.width / 2, abs=1)


def test_four_three_preview_is_rejected():
    # 4:3 modes are zoomed to a narrower horizontal field, so no crop maps
    # them onto the still and every mark would be wrong.
    cfg = replace(Config(), preview=PreviewConfig(width=640, height=480))
    with pytest.raises(ValueError, match="16:9"):
        validate(cfg)


def test_page_reports_what_the_preview_cannot_show():
    # The scan sees more than the preview. A positioning aid that hid that
    # would be worse than none.
    cfg = Config()
    html = previewpage.page_html(cfg, preview.marks(cfg))
    assert "120 pixel rows more" in html
    assert "/preview/stream" in html
    for name in ("A4", "A5", "Letter"):
        assert name in html


class _FakeStream(preview.PreviewStream):
    """A PreviewStream with the camera replaced by pushed frames."""

    def _start_locked(self):
        self._running = True
        return True

    def _stop_locked(self):
        self._running = False
        with self._cond:
            self._latest = None
            self._cond.notify_all()

    def push(self, data):
        with self._cond:
            self._latest = data
            self._seq += 1
            self._cond.notify_all()


def test_open_stream_survives_a_scan():
    # Regression: `released()` used to blank the latest frame, the generator
    # saw None and returned, the MJPEG response ended -- and a browser does
    # not reconnect a broken <img> stream, so the preview stayed dead until
    # the page was reloaded. A single-frame fetch resumed fine, which is why
    # this was missed.
    import threading

    stream = _FakeStream(Config())
    stream.start()
    stream.push(b"one")

    got, done = [], threading.Event()

    def consume():
        for frame in stream.frames(stall_timeout=10.0):
            got.append(frame)
            if len(got) == 2:
                break
        done.set()

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()

    with stream.released():
        pass  # a scan happens here: camera gone, then back

    stream.push(b"two")
    assert done.wait(10.0), "stream ended during the scan instead of waiting"
    assert got == [b"one", b"two"]


def test_stream_ends_when_the_preview_really_stops():
    # The flip side: waiting through a pause must not mean hanging forever
    # once the preview is genuinely shut down.
    stream = _FakeStream(Config())
    stream.start()
    stream.push(b"one")
    frames = stream.frames(stall_timeout=10.0)
    assert next(frames) == b"one"
    stream.stop()
    with pytest.raises(StopIteration):
        next(frames)


def test_loopback_output_is_added_only_when_configured():
    # Publishing to a v4l2loopback device is what puts the crop marks inside
    # Kamoso; without one the preview is web-only.
    plain = preview.build_command(Config())
    assert "-f" in plain and plain[-1] == "pipe:1"
    assert not any(a.startswith("/dev/video") and a != "/dev/video0" for a in plain)

    cfg = replace(Config(), preview=replace(Config().preview,
                                            loopback_device="/dev/video9"))
    both = preview.build_command(cfg)
    assert "/dev/video9" in both
    # One camera read, two outputs: reading the device twice is impossible,
    # access being exclusive.
    assert both.count("-i") == 1
    assert both[-1] == "pipe:1"


def test_marks_are_burned_in_by_ffmpeg_not_python():
    chain = preview.filter_chain(Config())
    # One outlined box per paper, over and above the union frame and the
    # filled dimming bands.
    paper_boxes = [p for p in chain.split(",")
                   if p.startswith("drawbox") and "t=3" in p]
    assert len(paper_boxes) == 3
    for name in ("A4", "A5", "Letter"):
        assert f"text='{name}'" in chain


def test_labels_do_not_stack_on_a_shared_top_edge():
    # Every mark starts at the coverage origin, so all three share a top edge
    # and unstaggered labels would land in one illegible pile.
    chain = preview.filter_chain(Config())
    ys = [int(p.split(":y=")[1].split(":")[0])
          for p in chain.split(",") if p.startswith("drawtext")]
    assert len(ys) == len(set(ys)), f"labels overlap at {ys}"


def test_union_is_the_bounding_box_of_every_paper():
    cfg = replace(Config(), rig=replace(Config().rig, anchor="top-left"))
    m = preview.marks(cfg)
    x, y, w, h = preview.union_rect(m)
    assert (x, y) == (0, -67)
    assert w == max(k.x + k.width for k in m)
    assert h == max(k.y + k.height for k in m) - y


def test_anchor_moves_the_marks_and_matches_the_scan():
    # The preview and imaging.render must agree about where a region sits, or
    # the mark points somewhere the scan will not crop.
    from camscan_escl.imaging import anchor_offset_mm

    cfg = replace(Config(), rig=replace(Config().rig, anchor="center"))
    a5 = next(m for m in preview.marks(cfg) if m.name == "A5")
    cov = cfg.rig.coverage_mm
    off_x, _ = anchor_offset_mm("center", cov, (148.0, 210.0))
    expected_x = round(off_x / cov[0] * cfg.capture.native_width
                       * cfg.preview.width / cfg.capture.native_width)
    assert a5.x == expected_x
    assert a5.x > 0, "a centred A5 should not sit against the left edge"

    left = next(m for m in preview.marks(
        replace(cfg, rig=replace(cfg.rig, anchor="top-left"))) if m.name == "A5")
    assert left.x == 0


def test_dead_zone_is_dimmed_outside_the_union():
    # Everything outside the union can never appear in any scan, whatever
    # paper is picked, so it should not look usable.
    cfg = replace(Config(), rig=replace(Config().rig, coverage_mm=(420.0, 594.0)))
    chain = preview.filter_chain(cfg)
    fills = [p for p in chain.split(",") if "t=fill" in p]
    assert fills, "no dimming bands drawn"
    assert all("gray" in f for f in fills)


def test_no_bands_are_drawn_off_screen():
    cfg = replace(Config(), rig=replace(Config().rig, coverage_mm=(420.0, 594.0)))
    chain = preview.filter_chain(cfg)
    for part in chain.split(","):
        if "t=fill" not in part:
            continue
        w = int(part.split(":w=")[1].split(":")[0])
        h = int(part.split(":h=")[1].split(":")[0])
        assert w > 0 and h > 0, part


@pytest.mark.parametrize("landscape", [False, True])
@pytest.mark.parametrize("rotate", [0, 90])
def test_a_mark_can_only_have_its_papers_aspect(landscape, rotate):
    """The invariant that matters: A4 is 1:1.414, so its mark is that or 1.414:1.

    Nothing else is geometrically possible. Every orientation bug so far has
    produced a mark of some aspect no sheet of paper has -- one made A4
    nearly 3:1 -- while the sizes still looked plausible in isolation. This
    catches all of them at once, whatever combination of turns caused it.

    Only holds when the coverage matches the frame's shape, which is the only
    correct configuration anyway: otherwise the mapping is anisotropic and
    every mark is distorted along with every scan.
    """
    # The coverage describes the frame AFTER rotation, so a turned camera
    # needs a portrait coverage for the mapping to be isotropic at all.
    coverage = (200.0, 300.0) if rotate % 180 == 90 else (300.0, 200.0)
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=coverage, anchor="top-left"),
        capture=replace(Config().capture, rotate_deg=rotate),
        preview=replace(Config().preview, landscape=landscape),
    )
    for mark in preview.marks(cfg):
        paper = next(p for p in cfg.preview.papers if p[0] == mark.name)
        want = paper[1] / paper[2]
        got = mark.width / mark.height
        assert got == pytest.approx(want, rel=0.01) or \
               got == pytest.approx(1 / want, rel=0.01), (
            f"{mark.name} drawn {mark.width}x{mark.height} = {got:.3f}:1, "
            f"but the paper is {want:.3f}:1 (or {1/want:.3f}:1 turned)"
        )


def test_landscape_turns_the_paper_not_the_frame():
    # A4 landscape is wider than tall; the coverage is untouched, because the
    # camera sees the same area whichever way the page lies.
    cfg = replace(Config(),
                  rig=replace(Config().rig, coverage_mm=(300.0, 200.0)))
    portrait = next(m for m in preview.marks(cfg) if m.name == "A4")
    turned = next(m for m in preview.marks(
        replace(cfg, preview=replace(cfg.preview, landscape=True)))
        if m.name == "A4")
    assert portrait.width < portrait.height
    assert turned.width > turned.height
    assert turned.width == pytest.approx(portrait.height, rel=0.01)
    assert turned.height == pytest.approx(portrait.width, rel=0.01)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rotation_happens_at_the_head_of_the_pipeline(rotate):
    # The turn belongs before anything else reads the frame, so preview,
    # loopback, marks and scan all share one upright coordinate space.
    # Compensating downstream instead produced three separate orientation
    # bugs, each of which looked plausible in isolation.
    cfg = replace(Config(),
                  rig=replace(Config().rig,
                              coverage_mm=(200.0, 300.0) if rotate % 180 == 90
                              else (300.0, 200.0)),
                  capture=replace(Config().capture, rotate_deg=rotate))
    chain = preview.filter_chain(cfg)
    if rotate == 0:
        assert "transpose" not in chain
    else:
        assert chain.startswith("transpose"), \
            "the turn must come before the marks are drawn"


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_published_frame_is_turned_with_the_camera(rotate):
    cfg = replace(Config(), capture=replace(Config().capture, rotate_deg=rotate))
    w, h = preview.preview_size(cfg)
    if rotate % 180 == 90:
        assert (w, h) == (cfg.preview.height, cfg.preview.width)
    else:
        assert (w, h) == (cfg.preview.width, cfg.preview.height)


def test_turning_a_mark_is_reversible():
    # Four quarter turns is the identity; anything else means the transform
    # loses geometry rather than moving it.
    m = preview.Mark("A4", 100, 50, 300, 400, False, False, False)
    sensor = (1280, 720)
    turned = m
    for _ in range(4):
        turned = preview.turn_mark(turned, 90, sensor
                                   if _ % 2 == 0 else (sensor[1], sensor[0]))
    assert (turned.x, turned.y, turned.width, turned.height) == \
           (m.x, m.y, m.width, m.height)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("anchor,touches", [
    ("top-left", "left"), ("top-right", "right"),
    ("bottom-left", "left"), ("bottom-right", "right"),
])
def test_anchor_means_the_same_corner_whatever_the_rotation(rotate, anchor, touches):
    """The anchor grid is read in upright space, as imaging.render reads it.

    Marks used to be computed in the sensor's own space, so with the camera
    turned the grid pointed at a different corner than the scan cropped from
    -- invisibly, because both looked reasonable on their own.
    """
    coverage = (200.0, 300.0) if rotate % 180 == 90 else (300.0, 200.0)
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=coverage, anchor=anchor),
        capture=replace(Config().capture, rotate_deg=rotate),
        preview=replace(Config().preview, papers=(("A5", 148.0, 210.0),)),
    )
    sw, sh = cfg.capture.native_width, cfg.capture.native_height
    if rotate % 180 == 90:
        sw, sh = sh, sw
    x0, _y0, x1, _y1 = preview.visible_still_region(cfg)
    scale = preview.preview_size(cfg)[0] / (x1 - x0)
    cov_left = (0 - x0) * scale
    cov_right = (sw - x0) * scale

    mark = preview.marks(cfg)[0]
    if touches == "left":
        assert mark.x == pytest.approx(cov_left, abs=2)
    else:
        assert mark.x + mark.width == pytest.approx(cov_right, abs=2)


def test_a_mark_bigger_than_the_frame_is_still_drawn():
    """The case that most needs seeing was the one that drew nothing.

    A paper larger than the coverage has all four edges outside the picture,
    so every line falls off-screen and the preview looks as though the size
    is simply not enabled -- when what it is trying to say is that the paper
    does not fit and the camera needs raising.
    """
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(189.2, 283.8), anchor="center"),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview, papers=(("A4", 210.0, 297.0),)),
    )
    raw = preview.marks(cfg)[0]
    pw, ph = preview.preview_size(cfg)
    assert raw.x < 0 and raw.y < 0, "this fixture should not fit, or it tests nothing"

    scale, ox, oy = preview.fit_transform(cfg, [raw])
    assert scale < 1.0, "should have zoomed out"
    shown = preview.place(raw, scale, ox, oy)
    assert 0 <= shown.x and shown.x + shown.width <= pw
    assert 0 <= shown.y and shown.y + shown.height <= ph


def test_zooming_out_keeps_the_marks_shape():
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(189.2, 283.8)),
        capture=replace(Config().capture, rotate_deg=270),
    )
    raw = preview.marks(cfg)
    scale, ox, oy = preview.fit_transform(cfg, raw)
    for mark in raw:
        shown = preview.place(mark, scale, ox, oy)
        assert shown.width / shown.height == pytest.approx(
            mark.width / mark.height, rel=0.02)


def test_a_frame_that_already_fits_is_not_scaled():
    # Shrinking a picture that needs no shrinking would throw away detail
    # for nothing.
    cfg = replace(Config(),
                  rig=replace(Config().rig, coverage_mm=(600.0, 400.0)))
    raw = preview.marks(cfg)
    scale, ox, oy = preview.fit_transform(cfg, raw)
    assert (scale, ox, oy) == (1.0, 0, 0)
    assert preview.place(raw[0], scale, ox, oy) == raw[0]


def test_zoom_out_filters_come_after_the_turn():
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(189.2, 283.8)),
        capture=replace(Config().capture, rotate_deg=270),
    )
    chain = preview.filter_chain(cfg)
    assert chain.startswith("transpose")
    assert "scale=" in chain and "pad=" in chain
    assert chain.index("transpose") < chain.index("scale=") < chain.index("drawbox")
