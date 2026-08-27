"""Spec §5 / §11 stage 3: the returned JPEG must match region x res / 300."""

import io

import pytest
from PIL import Image

from camscan_escl import imaging
from camscan_escl.escl import A4, LETTER, ScanRegion, ScanSettings

COVERAGE = (210.0, 297.0)


def frame(size=(2304, 1536)):
    img = Image.new("RGB", size, (200, 40, 40))
    # A marker so a crop that lands in the wrong place is visible in failure.
    img.paste((40, 200, 40), (0, 0, size[0] // 2, size[1] // 2))
    return img


@pytest.mark.parametrize("paper", [A4, LETTER], ids=["a4", "letter"])
@pytest.mark.parametrize("dpi", [150, 200, 300])
def test_full_page_dimensions(paper, dpi):
    settings = ScanSettings(ScanRegion(0, 0, *paper), dpi, dpi, "RGB24")
    jpeg = imaging.render(frame(), settings, COVERAGE, 88)
    got = Image.open(io.BytesIO(jpeg)).size
    assert got == (round(paper[0] * dpi / 300), round(paper[1] * dpi / 300))
    assert got == settings.expected_size


def test_partial_region_dimensions():
    settings = ScanSettings(ScanRegion(300, 600, 1200, 1500), 150, 150, "RGB24")
    jpeg = imaging.render(frame(), settings, COVERAGE, 88)
    assert Image.open(io.BytesIO(jpeg)).size == (600, 750)


def test_anisotropic_resolution():
    settings = ScanSettings(ScanRegion(0, 0, *A4), 150, 300, "RGB24")
    jpeg = imaging.render(frame(), settings, COVERAGE, 88)
    assert Image.open(io.BytesIO(jpeg)).size == (1240, 3508)


def test_grayscale_is_single_channel_and_right_size():
    settings = ScanSettings(ScanRegion(0, 0, *A4), 150, 150, "Grayscale8")
    jpeg = imaging.render(frame(), settings, COVERAGE, 88)
    img = Image.open(io.BytesIO(jpeg))
    assert img.mode == "L"
    assert img.size == settings.expected_size


def test_region_beyond_coverage_is_padded_white_not_stretched():
    # Coverage is only half the page: the bottom half was never in frame.
    settings = ScanSettings(ScanRegion(0, 0, *A4), 150, 150, "RGB24")
    jpeg = imaging.render(frame(), settings, (210.0, 148.5), 88)
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    assert img.size == settings.expected_size
    assert img.getpixel((img.width // 2, img.height - 4)) == (255, 255, 255)
    assert img.getpixel((img.width // 2, 4)) != (255, 255, 255)


def test_region_entirely_outside_coverage_is_blank():
    settings = ScanSettings(ScanRegion(A4[0] * 3, 0, 600, 600), 150, 150, "RGB24")
    jpeg = imaging.render(frame(), settings, COVERAGE, 88)
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    assert img.size == settings.expected_size
    assert img.getpixel((img.width // 2, img.height // 2)) == (255, 255, 255)


def test_anchor_shifts_which_pixels_are_used():
    # The units contract is about size; the anchor is about position. A
    # centred anchor must still return exactly the contracted dimensions.
    from camscan_escl.imaging import anchor_offset_mm, render
    from camscan_escl.escl import ScanRegion, ScanSettings
    from PIL import Image
    import io

    # A frame with a distinctive left half, so a shift is detectable.
    frame = Image.new("RGB", (2304, 1536), (255, 255, 255))
    frame.paste(Image.new("RGB", (1152, 1536), (0, 0, 0)), (0, 0))

    settings = ScanSettings(ScanRegion(0, 0, 1240, 1754), 150, 150, "RGB24")
    coverage = (210.0, 297.0)

    left = Image.open(io.BytesIO(render(frame, settings, coverage, 90, "top-left")))
    right = Image.open(io.BytesIO(render(frame, settings, coverage, 90, "top-right")))

    assert left.size == right.size == settings.expected_size
    # Same contract, different pixels: shifting right must pull in more white.
    assert right.convert("L").histogram()[255:] > left.convert("L").histogram()[255:]


def test_unknown_anchor_falls_back_to_the_origin():
    from camscan_escl.imaging import anchor_offset_mm
    assert anchor_offset_mm("nonsense", (210.0, 297.0), (148.0, 210.0)) == (0.0, 0.0)
