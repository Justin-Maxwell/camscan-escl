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
from . import preview as preview_mod
from .preview import (MARK_THICKNESS, UNION_THICKNESS, Mark, fence_edge,
                      fit_transform, ghost_axis, ghost_rect, outside_of,
                      preview_size, union_rect, upright_still,
                      visible_still_region)

# Distinct hues, readable against paper and against a dark desk alike.
COLOURS = ("#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4")

# The overflow tint. Hard-coded rather than read from preview.overflow_colour,
# which is an ffmpeg colour spec and not a CSS one; kept at the same hue and
# opacity so the page and the stream say the same thing.
OVERFLOW_COLOUR = "#ff4500"      # orangered
OVERFLOW_OPACITY = "0.4"


def page_html(cfg: Config, marks: list[Mark], geometry: str = "null") -> str:
    w, h = preview_size(cfg)
    # How much scannable area sits outside the picture, per edge. Both edges
    # hide a strip on an unfenced rig; a fence removes the strip on the edge
    # it stands on, leaving only the far one. After a transpose the strips are
    # at the sides rather than the top and bottom.
    axis = ghost_axis(cfg)
    x0, y0, x1, y1 = visible_still_region(cfg)
    span = upright_still(cfg)[axis]
    names = ("left", "right") if axis == 0 else ("top", "bottom")
    hidden = [(name, px) for name, px in
              zip(names, (int(round((x0, y0)[axis])),
                          int(round(span - (x1, y1)[axis]))))
              if px > 0]
    if not hidden:
        ghost_note = "the preview shows the whole scannable area"
    else:
        ghost_note = (
            f"the scanner sees {max(px for _n, px in hidden)} pixel rows more "
            f"than this, at the "
            + " and at the ".join(name for name, _px in hidden)
        )
    edge = fence_edge(cfg)
    fenced = names[0] if edge == "low" else names[1] if edge == "high" else None
    fence_note = "" if fenced is None else (
        f'<p class="note">The <strong>{fenced}</strong> edge is fenced. The '
        f"scannable area stops where the picture stops there, so a sheet "
        f"pushed against the rail is registered where you can watch it land."
        f"</p>"
    )

    # The dead zone: everything outside the union of the paper sizes can
    # never appear in any scan. One path with evenodd, which SVG can express
    # and drawbox cannot.
    ux, uy, uw, uh = union_rect(marks)
    shapes = [
        f'<path d="M0,0 H{w} V{h} H0 Z M{ux},{uy} h{uw} v{uh} h{-uw} Z" '
        f'fill="#000" fill-opacity="0.55" fill-rule="evenodd" />',
        f'<rect x="{ux}" y="{uy}" width="{uw}" height="{uh}" fill="none" '
        f'stroke="#fff" stroke-opacity="0.85" '
        f'stroke-width="{UNION_THICKNESS}" />',
    ]

    # The same warning the stream carries, in the same place: the parts of a
    # mark outside the canvas are sheet no scan reaches. The page draws it
    # itself rather than relying on the burnt-in one, because the two are
    # generated from one geometry and disagreeing about it is the bug this
    # overlay exists to catch.
    canvas = ghost_rect(cfg, *fit_transform(cfg, preview_mod.marks(cfg)))
    for m in marks:
        for bx, by, bw, bh in outside_of((m.x, m.y, m.width, m.height), canvas):
            shapes.append(
                f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" '
                f'fill="{OVERFLOW_COLOUR}" fill-opacity="{OVERFLOW_OPACITY}" />'
            )

    legend = []
    for i, m in enumerate(marks):
        colour = COLOURS[i % len(COLOURS)]
        shapes.append(
            f'<rect x="{m.x}" y="{m.y}" width="{m.width}" height="{m.height}" '
            f'fill="none" stroke="{colour}" '
            f'stroke-width="{MARK_THICKNESS}" stroke-dasharray="12 8" />'
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
        # All four edges. An anchor on the right or the bottom pushes an
        # oversized sheet off the low edges instead, and naming only two of
        # them reported those sheets as fitting.
        overflows = [name for name, flag in (
            ("left", m.clipped_left), ("top", m.clipped_top),
            ("right", m.clipped_right), ("bottom", m.clipped_bottom),
        ) if flag]
        if not overflows:
            note = ""
        else:
            edges = (overflows[0] if len(overflows) == 1
                     else ", ".join(overflows[:-1]) + " and " + overflows[-1])
            note = (f" — runs past the {edges} of the scannable area, "
                    f"and is not captured there")
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

  // The overlay is server-rendered, so a settings change from the GUI moved
  // the marks in the video underneath while these stayed where they were --
  // two sets of crop marks disagreeing, with no way to tell which was
  // current. Poll the geometry the overlay was drawn from and reload when it
  // moves. Its own endpoint, because /preview/settings asks the camera for
  // its mode list and that has no business on a two-second timer.
  (function () {{
    var drawn = JSON.stringify({geometry});
    setInterval(function () {{
      fetch('/preview/geometry', {{cache: 'no-store'}})
        .then(function (r) {{ return r.ok ? r.json() : null; }})
        .then(function (g) {{
          if (g && JSON.stringify(g) !== drawn) location.reload();
        }})
        .catch(function () {{}});   // daemon restarting; try again next tick
    }}, 2000);
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
  The preview is 16:9 and the scan is 3:2, so <strong>{ghost_note}</strong>.
  A mark can leave the preview and still be captured.
</p>
{fence_note}
"""
