#!/usr/bin/env python3
"""Prepare references and assemble an independently generated background.

This module never generates imagery. Native image generation happens in Codex;
this code only validates, resizes, masks, composites, and tiles saved images.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

from PIL import Image, ImageChops, ImageDraw, ImageOps

MAX_EDGE = 8192
MAX_PIXELS = 40_000_000
MODES = {"single", "multi", "grid"}
PRESETS = {"follow", "watercolor", "anime", "painterly"}
SHAPES = {"rectangle", "hex-pointy", "hex-flat"}
CANVAS_PRESETS = {"1:1", "1:2", "2:1", "16:9", "9:16", "hex-pointy", "hex-flat"}


class ProjectError(ValueError):
    """A project cannot be safely or faithfully rendered."""


def _number(value, label, low=0, high=MAX_EDGE):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectError(f"{label} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ProjectError(f"{label} must be between {low} and {high}")
    return value


def _integer(value, label, low=0, high=MAX_EDGE):
    _number(value, label, low, high)
    if not isinstance(value, int):
        raise ProjectError(f"{label} must be a whole number")
    return value


def _source_path(project_path, src):
    if not isinstance(src, str) or not src:
        raise ProjectError("Every card needs a relative src path")
    path = Path(src)
    root = Path(project_path).resolve().parent
    if path.is_absolute() or not path.parts or path.parts[0] != "inputs":
        raise ProjectError("Card src must be relative and inside the project's inputs/ directory")
    try:
        resolved = (root / path).resolve()
    except (OSError, ValueError) as exc:
        raise ProjectError(f"Invalid card source path: {src!r}") from exc
    if not resolved.is_relative_to(root / "inputs") or not resolved.is_file():
        raise ProjectError(f"Card source is missing or outside the project's inputs/ directory: {src}")
    return resolved


def _open_image(path):
    try:
        with Image.open(path) as raw:
            if raw.format not in {"PNG", "JPEG", "WEBP"}:
                raise ProjectError(f"Unsupported image type: {path}")
            if raw.width * raw.height > MAX_PIXELS:
                raise ProjectError("Image has more than 40 million pixels")
            image = ImageOps.exif_transpose(raw)
            image.load()
            return image.convert("RGBA")
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"Cannot open image {path}: {exc}") from exc


def grid_bounds(width, height):
    """Nine row-major bounds covering each pixel once, including odd dimensions."""
    xs = [round(i * width / 3) for i in range(4)]
    ys = [round(i * height / 3) for i in range(4)]
    return [(xs[c], ys[r], xs[c + 1], ys[r + 1])
            for r in range(3) for c in range(3)]


def card_grid_slot(project, card):
    """Per-card slots extend the legacy one-card grid.slot format."""
    fallback = project.get("grid", {}).get("slot") if len(project["cards"]) == 1 else None
    return _integer(card.get("slot", fallback), "Card grid slot", 0, 8)


def shape_vertices(width, height, shape="rectangle"):
    """Polygon coordinates use outer canvas edges, not the last pixel indices."""
    if shape == "rectangle":
        return [(0, 0), (width, 0), (width, height), (0, height)]
    if shape == "hex-pointy":
        return [(width / 2, 0), (width, height / 4), (width, 3 * height / 4),
                (width / 2, height), (0, 3 * height / 4), (0, height / 4)]
    if shape == "hex-flat":
        return [(width / 4, 0), (3 * width / 4, 0), (width, height / 2),
                (3 * width / 4, height), (width / 4, height), (0, height / 2)]
    raise ProjectError("Canvas shape must be rectangle, hex-pointy, or hex-flat")


def point_in_shape(x, y, width, height, shape="rectangle", tolerance=0.51):
    """Convex half-plane check; tolerance is a perpendicular distance in pixels."""
    polygon = shape_vertices(width, height, shape)
    for index, (ax, ay) in enumerate(polygon):
        bx, by = polygon[(index + 1) % len(polygon)]
        dx, dy = bx - ax, by - ay
        if dx * (y - ay) - dy * (x - ax) < -tolerance * math.hypot(dx, dy):
            return False
    return True


def shape_mask(width, height, shape="rectangle"):
    """Rasterize by pixel centres, keeping deterministic opaque/transparent edges."""
    if shape == "rectangle":
        return Image.new("L", (width, height), 255)
    polygon = shape_vertices(width, height, shape)
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for row in range(height):
        py = row + 0.5
        intersections = []
        for index, (ax, ay) in enumerate(polygon):
            bx, by = polygon[(index + 1) % len(polygon)]
            if min(ay, by) <= py < max(ay, by):
                intersections.append(ax + (py - ay) * (bx - ax) / (by - ay))
        if len(intersections) >= 2:
            left = max(0, math.ceil(min(intersections) - 0.5 - 1e-9))
            right = min(width - 1, math.floor(max(intersections) - 0.5 + 1e-9))
            if right >= left:
                draw.line((left, row, right, row), fill=255)
    return mask


def _ensure_card_pixels_inside_shape(layers, mask):
    for layer in layers:
        if ImageChops.subtract(layer["footprint"], mask.crop(layer["box"])).getbbox():
            raise ProjectError(f"Card {layer['id']} touches the shape boundary after pixel rounding; "
                               "move it at least one pixel inward so the original stays complete")


def card_corners(card):
    """Clockwise screen-space rotation about the unrotated box's centre."""
    angle = math.radians(card.get("rotation", 0))
    cosine, sine = math.cos(angle), math.sin(angle)
    w, h = card["width"], card["height"]
    cx, cy = card["x"] + w / 2, card["y"] + h / 2
    return [(cx + dx * cosine - dy * sine, cy + dx * sine + dy * cosine)
            for dx, dy in [(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)]]


def validate_project(project_path):
    project_path = Path(project_path).resolve()
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProjectError(f"Cannot read project JSON: {exc}") from exc
    if not isinstance(project, dict) or type(project.get("version")) is not int or project.get("version") != 1:
        raise ProjectError("Project version must be 1")
    if not isinstance(project.get("mode"), str) or project["mode"] not in MODES:
        raise ProjectError("Mode must be single, multi, or grid")
    canvas = project.get("canvas")
    if not isinstance(canvas, dict):
        raise ProjectError("Canvas settings are required")
    width = _integer(canvas.get("width"), "Canvas width", 3)
    height = _integer(canvas.get("height"), "Canvas height", 3)
    if width * height > MAX_PIXELS:
        raise ProjectError("Canvas has more than 40 million pixels")
    _number(canvas.get("dpi", 300), "DPI", 36, 1200)
    shape = canvas.get("shape", "rectangle")
    if not isinstance(shape, str) or shape not in SHAPES:
        raise ProjectError("Canvas shape must be rectangle, hex-pointy, or hex-flat")
    canvas_preset = canvas.get("preset")
    if canvas_preset is not None and (not isinstance(canvas_preset, str) or canvas_preset not in CANVAS_PRESETS):
        raise ProjectError("Unsupported canvas preset")
    if shape != "rectangle":
        expected_ratio = math.sqrt(3) / 2 if shape == "hex-pointy" else 2 / math.sqrt(3)
        if abs(width - height * expected_ratio) > 1.1:
            raise ProjectError("Hexagon dimensions must form a regular hexagon (allowing pixel rounding)")
    style = project.get("style")
    if not isinstance(style, dict) or not isinstance(style.get("preset"), str) or style["preset"] not in PRESETS:
        raise ProjectError("A supported style preset is required")
    if style.get("backgroundOnly") is not True:
        raise ProjectError("This workflow requires backgroundOnly: true")
    if not isinstance(style.get("prompt", ""), str):
        raise ProjectError("Style prompt must be text")
    cards = project.get("cards")
    if not isinstance(cards, list) or not 1 <= len(cards) <= 12:
        raise ProjectError("Provide between 1 and 12 cards")
    if project["mode"] == "single" and len(cards) != 1:
        raise ProjectError("Single mode requires exactly one card")
    if project["mode"] == "grid":
        if len(cards) > 9:
            raise ProjectError("Grid mode accepts at most nine cards")
        grid = project.get("grid", {})
        if not isinstance(grid, dict) or grid.get("rows") != 3 or grid.get("cols") != 3:
            raise ProjectError("Grid mode requires exactly 3 rows and 3 columns")
        _integer(grid.get("slot", 4), "Grid slot", 0, 8)
        if grid.get("layout") == "card-sized":
            if shape != "rectangle" or width % 3 or height % 3:
                raise ProjectError("Card-sized grids require nine equal rectangular cells")
            if grid.get("cellWidth") != width // 3 or grid.get("cellHeight") != height // 3:
                raise ProjectError("Card-sized grid dimensions must match the canvas")
    occupied_slots = set()
    ids = set()
    for card in cards:
        if not isinstance(card, dict):
            raise ProjectError("Cards must be objects")
        card_id = card.get("id")
        if not isinstance(card_id, str) or not card_id or card_id in ids:
            raise ProjectError("Card IDs must be unique nonempty strings")
        ids.add(card_id)
        rotation = _number(card.get("rotation", 0), "Card rotation", -180, 180)
        diagonal = math.hypot(width, height)
        x = _number(card.get("x"), "Card x", -diagonal, width)
        y = _number(card.get("y"), "Card y", -diagonal, height)
        w = _number(card.get("width"), "Card width", 1, diagonal)
        h = _number(card.get("height"), "Card height", 1, diagonal)
        corners = card_corners(card)
        if any(cx < -0.01 or cy < -0.01 or cx > width + 0.01 or cy > height + 0.01
               for cx, cy in corners):
            raise ProjectError("A card extends beyond the canvas")
        if not all(point_in_shape(cx, cy, width, height, shape)
                   for cx, cy in corners):
            raise ProjectError("All four card corners must stay inside the canvas shape")
        if project["mode"] == "grid":
            if rotation != 0:
                raise ProjectError("Grid cards must stay upright; rotation is available in single and multi mode")
            slot = card_grid_slot(project, card)
            if slot in occupied_slots:
                raise ProjectError("Each grid card needs a different reserved slot")
            occupied_slots.add(slot)
            l, t, r, b = grid_bounds(width, height)[slot]
            if x < l - 0.51 or y < t - 0.51 or x + w > r + 0.51 or y + h > b + 0.51:
                raise ProjectError("The grid card must stay inside its reserved slot")
        original = _open_image(_source_path(project_path, card.get("src")))
        if project["mode"] == "grid" and project["grid"].get("layout") == "card-sized":
            ratio = original.width / original.height
            expected_w = min(r - l, (b - t) * ratio)
            expected_h = expected_w / ratio
            expected = (l + (r - l - expected_w) / 2,
                        t + (b - t - expected_h) / 2, expected_w, expected_h)
            if any(abs(actual - wanted) > 0.01 for actual, wanted in zip((x, y, w, h), expected)):
                raise ProjectError("Card-sized grid images must be fitted and centred in their cells")
    return project


def _card_layers(project_path, project, environments=None):
    """Derive placement from originals, optionally replacing pixels with pure scenery."""
    layers = []
    for card in project["cards"]:
        source = _source_path(project_path, card["src"])
        image = _open_image(source)
        requested = (card["x"], card["y"], card["width"], card["height"])
        w, h = max(1, round(card["width"])), max(1, round(card["height"]))
        fitted = ImageOps.contain(image, (w, h), Image.Resampling.LANCZOS)
        x = round(card["x"] + (card["width"] - fitted.width) / 2)
        y = round(card["y"] + (card["height"] - fitted.height) / 2)
        # Keep all rounded pixels within the chosen slot, including odd-size grids.
        limits = (0, 0, project["canvas"]["width"], project["canvas"]["height"])
        if project["mode"] == "grid":
            limits = grid_bounds(project["canvas"]["width"], project["canvas"]["height"])[card_grid_slot(project, card)]
            if fitted.width > limits[2] - limits[0] or fitted.height > limits[3] - limits[1]:
                fitted = ImageOps.contain(image, (limits[2] - limits[0], limits[3] - limits[1]), Image.Resampling.LANCZOS)
        x = min(max(limits[0], x), limits[2] - fitted.width)
        y = min(max(limits[1], y), limits[3] - fitted.height)
        environment_info = None
        if environments is not None:
            environment = environments[card["id"]]
            scenery = environment["image"]
            ratio_error = abs(scenery.width / scenery.height / (image.width / image.height) - 1)
            if ratio_error > 0.002:
                raise ProjectError(f"Environment for {card['id']} has a different aspect ratio from its original card")
            # Register to the original aspect before applying its rounded output size.
            # Fitting directly to that rounded size would crop differently from the original.
            original_ratio = image.width / image.height
            crop_width = min(scenery.width, scenery.height * original_ratio)
            crop_height = min(scenery.height, scenery.width / original_ratio)
            left, top = (scenery.width - crop_width) / 2, (scenery.height - crop_height) / 2
            source_crop = (left, top, left + crop_width, top + crop_height)
            fitted = scenery.resize(fitted.size, Image.Resampling.LANCZOS, box=source_crop)
            environment_info = {key: value for key, value in environment.items() if key != "image"}
            environment_info.update(fitted_size=list(fitted.size), source_aspect_error=ratio_error,
                                    source_crop=list(source_crop),
                                    fit="uniform rounding crop to original aspect, then identical original-card pixel sizing")
        rotation = card.get("rotation", 0)
        footprint = Image.new("L", fitted.size, 255)
        if rotation:
            fitted = fitted.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
            footprint = footprint.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
            x = round(card["x"] + card["width"] / 2 - fitted.width / 2)
            y = round(card["y"] + card["height"] / 2 - fitted.height / 2)
        layers.append({"id": card["id"], "name": card.get("name", card["id"]),
                       "src": card["src"], "image": fitted, "footprint": footprint,
                       "rotation": rotation,
                       "source_size": list(image.size), "requested_box": list(requested),
                       "box": [x, y, x + fitted.width, y + fitted.height],
                       "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        if environment_info is not None:
            layers[-1]["environment"] = environment_info
    return layers


def _layer_info(layer):
    return {key: value for key, value in layer.items() if key not in {"image", "footprint"}}


def _save_image(image, path, dpi):
    image.save(path, dpi=(dpi, dpi))


def _prompt_text(project, layers):
    canvas = project["canvas"]
    preset = {"follow": "Match the supplied artwork's medium, lighting, palette, and environment.",
              "watercolor": "Use a soft watercolor landscape with visible paper and pigment textures.",
              "anime": "Use a detailed hand-painted animation background with clean environmental shapes.",
              "painterly": "Use a painterly illustration with natural brush texture."}[project["style"]["preset"]]
    placements = "\n".join(
        f"- {layer['id']}: source `{layer['src']}`; final card bounds {layer['box']} pixels "
        f"(left, top, right, bottom); clockwise rotation {layer['rotation']} degrees; "
        f"source image {layer['source_size']}."
        for layer in layers)
    notes = project["style"].get("prompt", "").strip()
    shape = canvas.get("shape", "rectangle")
    return f"""# Background generation request

Target: {canvas['width']} × {canvas['height']} pixels, {canvas.get('dpi', 300)} DPI metadata.
Mode: {project['mode']}. Final canvas shape: {shape}.
Produce a single continuous, fully opaque rectangular landscape/background at the target dimensions.
For a hexagon, the finishing script applies the polygon outline and transparency afterwards.
Do not render a hexagonal frame, outline, blank corner, or transparent corner in the generated source.

## Reference materials
`layout-reference.png` is a layout guide containing the user's original card images.
Those original images are visual references for scene continuity; they are not generated output.
Original cards are preserved by the finishing script in a separate composite preview only.
Do not preserve their characters or printed content inside the generated image.
`mask.png` is a geometry guide: white denotes background inside the final shape and black denotes
card locations plus areas outside the final shape. Cards cover their positions in a separate preview;
the finishing script removes the outside areas. Paint continuous scenery through every area anyway.
Inspect the source cards as well as the layout reference before generating.

{placements}

## Generation prompt
Create only the extended environmental background matching these placed card references.
Continue the visible horizon, terrain, foliage, water, architecture, perspective and light
around their given positions. {preset}
Fill the entire canvas with the environment, including under the future card positions.
Where a reference character or printed element appears, continue the surrounding scenery
instead of retaining that element. Do not leave a flat block, empty hole or placeholder there.
Do not include the original cards, card-shaped panels, borders, text, numbers, logos,
watermarks, Pokémon, other characters, or duplicates of characters from the references.
Keep important environmental transitions aligned with the placed reference edges.
User scene preferences: {notes or 'Follow the environments visible in the references.'}

## Agent handoff
Use the current session's native image-generation tool, not a substitute Python renderer.
The default background-only workflow first edits each complete, unmasked original into an
empty environment at its original composition. Record those genuine generated outputs in an
environments.json mapping, then run `prepare --environments ...` on this same project.
Use the returned environment-layout.png and outpaint-request.md for the full-canvas generation.
Reuse an existing reviewed environment when its original-source hash still matches. This
separates content production into real stages; it is not concealment or a moderation guarantee.
Continuity tests use this same scenery-only output contract. Do not change the task to
preserving reference characters or text in an intermediate outpaint; original-card
preservation is verified after deterministic compositing, separately from background quality.
Keep the references honestly labeled; background-only composition is for output separation
and preserving originals, not a method to bypass a safety decision. If the tool blocks the
request, stop and report it rather than changing accounts, tools, or disguising the request.
Inspect the generated image: it must contain only scenery, with no original card imagery,
text, card-shaped frames or reproduced characters. This visual check cannot be proven by
the deterministic finishing script. Save the approved generated image, then run `finish`.
"""


def _load_environments(project_path, project, manifest_path):
    manifest_path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProjectError(f"Cannot read environment mapping: {exc}") from exc
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ProjectError("Environment mapping version must be 1")
    entries = manifest.get("environments")
    if not isinstance(entries, list):
        raise ProjectError("Environment mapping needs an environments list")
    cards = {card["id"]: card for card in project["cards"]}
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProjectError("Environment entries must be objects")
        card_id = entry.get("card_id")
        if not isinstance(card_id, str) or card_id not in cards:
            raise ProjectError(f"Unknown environment card_id: {card_id!r}")
        if card_id in result:
            raise ProjectError(f"Duplicate environment card_id: {card_id}")
        source = _source_path(project_path, cards[card_id]["src"])
        if entry.get("source_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ProjectError(f"Environment source_sha256 does not match the current original card: {card_id}")
        src = entry.get("src")
        if not isinstance(src, str) or not src:
            raise ProjectError(f"Environment for {card_id} needs a src path")
        path = (manifest_path.parent / src).resolve()
        if not path.is_file():
            raise ProjectError(f"Environment image is missing for {card_id}: {path}")
        scenery = _open_image(path)
        if scenery.getextrema()[3] != (255, 255):
            raise ProjectError(f"Environment for {card_id} must be fully opaque")
        info = {"path": str(path), "image": scenery, "source_size": list(scenery.size),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for key in ("prompt", "generation_source"):
            if key in entry:
                if not isinstance(entry[key], str):
                    raise ProjectError(f"Environment {key} must be text for {card_id}")
                info[key] = entry[key]
        result[card_id] = info
    missing = cards.keys() - result.keys()
    if missing:
        raise ProjectError(f"Missing environments for card IDs: {', '.join(sorted(missing))}")
    return manifest_path, result


def _prepare_directory(project_path, output, default, overwrite=False):
    target = (project_path.parent / (output if output is not None else default)).resolve()
    if target == project_path.parent or target.is_relative_to(project_path.parent / "inputs"):
        raise ProjectError("Prepare output directory must not overwrite project inputs")
    if target.exists() and (not target.is_dir() or (not overwrite and any(target.iterdir()))):
        raise ProjectError("Prepare output directory is not empty; choose a new directory")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _prepare_environment_project(project_path, project, environments, output):
    mapping_path, sources = _load_environments(project_path, project, environments)
    layers = _card_layers(project_path, project, sources)
    canvas = project["canvas"]
    size = (canvas["width"], canvas["height"])
    _ensure_card_pixels_inside_shape(layers, shape_mask(*size, canvas.get("shape", "rectangle")))
    reference = Image.new("RGBA", size, (0, 0, 0, 0))
    for layer in layers:
        reference.alpha_composite(layer["image"], tuple(layer["box"][:2]))
    target = _prepare_directory(project_path, output, "prepare-environments")
    reference_path = target / "environment-layout.png"
    request_path = target / "outpaint-request.md"
    record_path = target / "environment-placement.json"
    _save_image(reference, reference_path, canvas.get("dpi", 300))
    placements = "\n".join(
        f"- {layer['id']}: original `{layer['src']}`, original SHA256 {layer['source_sha256']}; "
        f"generated environment `{layer['environment']['path']}`; actual rotated bounding box "
        f"{layer['box']} (left, top, right, bottom) pixels; clockwise rotation {layer['rotation']} degrees."
        for layer in layers)
    request_path.write_text(f"""# Extend a positioned pure-environment reference

Target: {size[0]} × {size[1]} pixels. Final shape: {canvas.get('shape', 'rectangle')}.
`environment-layout.png` contains already-generated scenery derived from the user's original
card illustrations. Originals remain unchanged and are composited separately after generation.
The transparent surroundings are a new, not-yet-painted canvas; no original character is masked.
Inspect every environment fragment before use: it must have no character, card, printing or frame.

{placements}

Keep the existing scenery at its exact position, scale and rotation. Fill the transparent
surroundings into ONE continuous environment. Do not zoom, recenter, enlarge or duplicate the
plants, rocks or other landmarks. Continue each edge-crossing contour, colour, light direction,
perspective and texture locally. Rotated bounding boxes include transparent corners: the actual
opaque footprints in the reference define the positioned fragments. Respect their overlap order.
The result must be a fully opaque rectangular background, including beneath the future cards;
do not include characters, cards, text, logos, borders, blank panels or holes. Hexagon clipping
and original-card compositing happen only in the finishing script. Follow the fragments' style.
User scene preferences: {project['style'].get('prompt', '').strip() or 'Follow the supplied environments.'}

Use the native image tool; this script does not generate imagery or accept an image-edit mask.
Keep the reference provenance explicit. This is background production, not a safety workaround;
it does not promise moderation approval. Stop and report an actual safety refusal.
Save the actual output and inspect it before `finish`. Preparation and successful rendering do
not establish background purity or seam continuity. Review the background and the final original-
card composite separately; source preservation alone is not a continuity pass.
""", encoding="utf-8")
    record = {"version": 1, "status": "prepared", "reference_kind": "generated_environment_layout",
              "project_path": str(project_path), "canvas": canvas,
              "environment_mapping": str(mapping_path),
              "environment_mapping_sha256": hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
              "cards": [_layer_info(layer) for layer in layers],
              "visual_review_required": ["pure_environment", "original_card_edge_continuity"],
              "note": "Geometry only verified; no generation, semantic approval, seam acceptance or moderation guarantee."}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**record, "reference": str(reference_path), "request": str(request_path), "manifest": str(record_path)}


def prepare_project(project_path, environments=None, output=None):
    project_path = Path(project_path).resolve()
    project = validate_project(project_path)
    if environments is not None:
        return _prepare_environment_project(project_path, project, environments, output)
    layers = _card_layers(project_path, project)
    canvas = project["canvas"]
    size = (canvas["width"], canvas["height"])
    dpi = canvas.get("dpi", 300)
    target = _prepare_directory(project_path, output, "prepare", overwrite=output is None)
    reference = Image.new("RGBA", size, "#eee9de")
    silhouette = shape_mask(*size, canvas.get("shape", "rectangle"))
    _ensure_card_pixels_inside_shape(layers, silhouette)
    if canvas.get("shape", "rectangle") != "rectangle":
        reference = Image.composite(reference, Image.new("RGBA", size, "#b5b5b5"), silhouette)
    mask = silhouette.copy()
    for layer in layers:
        l, t, r, b = layer["box"]
        reference.alpha_composite(layer["image"], (l, t))
        mask.paste(0, (l, t), layer["footprint"])
    _save_image(reference.convert("RGB"), target / "layout-reference.png", dpi)
    _save_image(mask, target / "mask.png", dpi)
    (target / "generation-request.md").write_text(_prompt_text(project, layers), encoding="utf-8")
    result = {"project_path": str(project_path), "reference": str(target / "layout-reference.png"),
              "mask": str(target / "mask.png"), "request": str(target / "generation-request.md"),
              "cards": [_layer_info(layer) for layer in layers]}
    return result


def _fit_background(image, size, fit):
    if image.getextrema()[3] != (255, 255):
        raise ProjectError("Generated background must be fully opaque; regenerate a full scene")
    image = image.convert("RGB")
    if image.size == size:
        return image, "exact"
    ratio_error = abs(image.width / image.height / (size[0] / size[1]) - 1)
    if fit is None and ratio_error > 0.002:
        raise ProjectError("Background aspect ratio differs from the canvas; request matching output "
                           "or explicitly choose --fit cover, contain, or stretch")
    if fit == "stretch":
        return image.resize(size, Image.Resampling.LANCZOS), "stretch (explicit)"
    if fit == "contain":
        fitted = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
        result = Image.new("RGB", size, "#eee9de")
        result.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
        return result, "contain with opaque #eee9de padding (explicit)"
    return ImageOps.fit(image, size, Image.Resampling.LANCZOS), "cover (explicit)" if fit else "uniform resize (rounding crop <=0.2%)"


def finish_project(project_path, background, output=None, fit=None):
    project_path = Path(project_path).resolve()
    project = validate_project(project_path)
    background_path = Path(background).resolve()
    if not background_path.is_file():
        raise ProjectError("Generated background file is missing")
    if fit not in {None, "contain", "cover", "stretch"}:
        raise ProjectError("Unsupported background fit policy")
    layers = _card_layers(project_path, project)
    canvas = project["canvas"]
    size = (canvas["width"], canvas["height"])
    dpi = canvas.get("dpi", 300)
    raw_background = _open_image(background_path)
    clean, fit_policy = _fit_background(raw_background, size, fit)
    shape = canvas.get("shape", "rectangle")
    silhouette = shape_mask(*size, shape)
    _ensure_card_pixels_inside_shape(layers, silhouette)
    if shape != "rectangle":
        clean = clean.convert("RGBA")
        clean.putalpha(silhouette)
    output = Path(output).resolve() if output else project_path.parent / "outputs"
    if output == project_path.parent or output.is_relative_to(project_path.parent / "inputs"):
        raise ProjectError("Output directory must not overwrite project inputs")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ProjectError("Output directory is not empty; choose a new output directory to preserve earlier results")
    staging = Path(tempfile.mkdtemp(prefix=".render-", dir=output.parent))
    try:
        _save_image(clean, staging / "background.png", dpi)
        composite = clean.convert("RGBA")
        cutout = clean.convert("RGBA")
        alpha = silhouette.copy()
        for layer in layers:
            l, t, r, b = layer["box"]
            composite.alpha_composite(layer["image"], (l, t))
            alpha.paste(0, (l, t), layer["footprint"])
        cutout.putalpha(alpha)
        _save_image(composite.convert("RGB") if shape == "rectangle" else composite,
                    staging / "composite-preview.png", dpi)
        _save_image(cutout, staging / "background-cutout.png", dpi)
        tiles = []
        print_dir = "print-eight"
        if project["mode"] == "grid":
            (staging / "tiles").mkdir()
            reserved = {card_grid_slot(project, card) for card in project["cards"]}
            print_dir = "print-eight" if len(reserved) == 1 else "print-background"
            (staging / print_dir).mkdir()
            for index, bounds in enumerate(grid_bounds(*size)):
                filename = f"background-{index + 1:02d}.png"
                tile = clean.crop(bounds)
                _save_image(tile, staging / "tiles" / filename, dpi)
                if index not in reserved:
                    shutil.copy2(staging / "tiles" / filename, staging / print_dir / filename)
                tiles.append({"index": index + 1, "row": index // 3 + 1, "column": index % 3 + 1,
                              "bounds": list(bounds), "size": list(tile.size),
                              "reserved_for_original_card": index in reserved,
                              "file": f"tiles/{filename}", "print": index not in reserved})
        outputs = ["background.png", "background-cutout.png", "composite-preview.png"]
        outputs += [tile["file"] for tile in tiles]
        outputs += [f"{print_dir}/{Path(tile['file']).name}" for tile in tiles if tile["print"]]
        manifest = {"version": 1, "mode": project["mode"], "status": "rendered",
                    "canvas": {**canvas, "shape": shape}, "shape": shape,
                    "shape_vertices": shape_vertices(*size, shape), "project_path": str(project_path),
                    "background_source": str(background_path),
                    "background_source_size": list(raw_background.size),
                    "background_source_sha256": hashlib.sha256(background_path.read_bytes()).hexdigest(),
                    "background_fit": fit_policy,
                    "background_visual_review": "Required by the skill; the renderer cannot detect generated characters or card imagery.",
                    "cards": [_layer_info(layer) for layer in layers],
                    "tiles": tiles, "outputs": outputs,
                    "print_note": "Production backgrounds and tiles never contain pasted original cards. composite-preview.png is for preview only."}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with zipfile.ZipFile(staging / "outputs.zip", "w", zipfile.ZIP_DEFLATED) as package:
            for filename in outputs + ["manifest.json"]:
                package.write(staging / filename, filename)
        for child in staging.iterdir():
            shutil.move(str(child), output / child.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"output_dir": str(output), "background": str(output / "background.png"),
            "preview": str(output / "composite-preview.png"), "cutout": str(output / "background-cutout.png"),
            "manifest": str(output / "manifest.json"), "zip": str(output / "outputs.zip"), "tile_count": len(tiles)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Validate the project and create reference artifacts")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--environments", help="JSON mapping of generated pure environments to original-card IDs and hashes")
    prepare.add_argument("--output", help="New preparation directory; relative paths are resolved against the project directory")
    finish = subparsers.add_parser("finish", help="Assemble approved generated background and original-card previews")
    finish.add_argument("--project", required=True)
    finish.add_argument("--background", required=True)
    finish.add_argument("--output")
    finish.add_argument("--fit", choices=["contain", "cover", "stretch"])
    args = parser.parse_args(argv)
    try:
        result = (prepare_project(args.project, args.environments, args.output) if args.command == "prepare"
                  else finish_project(args.project, args.background, args.output, args.fit))
    except ProjectError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
