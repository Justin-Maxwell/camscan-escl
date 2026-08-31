"""The preview page: the marked-up stream, a legend, and what it cannot show.

Deliberately one self-contained file with no assets and no JavaScript
framework. The daemon is a scanner, not a web app, and a positioning aid
that needs a build step would not survive contact with a rig.

The page draws NO geometry of its own. It used to carry an SVG overlay in the
image's coordinate space -- marks, labels, the dead zone, the union box --
every one of which ffmpeg had already burned into the stream underneath it.
So each rectangle appeared twice, in two different palettes, and the legend
swatch matched the SVG rather than the video or the GUI sidebar. Three
colourings of one set of rectangles, and nothing to say which was current.

The burnt-in copy is the one that has to exist: it is what the v4l2loopback
device publishes to Kamoso, and what the GTK window shows, neither of which
can carry an overlay. So the page shows the same picture those do, and adds
only what a picture cannot say -- which colour is which paper size, and how
much scannable area lies outside the frame.
"""

from __future__ import annotations

import html

from .config import Config
from .preview import (MARK_COLOURS, Mark, fence_edge, ghost_axis,
                      preview_size, upright_still, visible_still_region)


def page_html(cfg: Config, marks: list[Mark], geometry: str = "null") -> str:
    # Width only. The height was the SVG overlay's viewBox; with the overlay
    # gone the image sets its own, and capping the width is what stops a
    # 1180-wide stream being blown up to the window.
    w = preview_size(cfg)[0]
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

    legend = []
    for i, m in enumerate(marks):
        # The colour ffmpeg drew this mark in, so the swatch is a promise
        # about a line in the picture rather than about a rectangle this page
        # drew for itself. MARK_COLOURS are CSS keywords as well as ffmpeg
        # ones, and mean the same RGB in both, so there is nothing to convert
        # and nothing to keep in step by hand.
        colour = MARK_COLOURS[i % len(MARK_COLOURS)]
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
  .stage {{ max-width: {w}px; }}
  .stage img {{ width: 100%; display: block; }}
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
  <img src="/preview/stream" alt="live camera preview with the crop marks in it">
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
