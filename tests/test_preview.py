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
