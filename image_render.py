"""Render a report table (title + monospace grid) to a PNG.

Slack code blocks are the right shape on a desktop but wrap and shrink on a
phone, where most people actually read the morning cost report. An image of the
same monospace grid keeps the columns aligned at any width and reads cleanly on a
small screen.

Everything is drawn in ONE monospace family, varying only weight and size, so the
column maths stays trivial (every glyph is the same width) and the result matches
the aligned code-block tables the text path already produces. Matplotlib ships its
own DejaVu fonts, so this needs no system fonts in the container.
"""

import logging

log = logging.getLogger("cost-anomaly.image")

# DejaVu Sans Mono advances at ~0.602 em; a hair of headroom avoids clipping the
# last glyph of the widest line.
_CHAR_W = 0.62
_LINE_H = 1.72
_PAD = 30           # points of margin on every side
_DPI = 200          # crisp on a retina phone

_BG = "#ffffff"
_INK = "#1d1d21"
_TITLE = "#111318"
_MUTED = "#6b6f76"


def render(lines: list[dict], path: str) -> str:
    """Draw pre-styled lines top-to-bottom onto a white card and save a PNG.

    Each line: {"text": str, "size": int, "bold": bool, "color": str|None}, and
    optionally "segments": [(text, colour|None), …] to colour parts of the line
    (e.g. a green/red percentage). The monospace grid means a segment starting at
    character N sits at a fixed x, so coloured spans stay column-aligned.
    Returns the path, so callers can inline it into an upload.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lines = [l for l in lines if l.get("text", "").strip() or l.get("spacer")]
    if not lines:
        lines = [{"text": "(no data)", "size": 13, "bold": False, "color": _MUTED}]

    heights = [l["size"] * _LINE_H for l in lines]
    widths = [len(l["text"]) * l["size"] * _CHAR_W for l in lines]
    w_pt = _PAD * 2 + max(widths + [1.0])
    h_pt = _PAD * 2 + sum(heights)

    fig = plt.figure(figsize=(w_pt / 72.0, h_pt / 72.0), dpi=_DPI)
    fig.patch.set_facecolor(_BG)

    cursor = _PAD
    for line, h in zip(lines, heights):
        y = 1.0 - (cursor + h / 2.0) / h_pt
        size = line["size"]
        weight = "bold" if line.get("bold") else "normal"
        base = line.get("color") or _INK
        segments = line.get("segments")
        if segments:
            char_w = size * _CHAR_W
            col = 0
            for text, color in segments:
                if text:
                    fig.text((_PAD + col * char_w) / w_pt, y, text, fontsize=size,
                             fontfamily="monospace", fontweight=weight,
                             color=color or base, va="center", ha="left")
                col += len(text)
        else:
            fig.text(_PAD / w_pt, y, line["text"], fontsize=size,
                     fontfamily="monospace", fontweight=weight,
                     color=base, va="center", ha="left")
        cursor += h

    fig.savefig(path, dpi=_DPI, facecolor=_BG)
    plt.close(fig)
    return path
