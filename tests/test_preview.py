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


def test_a4_fills_the_canvas_when_coverage_is_a4():
    """coverage_mm == A4 means an A4 region IS the scannable area.

    So its mark fills the canvas exactly -- the canvas being the scannable
    area. It used to run 67 pixels off each end, because the frame was the
    size of the streamed band and the streamed band is smaller than the
    scannable area; the mark was right and the frame was too small to hold it.
    """
    cfg = Config()
    canvas = preview.preview_size(cfg)
    a4 = next(m for m in preview.marks(cfg) if m.name == "A4")
    assert (a4.x, a4.y) == (0, 0)
    assert (a4.width, a4.height) == canvas
    assert not a4.clipped_top and not a4.clipped_bottom
    assert not a4.clipped_right

    # And the live stream covers only the middle of it.
    stream = preview.stream_size(cfg)
    assert stream[0] < canvas[0] or stream[1] < canvas[1]


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
    # filled dimming bands. Matched on the paper colours rather than on a
    # thickness: thickness now varies per paper so that sizes sharing an edge
    # stay individually visible instead of stacking into one colour.
    paper_boxes = [p for p in chain.split(",")
                   if p.startswith("drawbox")
                   and any(f"color={c}@" in p for c in preview.MARK_COLOURS)]
    assert len(paper_boxes) == 3
    for name in ("A4", "A5", "Letter"):
        assert f"text='{name}'" in chain


def test_marks_sharing_an_edge_are_drawn_at_different_thicknesses():
    """Anchoring to a corner makes coincident edges the normal case.

    Every mark then starts at the same pixel, and equal-thickness outlines
    stack so that only whichever colour was drawn last can be seen. Concentric
    widths keep all of them visible at once.
    """
    cfg = replace(Config(), rig=replace(Config().rig, anchor="top-left"))
    chain = preview.filter_chain(cfg)
    boxes = [p for p in chain.split(",")
             if p.startswith("drawbox")
             and any(f"color={c}@" in p for c in preview.MARK_COLOURS)]
    thicknesses = [int(p.split(":t=")[1]) for p in boxes]
    assert len(set(thicknesses)) == len(thicknesses), thicknesses
    # Largest paper outermost, so the smaller ones sit inside it.
    assert thicknesses == sorted(thicknesses, reverse=True)


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
    # (0, 0), not (0, -67): the anchor is applied inside the area the preview
    # can actually show, so a top-left mark starts at the top-left of the
    # picture rather than 67 pixels above it.
    assert (x, y) == (0, 0)
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


def test_an_edge_anchor_lands_on_the_edge_of_the_picture_not_the_coverage():
    """The complaint: marks on the padding, against nothing you can see.

    With the camera turned, the preview shows the middle 84.4% of the
    coverage's width. Anchored against the coverage, "left" put every mark at
    x = -67 in a 720-pixel frame -- off the picture, on the padding, where no
    sheet of paper can be laid. It must land on the edge of the picture.
    """
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(195.2, 292.8), anchor="left"),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview, papers=(("A5", 148.0, 210.0),)),
    )
    pw, _ph = preview.preview_size(cfg)
    assert preview.marks(cfg)[0].x == 0

    right = replace(cfg, rig=replace(cfg.rig, anchor="right"))
    mark = preview.marks(right)[0]
    assert mark.x + mark.width == pytest.approx(pw, abs=1)


def test_the_scan_crops_where_the_mark_was_drawn_with_the_camera_turned():
    """Preview and scan must agree about where a region sits, or the mark lies.

    Both anchor against the full coverage, which is the scannable area. There
    was briefly a clamp here that anchored inside the streamed band instead --
    it existed only because the frame was the size of the stream and could not
    show an edge-anchored mark. Sizing the frame to the scannable area removed
    the reason for it, and with it the cost: an edge-anchored scan uses the
    whole sensor again.
    """
    from camscan_escl.imaging import anchor_offset_mm

    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(195.2, 292.8), anchor="left"),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview, papers=(("A5", 148.0, 210.0),)),
    )
    mark = preview.marks(cfg)[0]
    assert mark.x == 0, "anchored left means the left edge of the scannable area"

    off_x, _off_y = anchor_offset_mm("left", cfg.rig.coverage_mm, (148.0, 210.0))
    assert off_x == 0.0, "and the scan crops from that same edge"

    # The same agreement at the far edge, where an error would be obvious.
    right = replace(cfg, rig=replace(cfg.rig, anchor="right"))
    canvas_w = preview.preview_size(right)[0]
    assert preview.marks(right)[0].x + preview.marks(right)[0].width == \
        pytest.approx(canvas_w, abs=1)
    off_x, _ = anchor_offset_mm("right", cfg.rig.coverage_mm, (148.0, 210.0))
    assert off_x == pytest.approx(195.2 - 148.0, abs=0.01)


def _pads(cfg):
    """(left, right, top, bottom) padding in pixels for this configuration."""
    pw, ph = preview.preview_size(cfg)
    scale, off_x, off_y = preview.fit_transform(cfg, preview.marks(cfg))
    vid_w, vid_h = round(pw * scale), round(ph * scale)
    return (off_x, pw - off_x - vid_w, off_y, ph - off_y - vid_h)


def _overflowing():
    # A4 is wider than this coverage, and the coverage is wider than the strip
    # the preview can show, so the A4 mark runs off the frame.
    return replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(195.2, 292.8)),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview,
                        papers=(("A4", 210.0, 297.0), ("A5", 148.0, 210.0))),
    )


@pytest.mark.parametrize("anchor,padded_side", [
    ("left", 1), ("right", 0),          # index into (left, right, top, bottom)
])
def test_padding_appears_only_on_the_side_that_overflows(anchor, padded_side):
    """The video stays flush against the edge the anchor names.

    That edge is where paper is lined up, so a border there would be a strip
    of dead colour between the picture and the very thing being measured
    against it. The overflow is on the other side, and so is the border.
    """
    cfg = replace(_overflowing(), rig=replace(_overflowing().rig, anchor=anchor))
    left, right, _top, _bottom = _pads(cfg)
    pads = (left, right)
    assert pads[padded_side] > 0, "the overflowing side must show a border"
    assert pads[1 - padded_side] == 0, "the anchored side must stay flush"


def test_an_overflowing_mark_is_fully_visible_inside_the_frame():
    cfg = _overflowing()
    pw, ph = preview.preview_size(cfg)
    raw = preview.marks(cfg)
    scale, ox, oy = preview.fit_transform(cfg, raw)
    a4 = preview.place(raw[0], scale, ox, oy)
    assert a4.name == "A4"
    # The whole reason for the border: its right edge is now on screen.
    assert 0 <= a4.x and a4.x + a4.width <= pw
    assert a4.y >= 0 and a4.y + a4.height <= ph


def test_a_centred_anchor_pads_both_sides_evenly():
    cfg = replace(_overflowing(), rig=replace(_overflowing().rig, anchor="center"))
    left, right, _t, _b = _pads(cfg)
    assert abs(left - right) <= 1
    assert left > 0


def test_slack_on_the_axis_that_did_not_set_the_scale_is_shared():
    """One axis usually sets the scale and leaves the other with slack.

    Giving that slack entirely to one side put 7 pixels above the picture and
    269 below it, on a mark whose vertical overflow was centred.
    """
    cfg = replace(_overflowing(), rig=replace(_overflowing().rig, anchor="left"))
    _left, _right, top, bottom = _pads(cfg)
    assert abs(top - bottom) <= 1, (top, bottom)


def test_padding_is_capped_so_the_picture_is_not_swallowed():
    """A border wide enough to eat the picture says less than a clipped mark."""
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(40.0, 60.0), anchor="left"),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview, papers=(("A4", 210.0, 297.0),)),
    )
    pw, _ph = preview.preview_size(cfg)
    scale, _ox, _oy = preview.fit_transform(cfg, preview.marks(cfg))
    assert scale == pytest.approx(1.0 / (1.0 + cfg.preview.max_pad), abs=1e-6)
    left, right, _t, _b = _pads(cfg)
    assert left + right <= round(pw * cfg.preview.max_pad) + 1


def _boxes(chain, predicate):
    """Parse drawbox filters into (x, y, w, h) tuples."""
    out = []
    for part in chain.split(","):
        if not part.startswith("drawbox") or not predicate(part):
            continue
        # "drawbox=x=0:y=0:w=720:h=166:color=gray@0.55:t=fill" -- the first
        # key carries no colon in front of it, so parse pairs rather than
        # searching for ":x=".
        fields = dict(
            token.split("=", 1)
            for token in part.removeprefix("drawbox=").split(":")
            if "=" in token
        )
        out.append(tuple(int(fields[k]) for k in ("x", "y", "w", "h")))
    return out


def test_the_dimming_stays_off_the_border():
    """The border is not dead space; it is where the last scan is shown.

    The dead-zone dimming means "the camera sees this but no scan can reach
    it", which is a statement about live video. The border is the opposite --
    somewhere the scan DOES reach and the live view cannot -- so greying it
    would wash out the very thing it exists to carry.
    """
    cfg = replace(_overflowing(), rig=replace(_overflowing().rig, anchor="left"))
    raw = preview.marks(cfg)
    scale, ox, oy = preview.fit_transform(cfg, raw)
    assert scale < 1.0, "this fixture must produce a border, or it tests nothing"
    vx, vy, vw, vh = preview.video_rect(cfg, scale, ox, oy)

    for x, y, w, h in _boxes(preview.filter_chain(cfg), lambda p: "t=fill" in p):
        assert x >= vx and y >= vy, (x, y)
        assert x + w <= vx + vw and y + h <= vy + vh, (x, y, w, h)


def test_generated_state_never_lands_in_the_real_config_directory(tmp_path):
    """The suite ran real scans and wrote the user's live preview border.

    `_next_document` stores the still and composites the border on any config
    it is handed, so a test config pointing at the default directory replaced
    a running daemon's ghost with FAKE_CAMERA's flat blue rectangle -- and the
    daemon went on showing it until the next real scan.
    """
    from camscan_escl import config as config_mod

    scoped = replace(_overflowing(), state_dir=tmp_path)
    assert preview.ghost_path(scoped).parent == tmp_path
    assert preview.scan_still_path(scoped).parent == tmp_path

    # Unset still means the real directory, which is right for the daemon.
    assert preview.ghost_path(_overflowing()).parent == config_mod.CONFIG_DIR


def test_the_ghost_covers_more_than_the_live_picture():
    """The whole reason the border is worth filling.

    The scan captures the full still; the preview is a 16:9 crop of it. So
    the still reaches into the border on exactly the sides the live view is
    missing, and the ghost rectangle must strictly contain the video one.
    """
    cfg = replace(_overflowing(), rig=replace(_overflowing().rig, anchor="left"))
    scale, ox, oy = preview.fit_transform(cfg, preview.marks(cfg))
    vx, vy, vw, vh = preview.video_rect(cfg, scale, ox, oy)
    gx, gy, gw, gh = preview.ghost_rect(cfg, scale, ox, oy)

    assert gx <= vx and gy <= vy
    assert gx + gw >= vx + vw and gy + gh >= vy + vh
    assert (gw, gh) != (vw, vh), "the still must reach further than the preview"
    # The visible band is the middle 84.4% of the width, so the still is
    # about 1/0.844 as wide as the picture.
    assert gw / vw == pytest.approx(1 / 0.84375, rel=0.02)


def test_writing_a_ghost_leaves_a_hole_for_the_live_picture(tmp_path, monkeypatch):
    """Transparent where the video goes, or the overlay covers the preview."""
    from PIL import Image

    from camscan_escl import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    cfg = replace(_overflowing(), rig=replace(_overflowing().rig, anchor="left"))
    scan = Image.new("RGB", (1536, 2304), (20, 200, 40))   # unmistakable green

    path = preview.write_ghost(cfg, scan)
    assert path is not None and path.exists()

    image = Image.open(path).convert("RGBA")
    assert image.size == preview.preview_size(cfg)

    scale, ox, oy = preview.fit_transform(cfg, preview.marks(cfg))
    vx, vy, vw, vh = preview.video_rect(cfg, scale, ox, oy)
    assert image.getpixel((vx + vw // 2, vy + vh // 2))[3] == 0, "video is covered"

    # And opaque in the border, carrying the scan washed towards the padding.
    edge = image.getpixel((vx + vw + 5, vy + vh // 2))
    assert edge[3] == 255
    assert edge[1] > edge[0] and edge[1] > edge[2], edge   # still greenish

    # Faint means nearer the padding than the scan. Not "dimmer than the
    # scan" channel by channel -- the padding is a light colour, so blending
    # towards it can raise a channel as easily as lower one.
    pad = preview._rgb(cfg.preview.pad_colour)
    scan_rgb = (20, 200, 40)
    to_pad = sum(abs(a - b) for a, b in zip(edge[:3], pad))
    to_scan = sum(abs(a - b) for a, b in zip(edge[:3], scan_rgb))
    assert to_pad < to_scan, f"{edge} is closer to the scan than to the padding"


def test_changing_the_paper_sizes_redraws_the_ghost_rather_than_losing_it(
        tmp_path, monkeypatch):
    """The still stays true when the settings move; only its placement does not.

    Discarding it on any settings change was wrong: paper checkboxes are the
    most-clicked control in the window, and blanking the border until someone
    happened to scan again throws away a perfectly good photograph of the desk.
    """
    from PIL import Image

    from camscan_escl import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    base = replace(_overflowing(), rig=replace(_overflowing().rig, anchor="left"))
    preview.save_scan_still(base, Image.new("RGB", (2304, 1536), (20, 200, 40)))
    first = preview.rebuild_ghost(base)
    assert first is not None
    before = Image.open(first).copy()

    # Tick another paper size. That changes which marks overflow, so the zoom
    # and the offsets move -- and with them where the still belongs.
    wider = replace(base, preview=replace(
        base.preview, papers=(("Legal", 215.9, 355.6), ("A4", 210.0, 297.0))))
    assert preview.fit_transform(wider, preview.marks(wider)) != \
        preview.fit_transform(base, preview.marks(base)), "fixture must move the geometry"

    second = preview.rebuild_ghost(wider)
    assert second is not None and second.exists(), "the ghost must survive"
    after = Image.open(second)
    assert after.tobytes() != before.tobytes(), "it must be redrawn, not left stale"

    # And the still itself is untouched, so it can be redrawn again.
    assert preview.scan_still_path(wider).exists()


def test_a_rotation_change_reorients_the_stored_still(tmp_path, monkeypatch):
    """Stored unrotated on purpose, so `rotate_deg` can change without a rescan."""
    from PIL import Image

    from camscan_escl import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    base = replace(_overflowing(), rig=replace(_overflowing().rig, anchor="left"))
    preview.save_scan_still(base, Image.new("RGB", (2304, 1536), (20, 200, 40)))
    assert preview.rebuild_ghost(base) is not None

    turned = replace(base, capture=replace(base.capture, rotate_deg=90))
    assert preview.rebuild_ghost(turned) is not None, "must not need a rescan"


def test_the_ghost_survives_when_nothing_overflows(tmp_path, monkeypatch):
    """The un-streamed region is a fact of the camera, not of the paper sizes.

    The ghost used to be deleted whenever no mark overflowed, which threw away
    a good photograph of the desk for a reason that had nothing to do with it.
    The still always reaches further than the stream, so there is always
    somewhere for it to go.
    """
    from PIL import Image

    from camscan_escl import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    # Everything fits: no zoom, no extension.
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(294.3, 441.4), anchor="left"),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview, papers=(("A5", 148.0, 210.0),)),
    )
    assert preview.fit_transform(cfg, preview.marks(cfg)) == (1.0, 0, 0)

    preview.save_scan_still(cfg, Image.new("RGB", (2304, 1536), (20, 200, 40)))
    path = preview.rebuild_ghost(cfg)
    assert path is not None and path.exists(), "no overflow must not mean no ghost"
    assert preview.usable_ghost(cfg) == str(path)

    # And it covers the strip the stream does not reach.
    scale, ox, oy = preview.fit_transform(cfg, preview.marks(cfg))
    vx, _vy, vw, _vh = preview.video_rect(cfg, scale, ox, oy)
    gx, _gy, gw, _gh = preview.ghost_rect(cfg, scale, ox, oy)
    assert gx < vx and gx + gw > vx + vw


def test_the_ghost_can_be_turned_off(tmp_path, monkeypatch):
    from PIL import Image

    from camscan_escl import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    base = _overflowing()
    cfg = replace(base, preview=replace(base.preview, scan_ghost=False))
    assert preview.write_ghost(cfg, Image.new("RGB", (1536, 2304))) is None


def test_the_command_composites_the_ghost_under_the_marks():
    """Order matters: video, then ghost, then marks. Marks must stay on top."""
    cfg = _overflowing()
    argv = preview.build_command(cfg, "/dev/fake0", "", "/tmp/ghost.png")

    assert argv.count("-i") == 2, "camera and ghost"
    assert "-loop" in argv, "the still must repeat, not end after one frame"
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.index("[live]") < graph.index("overlay=0:0")
    assert graph.index("overlay=0:0") < graph.index("drawbox")
    # The camera is the overlay BASE, so output timing follows the camera and
    # not the looping still.
    assert "[live][ghost]overlay" in graph


def test_two_outputs_are_split_when_a_loopback_is_configured():
    cfg = _overflowing()
    argv = preview.build_command(cfg, "/dev/fake0", "/dev/fake9", "/tmp/g.png")
    graph = argv[argv.index("-filter_complex") + 1]
    assert "split=2[out0][out1]" in graph
    assert argv.count("-map") == 2
    assert argv[-1] == "pipe:1"
    assert "/dev/fake9" in argv


def test_without_a_ghost_the_command_keeps_its_simple_shape():
    cfg = _overflowing()
    argv = preview.build_command(cfg, "/dev/fake0", "/dev/fake9")
    assert "-filter_complex" not in argv
    assert argv.count("-vf") == 2
    assert argv.count("-i") == 1


def test_marks_that_fit_produce_no_border_at_all():
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(294.3, 441.4), anchor="left"),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview, papers=(("A5", 148.0, 210.0),)),
    )
    assert preview.fit_transform(cfg, preview.marks(cfg)) == (1.0, 0, 0)
    assert _pads(cfg) == (0, 0, 0, 0)


def test_does_not_fit_names_the_papers_and_the_numbers():
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(195.2, 292.8)),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview,
                        papers=(("A4", 210.0, 297.0), ("B7", 88.0, 125.0))),
    )
    oversize = preview.does_not_fit(cfg)
    assert [e["name"] for e in oversize] == ["A4"], "B7 fits and must not be listed"
    # The numbers are the point: what it needs against what the camera sees.
    assert oversize[0]["needs"][0] == 210.0
    assert oversize[0]["available"][0] < 210.0


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
    """The STREAM is what the camera gives; the canvas is bigger than it.

    The published frame is sized to the scannable area now, not to the
    streaming mode -- the still reaches further than any streaming mode can,
    and a frame the size of the stream could only ever show part of what a
    scan captures.
    """
    cfg = replace(Config(), capture=replace(Config().capture, rotate_deg=rotate))
    stream = preview.stream_size(cfg)
    if rotate % 180 == 90:
        assert stream == (cfg.preview.height, cfg.preview.width)
    else:
        assert stream == (cfg.preview.width, cfg.preview.height)

    canvas = preview.preview_size(cfg)
    still = preview.upright_still(cfg)
    # The canvas carries the whole still, so it shares the still's shape.
    assert canvas[0] / canvas[1] == pytest.approx(still[0] / still[1], rel=0.01)
    # And the stream sits inside it, never larger.
    assert canvas[0] >= stream[0] and canvas[1] >= stream[1]
    ox, oy = preview.stream_origin(cfg)
    assert ox + stream[0] <= canvas[0] and oy + stream[1] <= canvas[1]


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

    The edge an anchor touches is now the edge of the PICTURE, not the edge
    of the coverage: the preview cannot show the outer 15% of the coverage's
    width, so anchoring against it put every mark 67 pixels off the side of
    a 720-pixel frame, on padding no sheet of paper could be laid against.
    """
    coverage = (200.0, 300.0) if rotate % 180 == 90 else (300.0, 200.0)
    cfg = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=coverage, anchor=anchor),
        capture=replace(Config().capture, rotate_deg=rotate),
        preview=replace(Config().preview, papers=(("A5", 148.0, 210.0),)),
    )
    pw, _ph = preview.preview_size(cfg)

    mark = preview.marks(cfg)[0]
    if touches == "left":
        assert mark.x == pytest.approx(0, abs=2)
    else:
        assert mark.x + mark.width == pytest.approx(pw, abs=2)


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
        preview=replace(Config().preview, papers=(("A4", 210.0, 297.0),),
                        fit_marks=True),
    )
    raw = preview.marks(cfg)[0]
    pw, ph = preview.preview_size(cfg)
    assert raw.x < 0 and raw.y < 0, "this fixture should not fit, or it tests nothing"

    scale, ox, oy = preview.fit_transform(cfg, [raw])
    assert scale < 1.0, "should have zoomed out"
    shown = preview.place(raw, scale, ox, oy)
    # Dimensionally inside the frame, so every edge can be drawn. NOT asserted
    # to be positioned inside it: the fit is computed from mark sizes alone
    # now, because deriving it from position meant the live video shrank and
    # slid about whenever the anchor moved. Position is left to clip.
    assert shown.width <= pw and shown.height <= ph
    # And it must actually overlap the picture, or nothing is gained.
    assert shown.x < pw and shown.y < ph
    assert shown.x + shown.width > 0 and shown.y + shown.height > 0


def test_the_anchor_does_not_move_a_picture_nothing_overflows():
    """The complaint that prompted the change, as a test.

    Where every mark fits, `rig.anchor` moves the marks and nothing else: no
    border is needed, so none appears and the picture does not budge. It used
    to, because the zoom was derived from the union of mark POSITIONS while
    those positions were unclamped -- measured at scale 0.9149 offset (61, 54)
    anchored top-left against 1.0000 offset (0, 0) anchored centre, on the
    same coverage and the same papers.

    Where a mark DOES overflow, the picture is deliberately flush against the
    anchored edge with the border opposite; that is
    `test_padding_appears_only_on_the_side_that_overflows`.
    """
    base = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(294.3, 441.4)),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview,
                        papers=(("A4", 210.0, 297.0), ("A5", 148.0, 210.0))),
    )
    transforms = set()
    for anchor in ("top-left", "top", "center", "right", "bottom-right"):
        cfg = replace(base, rig=replace(base.rig, anchor=anchor))
        transforms.add(preview.fit_transform(cfg, preview.marks(cfg)))
    assert len(transforms) == 1, f"the picture moved with the anchor: {transforms}"


def test_an_oversized_paper_still_zooms_whatever_the_anchor():
    """Size-driven zoom must survive the anchor-independence change."""
    base = replace(
        Config(),
        rig=replace(Config().rig, coverage_mm=(189.2, 283.8)),
        capture=replace(Config().capture, rotate_deg=270),
        preview=replace(Config().preview, papers=(("A4", 210.0, 297.0),),
                        fit_marks=True),
    )
    for anchor in ("top-left", "center", "bottom-right"):
        cfg = replace(base, rig=replace(base.rig, anchor=anchor))
        scale, _ox, _oy = preview.fit_transform(cfg, preview.marks(cfg))
        assert scale < 1.0, f"{anchor}: an oversized paper should still zoom out"


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
        preview=replace(Config().preview, fit_marks=True),
    )
    chain = preview.filter_chain(cfg)
    assert chain.startswith("transpose")
    assert "scale=" in chain and "pad=" in chain
    assert chain.index("transpose") < chain.index("scale=") < chain.index("drawbox")
