"""The preview page: a live frame with crop marks drawn over it.

Deliberately one self-contained file with no assets and no JavaScript
framework. The daemon is a scanner, not a web app, and a positioning aid
that needs a build step would not survive contact with a rig.

The overlay is an SVG in the same coordinate space as the preview image, so
the two scale together however the browser lays the page out.
"""

from __future__ import annotations

import html

from .config import Config
from .preview import (Mark, preview_size, sensor_preview_size,
                      union_rect, visible_still_rows)

# Distinct hues, readable against paper and against a dark desk alike.
COLOURS = ("#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4")


def page_html(cfg: Config, marks: list[Mark]) -> str:
    w, h = preview_size(cfg)
    still = (cfg.capture.native_width, cfg.capture.native_height)
    top, bottom = visible_still_rows(still, sensor_preview_size(cfg))
    hidden_rows = int(round(top))
    # After a transpose the hidden band is at the sides, not the top.
    edges = ("left and at the right" if cfg.capture.rotate_deg % 180 == 90
             else "top and at the bottom")

    # The dead zone: everything outside the union of the paper sizes can
    # never appear in any scan. One path with evenodd, which SVG can express
    # and drawbox cannot.
    ux, uy, uw, uh = union_rect(marks)
    shapes = [
        f'<path d="M0,0 H{w} V{h} H0 Z M{ux},{uy} h{uw} v{uh} h{-uw} Z" '
        f'fill="#000" fill-opacity="0.55" fill-rule="evenodd" />',
        f'<rect x="{ux}" y="{uy}" width="{uw}" height="{uh}" fill="none" '
        f'stroke="#fff" stroke-opacity="0.85" stroke-width="2" />',
    ]
    legend = []
    for i, m in enumerate(marks):
        colour = COLOURS[i % len(COLOURS)]
        shapes.append(
            f'<rect x="{m.x}" y="{m.y}" width="{m.width}" height="{m.height}" '
            f'fill="none" stroke="{colour}" stroke-width="3" '
            f'stroke-dasharray="12 8" />'
        )
        # Staggered: marks sharing a top edge would pile their labels up.
        label_y = max(m.y + 24, 24) + i * 30
        shapes.append(
            f'<text x="{m.x + 10}" y="{label_y}" fill="{colour}" '
            f'font-family="system-ui, sans-serif" font-size="22" '
            f'font-weight="600" '
            f'style="paint-order:stroke;stroke:#000;stroke-width:4px">'
            f"{html.escape(m.name)}</text>"
        )
        notes = []
        if m.clipped_bottom:
            notes.append("extends below the preview")
        if m.clipped_right:
            notes.append("wider than the frame")
        note = f" — {', '.join(notes)}" if notes else ""
        legend.append(
            f'<li><span class="swatch" style="background:{colour}"></span>'
            f"{html.escape(m.name)}{html.escape(note)}</li>"
        )

    return f"""<!doctype html>
<meta charset="utf-8">
<title>camscan-escl preview</title>
<style>
  :root {{ color-scheme: dark light; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem;
         background: #16181d; color: #e8e8ea; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 .75rem; font-weight: 600; }}
  .stage {{ position: relative; max-width: {w}px; }}
  .stage img, .stage svg {{ width: 100%; display: block; }}
  .stage svg {{ position: absolute; inset: 0; pointer-events: none; }}
  ul {{ list-style: none; padding: 0; margin: 1rem 0 0;
        display: flex; gap: 1.25rem; flex-wrap: wrap; font-size: .9rem; }}
  .swatch {{ display: inline-block; width: .8rem; height: .8rem;
             margin-right: .4rem; border-radius: 2px; vertical-align: middle; }}
  .note {{ margin-top: 1rem; font-size: .85rem; color: #a0a4ad;
           max-width: 60ch; line-height: 1.5; }}
  code {{ background: #23262d; padding: .1rem .35rem; border-radius: 3px; }}
</style>
<h1>camscan-escl — position the page</h1>
<div class="stage">
  <img src="/preview/stream" alt="live camera preview">
  <svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">{''.join(shapes)}</svg>
</div>
<ul>{''.join(legend)}</ul>
<script>
  // Belt and braces. The server now holds an MJPEG response open across a
  // scan, but a stream can still die -- daemon restart, network blip -- and
  // a browser never retries a broken <img> stream on its own. Without this
  // the preview goes dead until someone reloads the page.
  (function () {{
    var img = document.querySelector('.stage img');
    var retry = null;
    img.addEventListener('error', function () {{
      if (retry) return;
      retry = setTimeout(function () {{
        retry = null;
        img.src = '/preview/stream?r=' + Date.now();
      }}, 1500);
    }});
  }})();
</script>
<p class="note">
  Marks show where each paper size lands, given
  <code>rig.coverage_mm = [{cfg.rig.coverage_mm[0]:g}, {cfg.rig.coverage_mm[1]:g}]</code>.
  If a sheet you have positioned inside a mark does not come out filling the
  scan, that setting is wrong — it is the measurement the marks and the scans
  both depend on.
</p>
<p class="note">
  The preview is 16:9 and the scan is 3:2, so <strong>the scanner sees
  {hidden_rows} pixel rows more than this, at the {edges}</strong>.
  A mark can leave the preview and still be captured.
</p>
"""
