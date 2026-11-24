"""Build the Monet app icon (monet.ico, monet.icns, monet-logo.png) from the
square line-art logo (``monet-logo-square.svg`` — the attenuation-curve wave
rising into a light spot, without lettering).

Mirrors Picasso's ``make_picasso_icon.py``: the line-art sits on a white
rounded tile (macOS Big Sur grid: 824/1024 tile, ~22% corner radius) so it
stays visible on dark docks and taskbars, and each size is rendered from the
vector source with its own stroke width so lines never drop below ~1.4 px.

Cross-platform: the ``.ico`` (used by the Qt GUI window and the Windows
installer) and the transparent ``monet-logo.png`` are always written via
Pillow; the macOS ``.icns`` is only built when ``iconutil`` is available.

Usage, from this folder::

    python make_monet_icon.py [logo.svg] [out_dir]

defaults to ``monet-logo-square.svg`` and this folder (``--package`` also
copies ``monet.ico`` into ``monet/icons/`` for the installed GUI).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import QtCore, QtGui, QtSvg  # noqa: E402

TILE = 824 / 1024  # tile side as a fraction of the icon
RADIUS = 0.2237  # tile corner radius as a fraction of the tile
FILL = 0.66  # logo extent as a fraction of the tile
MARGIN = 0.08  # minimum clearance between the logo and the tile edge
STROKE_BOOST = 1.6  # stroke width relative to the source
MIN_STROKE = 1.4  # px in the output

_STROKE_WIDTH = re.compile(r'(stroke-width\s*[:=]\s*"?)([\d.]+)')


def with_stroke(svg, width):
    """Return ``svg`` with every stroke width set to ``width``."""
    return _STROKE_WIDTH.sub(lambda m: f"{m.group(1)}{width:.6g}", svg)


def renderer(svg):
    r = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode()))
    if not r.isValid():
        raise ValueError("cannot parse the SVG")
    return r


def alpha(image):
    """Alpha channel of an ARGB32 QImage as a uint8 array."""
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    rows = np.frombuffer(ptr, np.uint8).reshape(
        image.height(), image.bytesPerLine()
    )
    return (
        rows[:, : 4 * image.width()]
        .reshape(image.height(), image.width(), 4)[..., 3]
        .copy()
    )


def new_image(size):
    image = QtGui.QImage(
        size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied
    )
    image.fill(QtCore.Qt.GlobalColor.transparent)
    return image


def paint_tile(image, tile):
    """Fill a rounded square of side ``tile`` centered in ``image``."""
    off = (image.width() - tile) / 2
    path = QtGui.QPainterPath()
    path.addRoundedRect(
        QtCore.QRectF(off, off, tile, tile), RADIUS * tile, RADIUS * tile
    )
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.fillPath(path, QtCore.Qt.GlobalColor.white)
    painter.end()


def content_bounds(svg):
    """Bounds of the drawn line (center line), in SVG user units."""
    view = renderer(svg).viewBoxF()
    res = 4096 / max(view.width(), view.height())  # px per unit
    image = QtGui.QImage(
        round(view.width() * res),
        round(view.height() * res),
        QtGui.QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    renderer(with_stroke(svg, 2 / res)).render(painter)
    painter.end()
    ys, xs = np.nonzero(alpha(image))
    return QtCore.QRectF(
        view.x() + (xs.min() + 1) / res,
        view.y() + (ys.min() + 1) / res,
        (xs.max() - xs.min() - 1) / res,
        (ys.max() - ys.min() - 1) / res,
    )


def render(svg, bounds, stroke_src, size, tiled=True):
    tile = TILE * size
    scale = FILL * tile / max(bounds.width(), bounds.height())  # px/unit
    stroke = max(stroke_src * scale * STROKE_BOOST, MIN_STROKE) / scale
    logo = renderer(with_stroke(svg, stroke))
    half = size / 2 / scale
    center = bounds.center()
    logo.setViewBox(
        QtCore.QRectF(center.x() - half, center.y() - half, 2 * half, 2 * half)
    )

    glyph = new_image(size)
    painter = QtGui.QPainter(glyph)
    logo.render(painter)
    painter.end()
    safe = new_image(size)
    paint_tile(safe, (1 - 2 * MARGIN) * tile)
    if (alpha(glyph) > alpha(safe).astype(int) + 32).any():
        raise ValueError(
            f"the logo comes closer than MARGIN to the tile edge at {size} "
            "px; lower FILL"
        )
    if not tiled:
        return glyph  # transparent line-art, no tile (for docs/README)

    icon = new_image(size)
    paint_tile(icon, tile)
    painter = QtGui.QPainter(icon)
    painter.drawImage(0, 0, glyph)
    painter.end()
    return icon


def _qimage_to_pil(qimg):
    qimg = qimg.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    ptr = qimg.constBits()
    ptr.setsize(qimg.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(qimg.height(), qimg.width(), 4)
    return Image.fromarray(arr.copy(), "RGBA")


def main(logo_path, out_dir, package=False):
    QtGui.QGuiApplication.instance() or QtGui.QGuiApplication(sys.argv[:1])
    with open(logo_path, encoding="utf-8") as f:
        svg = f.read()
    widths = {float(m.group(2)) for m in _STROKE_WIDTH.finditer(svg)}
    if len(widths) != 1:
        raise ValueError(f"expected one stroke width, found {widths}")
    stroke_src = widths.pop()
    bounds = content_bounds(svg)

    sizes = [16, 24, 32, 48, 64, 128, 256, 512, 1024]
    with tempfile.TemporaryDirectory() as tmp:
        pngs = {}
        for s in sizes:
            pngs[s] = os.path.join(tmp, f"{s}.png")
            render(svg, bounds, stroke_src, s).save(pngs[s])

        # Windows / Qt .ico (Pillow, cross-platform)
        ico_sizes = [16, 24, 32, 48, 64, 128, 256]
        imgs = {s: Image.open(pngs[s]) for s in ico_sizes}
        ico_path = os.path.join(out_dir, "monet.ico")
        imgs[256].save(
            ico_path,
            format="ICO",
            sizes=[(s, s) for s in ico_sizes],
            append_images=[imgs[s] for s in ico_sizes[:-1]],
        )
        print("wrote", ico_path)

        # transparent line-art for docs / README
        logo_png = os.path.join(out_dir, "monet-logo.png")
        _qimage_to_pil(
            render(svg, bounds, stroke_src, 1024, tiled=False)
        ).save(logo_png)
        print("wrote", logo_png)

        # macOS .icns (needs iconutil)
        if shutil.which("iconutil"):
            iconset = os.path.join(tmp, "monet.iconset")
            os.mkdir(iconset)
            for s in [16, 32, 128, 256, 512]:
                os.link(pngs[s], os.path.join(iconset, f"icon_{s}x{s}.png"))
                os.link(
                    pngs[2 * s],
                    os.path.join(iconset, f"icon_{s}x{s}@2x.png"),
                )
            icns = os.path.join(out_dir, "monet.icns")
            subprocess.run(
                ["iconutil", "-c", "icns", iconset, "-o", icns], check=True
            )
            print("wrote", icns)
        else:
            print("iconutil not found — skipping monet.icns (macOS only)")

        if package:
            here = os.path.dirname(os.path.abspath(__file__))
            pkg = os.path.normpath(
                os.path.join(here, "..", "..", "monet", "icons")
            )
            os.makedirs(pkg, exist_ok=True)
            shutil.copy2(ico_path, os.path.join(pkg, "monet.ico"))
            print("copied monet.ico ->", pkg)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    argv = [a for a in sys.argv[1:] if a != "--package"]
    package = "--package" in sys.argv
    logo_path = (
        argv[0] if argv else os.path.join(here, "monet-logo-square.svg")
    )
    out_dir = argv[1] if len(argv) > 1 else here
    main(logo_path, out_dir, package=package)
