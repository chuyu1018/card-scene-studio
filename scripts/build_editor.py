#!/usr/bin/env python3
"""Build the shipped, self-contained editor. Run only after changing its sources."""
import base64
import io
import json
import re
from pathlib import Path

from PIL import Image

EDITOR = Path(__file__).resolve().parents[1] / "assets" / "editor"


def build():
    assets = {}
    for path in sorted((EDITOR / "demos").glob("*.png")):
        assets[f"demos/{path.name}"] = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    for path in sorted((EDITOR / "scenes").glob("*.png")):
        with Image.open(path) as image:
            image.thumbnail((900, 1260), Image.Resampling.LANCZOS)
            encoded = io.BytesIO()
            image.save(encoded, format="WEBP", quality=86, method=6)
        assets[f"scenes/{path.name}"] = "data:image/webp;base64," + base64.b64encode(encoded.getvalue()).decode()
    html = (EDITOR / "index.html").read_text()
    html = html.replace('<link rel="stylesheet" href="editor.css">', '<style>' + (EDITOR / "editor.css").read_text() + '</style>')
    html = re.sub(r'src="(demos/[^\"]+)"', r'data-asset="\1"', html)
    html = html.replace("<script>\n'use strict';", '<script>\nconst BUNDLED_ASSETS=' + json.dumps(assets, separators=(",", ":")) + ";\n'use strict';", 1)
    output = EDITOR / "canvas.html"
    output.write_text(html)
    print(json.dumps({"html": str(output), "bytes": output.stat().st_size, "assets": len(assets)}))


if __name__ == "__main__":
    build()
