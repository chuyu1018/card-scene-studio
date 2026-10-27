#!/usr/bin/env python3
"""Create a card-background project from natural-language choices resolved by Codex.

The user does not need the editor or pixel coordinates. This helper preserves the
original image files, fits copies inside the requested outline, and uses the same
project storage and validation as the local editor. It does not generate images.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
from pathlib import Path
import sys

from PIL import Image, ImageOps

from render import grid_bounds, shape_vertices
from server import MAX_IMAGE, store_project

PRESETS = {
    "1:1": (2400, 2400, "rectangle"),
    "1:2": (1200, 2400, "rectangle"),
    "2:1": (2400, 1200, "rectangle"),
    "16:9": (2400, 1350, "rectangle"),
    "9:16": (1350, 2400, "rectangle"),
    "hex-pointy": (2078, 2400, "hex-pointy"),
    "hex-flat": (2400, 2078, "hex-flat"),
}
ANCHORS = {"center", "left", "right", "top", "bottom"}
MARGIN = 2.0


def _clip_half_plane(polygon, nx, ny, threshold):
    """Keep nx*x + ny*y >= threshold, preserving convex polygon order."""
    if not polygon:
        return []
    result = []
    previous = polygon[-1]
    previous_distance = nx * previous[0] + ny * previous[1] - threshold
    for current in polygon:
        current_distance = nx * current[0] + ny * current[1] - threshold
        previous_inside = previous_distance >= -1e-8
        current_inside = current_distance >= -1e-8
        if previous_inside != current_inside:
            denominator = previous_distance - current_distance
            if abs(denominator) > 1e-12:
                fraction = previous_distance / denominator
                result.append((previous[0] + fraction * (current[0] - previous[0]),
                               previous[1] + fraction * (current[1] - previous[1])))
        if current_inside:
            result.append(current)
        previous, previous_distance = current, current_distance
    return result


def _centre_region(width, height, shape, box_width, box_height, bounds):
    """Erode the canvas and a layout cell by a card rectangle plus safe margin."""
    left, top, right, bottom = bounds
    polygon = [(left, top), (right, top), (right, bottom), (left, bottom)]
    outlines = [shape_vertices(width, height, shape), polygon.copy()]
    for outline in outlines:
        for index, (ax, ay) in enumerate(outline):
            bx, by = outline[(index + 1) % len(outline)]
            nx, ny = -(by - ay), bx - ax
            threshold = nx * ax + ny * ay + abs(nx) * box_width / 2 + abs(ny) * box_height / 2
            threshold += MARGIN * math.hypot(nx, ny)
            polygon = _clip_half_plane(polygon, nx, ny, threshold)
            if not polygon:
                return []
    return polygon


def _nearest_point(target, polygon):
    if all((polygon[(i + 1) % len(polygon)][0] - a[0]) * (target[1] - a[1])
           - (polygon[(i + 1) % len(polygon)][1] - a[1]) * (target[0] - a[0]) >= -1e-7
           for i, a in enumerate(polygon)):
        return target
    choices = []
    for index, a in enumerate(polygon):
        b = polygon[(index + 1) % len(polygon)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        squared = dx * dx + dy * dy
        fraction = max(0, min(1, ((target[0] - a[0]) * dx + (target[1] - a[1]) * dy) / squared)) if squared else 0
        point = (a[0] + fraction * dx, a[1] + fraction * dy)
        choices.append((math.dist(target, point), point))
    return min(choices, key=lambda pair: pair[0])[1]


def place_card(width, height, shape, aspect, requested_width, requested_height, centre, bounds=None):
    """Find a proportional rectangle completely within the outline/cell intersection."""
    bounds = bounds or (0, 0, width, height)
    box_width = min(requested_width, requested_height * aspect)
    box_height = box_width / aspect
    polygon = _centre_region(width, height, shape, box_width, box_height, bounds)
    if not polygon:
        low, high = 0.0, 1.0
        for _ in range(50):
            scale = (low + high) / 2
            if _centre_region(width, height, shape, box_width * scale, box_height * scale, bounds):
                low = scale
            else:
                high = scale
        # Stay just inside numerical boundary so rounding cannot clip original pixels.
        scale = low * 0.99999
        box_width, box_height = box_width * scale, box_height * scale
        polygon = _centre_region(width, height, shape, box_width, box_height, bounds)
    if not polygon or min(box_width, box_height) < 1:
        raise ValueError("所选画布与卡位没有足够空间放置完整图片，请换一个卡位或画布")
    cx, cy = _nearest_point(centre, polygon)
    return {"x": cx - box_width / 2, "y": cy - box_height / 2,
            "width": box_width, "height": box_height}


def _load_original(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"图片不存在：{path}")
    if path.stat().st_size > MAX_IMAGE:
        raise ValueError(f"单张图片不能超过 20 MB：{path.name}")
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as original:
        mime = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}.get(original.format)
        if not mime:
            raise ValueError(f"仅支持 PNG、JPEG、WebP：{path.name}")
        if original.width * original.height > 40_000_000:
            raise ValueError(f"图片不能超过四千万像素：{path.name}")
        oriented = ImageOps.exif_transpose(original)
        oriented.load()
        aspect = oriented.width / oriented.height
    return {"name": path.name,
            "dataUrl": f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii"),
            "aspect": aspect}


def create_project(root, images, mode="single", preset="9:16", anchor="center", slot=5, prompt=""):
    if mode not in {"single", "multi", "grid"}:
        raise ValueError("模式必须是 single、multi 或 grid")
    if preset not in PRESETS or anchor not in ANCHORS:
        raise ValueError("不支持的画布比例或位置")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 9:
        raise ValueError("九宫格卡位必须是 1 至 9")
    images = list(images)
    if not 1 <= len(images) <= 12:
        raise ValueError("请提供 1 至 12 张原图")
    if mode == "single" and len(images) != 1:
        raise ValueError("单卡模式仅接受一张原图")
    if mode == "grid" and len(images) > 9:
        raise ValueError("九宫格最多接受九张原图")
    originals = [_load_original(path) for path in images]
    width, height, shape = PRESETS[preset]
    grid = {"rows": 3, "cols": 3, "slot": slot - 1}
    if mode == "grid":
        ratio = originals[0]["aspect"]
        cw = max(1, round(1024 * ratio)) if ratio <= 1 else 1024
        ch = 1024 if ratio <= 1 else max(1, round(1024 / ratio))
        width, height, shape, preset = cw * 3, ch * 3, "rectangle", None
        grid.update(layout="card-sized", cellWidth=cw, cellHeight=ch)
    cards = []
    if mode == "single":
        location = {"center": (0.5, 0.5), "left": (0.25, 0.5), "right": (0.75, 0.5),
                    "top": (0.5, 0.25), "bottom": (0.5, 0.75)}[anchor]
        original = originals[0]
        geometry = place_card(width, height, shape, original["aspect"], height * 0.5 * original["aspect"], height * 0.5,
                              (width * location[0], height * location[1]))
        cards.append({"id": "card-01", **original, **geometry})
    elif mode == "grid":
        slots = [6, 4, 2] if len(originals) == 3 else [slot - 1] + [i for i in range(9) if i != slot - 1]
        for index, original in enumerate(originals):
            chosen_slot = slots[index]
            left, top, right, bottom = grid_bounds(width, height)[chosen_slot]
            card_width = min(right - left, (bottom - top) * original["aspect"])
            card_height = card_width / original["aspect"]
            geometry = {"x": left + (right - left - card_width) / 2,
                        "y": top + (bottom - top - card_height) / 2,
                        "width": card_width, "height": card_height}
            cards.append({"id": f"card-{index + 1:02d}", **original, **geometry, "slot": chosen_slot})
    else:
        count = len(originals)
        group_width, group_height = width * .84, height * .76
        candidates = []
        for columns in range(1, count + 1):
            rows = math.ceil(count / columns)
            cw, ch = group_width / columns, group_height / rows
            size = min(650, *(min(cw * .86 / min(1, o["aspect"]),
                                  ch * .72 / min(1, 1 / o["aspect"])) for o in originals))
            candidates.append((size - abs(columns - min(2, count)) * .001, columns, cw, ch, size))
        _, columns, cell_width, cell_height, size = max(candidates)
        group_centre = {"center": (.5, .5), "left": (.42, .5), "right": (.58, .5),
                        "top": (.5, .42), "bottom": (.5, .58)}[anchor]
        for index, original in enumerate(originals):
            row, column = divmod(index, columns)
            count_in_row = min(columns, count - row * columns)
            left = width * group_centre[0] - group_width / 2 + (column + (columns - count_in_row) / 2) * cell_width
            top = height * group_centre[1] - group_height / 2 + row * cell_height
            stagger = 0 if count == 1 else (1 if column % 2 else -1) * cell_height * .15
            geometry = place_card(width, height, shape, original["aspect"],
                                  size * min(1, original["aspect"]), size * min(1, 1 / original["aspect"]),
                                  (left + cell_width / 2, top + cell_height / 2 + stagger),
                                  (left, top, left + cell_width, top + cell_height))
            cards.append({"id": f"card-{index + 1:02d}", **original, **geometry})
    for card in cards:
        card.pop("aspect")
    payload = {"version": 1, "mode": mode,
               "canvas": {"width": width, "height": height, "dpi": 300, "shape": shape, "preset": preset},
               "grid": grid,
               "style": {"preset": "follow", "prompt": prompt, "backgroundOnly": True},
               "cards": cards}
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return store_project(root, payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--images", required=True, nargs="+", type=Path)
    parser.add_argument("--mode", choices=["single", "multi", "grid"], default="single")
    parser.add_argument("--preset", choices=list(PRESETS), default="9:16")
    parser.add_argument("--anchor", choices=["center", "left", "right", "top", "bottom"], default="center")
    parser.add_argument("--slot", type=int, choices=range(1, 10), default=5)
    parser.add_argument("--prompt", default="", help="Optional environmental preferences; artwork style follows the originals")
    args = parser.parse_args(argv)
    try:
        result = create_project(args.root, args.images, args.mode, args.preset, args.anchor, args.slot, args.prompt)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
