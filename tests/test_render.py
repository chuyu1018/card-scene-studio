"""Behavioral checks for geometry and deliverable separation; no AI simulation."""
import hashlib
import importlib.util
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile

from PIL import Image, ImageChops

SPEC = importlib.util.spec_from_file_location("card_scene_render", Path(__file__).resolve().parents[1] / "scripts" / "render.py")
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


class RendererTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "inputs").mkdir()
        # Distinctive geometric test fixture, not an invented generative result.
        self.card = Image.new("RGB", (20, 30), (237, 31, 89))
        for y in range(30):
            for x in range(20):
                self.card.putpixel((x, y), (x * 10, y * 7, 89))
        self.card.save(self.root / "inputs" / "first.png")
        self.second = Image.new("RGB", (20, 30), (14, 44, 200))
        self.second.save(self.root / "inputs" / "second.png")
        self.project = {
            "version": 1, "mode": "single", "canvas": {"width": 101, "height": 103, "dpi": 300},
            "style": {"preset": "follow", "prompt": "Forest stream", "backgroundOnly": True},
            "grid": {"rows": 3, "cols": 3, "slot": 4},
            "cards": [{"id": "one", "name": "first", "src": "inputs/first.png", "x": 10, "y": 12, "width": 20, "height": 30}]
        }
        self.path = self.root / "project.json"
        self.background = Image.new("RGB", (101, 103))
        for y in range(103):
            for x in range(101):
                self.background.putpixel((x, y), (x, y, (x + y) % 256))
        self.background_path = self.root / "supplied-background.png"
        self.background.save(self.background_path)

    def tearDown(self):
        self.temp.cleanup()

    def write_project(self):
        self.path.write_text(json.dumps(self.project), encoding="utf-8")
        return self.path

    def assert_pixels(self, a, b):
        self.assertEqual(a.size, b.size)
        self.assertIsNone(ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox())

    def write_environments(self):
        # Synthetic fixtures only test geometry, never AI quality or background purity.
        folder = self.root / "generated"
        folder.mkdir(exist_ok=True)
        entries = []
        for card in self.project["cards"]:
            source = self.root / card["src"]
            target = folder / f"{card['id']}.png"
            with Image.open(source) as image:
                image.save(target)
            entries.append({"card_id": card["id"], "src": target.name,
                            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                            "prompt": "Synthetic geometry fixture", "generation_source": "unit test"})
        path = folder / "environments.json"
        path.write_text(json.dumps({"version": 1, "environments": entries}), encoding="utf-8")
        return path

    def test_environment_cli_records_provenance_without_modifying_originals_or_default_prepare(self):
        self.write_project()
        source = self.root / "inputs" / "first.png"
        old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        mapping = self.write_environments()
        default = render.prepare_project(self.path)
        old_reference = Path(default["reference"]).read_bytes()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = render.main(["prepare", "--project", str(self.path), "--environments", str(mapping),
                                     "--output", "prepare-scenes-01"])
        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(Path(result["reference"]).parent, (self.root / "prepare-scenes-01").resolve())
        self.assertEqual(result["cards"][0]["box"], default["cards"][0]["box"])
        with Image.open(result["reference"]) as image:
            self.assertEqual(image.size, (101, 103))
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getchannel("A").getbbox(), (10, 12, 30, 42))
            self.assert_pixels(image.crop((10, 12, 30, 42)), self.card)
        record = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(record["status"], "prepared")
        self.assertIn("original_card_edge_continuity", record["visual_review_required"])
        environment = record["cards"][0]["environment"]
        self.assertEqual(environment["sha256"], hashlib.sha256((mapping.parent / "one.png").read_bytes()).hexdigest())
        self.assertEqual(environment["generation_source"], "unit test")
        self.assertEqual(record["cards"][0]["source_sha256"], old_hash)
        self.assertEqual(Path(default["reference"]).read_bytes(), old_reference)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), old_hash)
        self.assertFalse((self.root / "status.json").exists())
        prompt = Path(result["request"]).read_text()
        self.assertIn("no original character is masked", prompt)
        self.assertIn("does not promise moderation approval", prompt)

    def test_environment_uses_original_fractional_size_centre_and_rotation(self):
        for rotation in (0, 35, 90):
            with self.subTest(rotation=rotation):
                self.project["cards"][0].update(x=30.25, y=24.5, width=34.4, height=48.2, rotation=rotation)
                self.write_project()
                mapping = self.write_environments()
                # A different resolution must still use the original card's geometry.
                Image.new("RGB", (40, 60), "teal").save(mapping.parent / "one.png")
                original = render._card_layers(self.path, self.project)[0]
                result = render.prepare_project(self.path, mapping, f"prepare-angle-{rotation}")
                self.assertEqual(result["cards"][0]["box"], original["box"])
                self.assertEqual(result["cards"][0]["rotation"], rotation)
                with Image.open(result["reference"]) as image:
                    expected = Image.new("L", image.size, 0)
                    expected.paste(original["footprint"], tuple(original["box"][:2]))
                    self.assertEqual(image.getchannel("A").tobytes(), expected.tobytes())

    def test_environment_overlap_follows_project_order_not_mapping_order(self):
        self.project["mode"] = "multi"
        self.project["cards"].append({"id": "two", "src": "inputs/second.png", "x": 20, "y": 20, "width": 20, "height": 30})
        self.write_project()
        mapping = self.write_environments()
        data = json.loads(mapping.read_text())
        data["environments"].reverse()
        data["environments"][0]["src"] = str(mapping.parent / "two.png")
        mapping.write_text(json.dumps(data))
        result = render.prepare_project(self.path, mapping)
        with Image.open(result["reference"]) as image:
            self.assertEqual(image.getpixel((25, 25)), (14, 44, 200, 255))
            self.assertEqual(image.getpixel((11, 13))[:3], self.card.getpixel((1, 1)))
        self.assertEqual([layer["id"] for layer in result["cards"]], ["one", "two"])

    def test_environment_registration_does_not_crop_again_at_rounded_card_size(self):
        self.project["cards"][0].update(x=30.25, y=24.5, width=34.4, height=49.2, rotation=35)
        self.write_project()
        mapping = self.write_environments()
        original = render._card_layers(self.path, self.project)[0]
        result = render.prepare_project(self.path, mapping)
        with Image.open(result["reference"]) as image:
            expected = Image.new("RGBA", image.size, (0, 0, 0, 0))
            expected.alpha_composite(original["image"], tuple(original["box"][:2]))
            self.assertEqual(image.tobytes(), expected.tobytes())

    def test_environment_accepts_generation_resolution_rounding_without_position_drift(self):
        Image.new("RGB", (733, 1024), "teal").save(self.root / "inputs" / "first.png")
        self.write_project()
        mapping = self.write_environments()
        Image.new("RGB", (1061, 1483), "teal").save(mapping.parent / "one.png")
        original = render._card_layers(self.path, self.project)[0]
        result = render.prepare_project(self.path, mapping)
        self.assertEqual(result["cards"][0]["box"], original["box"])
        environment = result["cards"][0]["environment"]
        self.assertGreater(environment["source_aspect_error"], 0)
        self.assertLess(environment["source_aspect_error"], 0.002)
        self.assertEqual(environment["fitted_size"], list(original["image"].size))

    def test_environment_three_grid_slots_have_exact_original_footprints(self):
        self.project.update(mode="grid", grid={"rows": 3, "cols": 3, "layout": "card-sized", "cellWidth": 20, "cellHeight": 30})
        self.project["canvas"].update(width=60, height=90)
        self.project["cards"][0].update(x=0, y=0, slot=0)
        for index, slot in enumerate((4, 8), start=1):
            self.project["cards"].append({"id": f"extra-{index}", "src": "inputs/second.png", "slot": slot,
                                          "x": index * 20, "y": index * 30, "width": 20, "height": 30})
        self.write_project()
        mapping = self.write_environments()
        result = render.prepare_project(self.path, mapping)
        self.assertEqual([layer["box"] for layer in result["cards"]],
                         [[0, 0, 20, 30], [20, 30, 40, 60], [40, 60, 60, 90]])
        with Image.open(result["reference"]) as image:
            for slot, bounds in enumerate(render.grid_bounds(60, 90)):
                expected = 255 if slot in (0, 4, 8) else 0
                self.assertEqual(image.getchannel("A").crop(bounds).getextrema(), (expected, expected))

    def test_environment_rejects_unknown_duplicate_missing_ids_and_stale_hash(self):
        self.write_project()
        mapping = self.write_environments()
        initial = json.loads(mapping.read_text())
        cases = [
            (lambda d: d["environments"][0].update(card_id="unknown"), "Unknown environment card_id"),
            (lambda d: d["environments"].append(dict(d["environments"][0])), "Duplicate environment card_id"),
            (lambda d: d["environments"].clear(), "Missing environments"),
            (lambda d: d["environments"][0].update(source_sha256="old"), "source_sha256"),
            (lambda d: d["environments"][0].update(src="missing.png"), "image is missing"),
        ]
        for mutate, error in cases:
            data = json.loads(json.dumps(initial))
            mutate(data)
            mapping.write_text(json.dumps(data))
            with self.subTest(error=error), self.assertRaisesRegex(render.ProjectError, error):
                render.prepare_project(self.path, mapping)
        self.assertFalse((self.root / "prepare-environments").exists())
        mapping.write_text(json.dumps(initial))
        Image.new("RGB", self.card.size, "black").save(self.root / "inputs" / "first.png")
        with self.assertRaisesRegex(render.ProjectError, "source_sha256"):
            render.prepare_project(self.path, mapping)

    def test_environment_rejects_transparency_and_aspect_changes_before_creating_output(self):
        self.write_project()
        mapping = self.write_environments()
        for image, error in [(Image.new("RGBA", (20, 30), (0, 0, 0, 254)), "fully opaque"),
                             (Image.new("RGB", (30, 20), "blue"), "aspect ratio")]:
            image.save(mapping.parent / "one.png")
            with self.subTest(error=error), self.assertRaisesRegex(render.ProjectError, error):
                render.prepare_project(self.path, mapping)
        self.assertFalse((self.root / "prepare-environments").exists())

    def test_environment_preparation_preserves_existing_files(self):
        self.write_project()
        mapping = self.write_environments()
        result = render.prepare_project(self.path, mapping)
        before = Path(result["reference"]).read_bytes()
        with self.assertRaisesRegex(render.ProjectError, "not empty"):
            render.prepare_project(self.path, mapping)
        self.assertEqual(Path(result["reference"]).read_bytes(), before)
        with self.assertRaisesRegex(render.ProjectError, "inputs"):
            render.prepare_project(self.path, mapping, "inputs/new-prepare")

    def test_single_preserves_original_and_background_separately(self):
        source = self.root / "inputs" / "first.png"
        old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        prep = render.prepare_project(self.write_project())
        mask = Image.open(prep["mask"])
        self.assertEqual(mask.getpixel((10, 12)), 0)
        self.assertEqual(mask.getpixel((29, 41)), 0)
        self.assertEqual(mask.getpixel((30, 42)), 255)
        result = render.finish_project(self.path, self.background_path)
        self.assert_pixels(Image.open(result["background"]), self.background)
        preview = Image.open(result["preview"])
        self.assert_pixels(preview.crop((10, 12, 30, 42)), self.card)
        self.assert_pixels(preview.crop((40, 50, 100, 100)), self.background.crop((40, 50, 100, 100)))
        cutout = Image.open(result["cutout"])
        self.assertEqual(cutout.getpixel((11, 13))[3], 0)
        self.assertEqual(cutout.getpixel((0, 0))[3], 255)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), old_hash)
        self.assertFalse((self.root / "status.json").exists())
        with zipfile.ZipFile(result["zip"]) as archive:
            self.assertIn("background.png", archive.namelist())
            self.assertIn("composite-preview.png", archive.namelist())
            self.assertNotIn("inputs/first.png", archive.namelist())

    def test_multicard_overlap_follows_list_order_and_preserves_aspect(self):
        self.project["mode"] = "multi"
        self.project["cards"].append({"id": "two", "src": "inputs/second.png", "x": 20, "y": 20, "width": 40, "height": 40})
        self.write_project()
        result = render.finish_project(self.path, self.background_path)
        manifest = json.loads(Path(result["manifest"]).read_text())
        box = manifest["cards"][1]["box"]
        self.assertEqual(box[3] - box[1], 40)
        self.assertLess(box[2] - box[0], 40)
        self.assertAlmostEqual((box[2] - box[0]) / (box[3] - box[1]), 20 / 30, delta=0.03)
        self.assertEqual(Image.open(result["preview"]).getpixel((28, 25)), (14, 44, 200))
        self.assert_pixels(Image.open(result["background"]), self.background)

    def test_quarter_turn_preserves_pixels_and_clockwise_direction(self):
        source = self.root / "inputs" / "first.png"
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        self.project["cards"][0].update(x=30, y=30, rotation=90)
        prep = render.prepare_project(self.write_project())
        result = render.finish_project(self.path, self.background_path)
        layer = json.loads(Path(result["manifest"]).read_text())["cards"][0]
        self.assertEqual(layer["rotation"], 90)
        self.assertEqual(layer["box"], [25, 35, 55, 55])
        self.assert_pixels(Image.open(result["preview"]).crop(layer["box"]),
                           self.card.transpose(Image.Transpose.ROTATE_270))
        self.assertEqual(Image.open(prep["mask"]).getpixel((26, 36)), 0)
        self.assertEqual(Image.open(result["cutout"]).getpixel((26, 36))[3], 0)
        self.assert_pixels(Image.open(result["background"]), self.background)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)

    def test_free_rotation_masks_only_the_rotated_footprint(self):
        self.project["cards"][0].update(x=30, y=30, rotation=35)
        prep = render.prepare_project(self.write_project())
        result = render.finish_project(self.path, self.background_path)
        box = prep["cards"][0]["box"]
        mask = Image.open(prep["mask"])
        cutout = Image.open(result["cutout"])
        preview = Image.open(result["preview"])
        self.assertEqual(mask.getpixel((40, 45)), 0)
        self.assertEqual(cutout.getpixel((40, 45))[3], 0)
        self.assertEqual(mask.getpixel(tuple(box[:2])), 255)
        self.assertEqual(cutout.getpixel(tuple(box[:2]))[3], 255)
        self.assertEqual(preview.getpixel(tuple(box[:2])), self.background.getpixel(tuple(box[:2])))
        self.assert_pixels(Image.open(result["background"]), self.background)

    def test_rotated_bounds_and_invalid_angles_are_rejected(self):
        for angle in [True, "20", float("nan"), float("inf"), 181]:
            with self.subTest(angle=angle):
                self.project["cards"][0]["rotation"] = angle
                with self.assertRaises(render.ProjectError):
                    render.validate_project(self.write_project())
        self.project["cards"][0].update(x=0, y=0, rotation=45)
        with self.assertRaises(render.ProjectError):
            render.validate_project(self.write_project())
        # A tall unrotated box may extend beyond a wide canvas while its rotated corners fit.
        self.project["canvas"].update(width=100, height=40)
        self.project["cards"][0].update(x=40, y=-15, width=20, height=70, rotation=90)
        render.prepare_project(self.write_project())
        self.project["mode"] = "grid"
        with self.assertRaises(render.ProjectError):
            render.validate_project(self.write_project())

    def test_grid_all_tiles_reassemble_exactly_and_eight_exclude_slot(self):
        self.project["mode"] = "grid"
        self.project["cards"][0].update(x=40, y=36, width=20, height=30)
        result = render.finish_project(self.write_project(), self.background_path)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(len(manifest["tiles"]), 9)
        rebuilt = Image.new("RGB", self.background.size)
        counts = Image.new("L", self.background.size, 0)
        for tile in manifest["tiles"]:
            l, t, r, b = tile["bounds"]
            pixels = Image.open(Path(result["output_dir"]) / tile["file"])
            self.assertEqual(pixels.size, (r - l, b - t))
            rebuilt.paste(pixels, (l, t))
            for y in range(t, b):
                for x in range(l, r):
                    counts.putpixel((x, y), counts.getpixel((x, y)) + 1)
        self.assertEqual(counts.getextrema(), (1, 1))
        self.assert_pixels(rebuilt, self.background)
        eight = Path(result["output_dir"]) / "print-eight"
        self.assertEqual(len(list(eight.glob("*.png"))), 8)
        self.assertFalse((eight / "background-05.png").exists())
        self.assertTrue(manifest["tiles"][4]["reserved_for_original_card"])
        self.assertFalse(manifest["tiles"][4]["print"])

    def test_grid_all_nine_reserved_positions(self):
        self.project["mode"] = "grid"
        for slot, (l, t, r, b) in enumerate(render.grid_bounds(101, 103)):
            self.project["grid"]["slot"] = slot
            self.project["cards"][0].update(x=l, y=t, width=r-l, height=b-t)
            self.write_project()
            prep = render.prepare_project(self.path)
            box = prep["cards"][0]["box"]
            self.assertGreaterEqual(box[0], l)
            self.assertGreaterEqual(box[1], t)
            self.assertLessEqual(box[2], r)
            self.assertLessEqual(box[3], b)

    def test_three_grid_cards_preserve_sources_and_export_six_background_tiles(self):
        self.project["mode"] = "grid"
        self.project["cards"] = []
        for index, slot in enumerate([0, 4, 8]):
            l, t, _, _ = render.grid_bounds(101, 103)[slot]
            self.project["cards"].append({"id": f"card-{index}", "src": "inputs/first.png",
                                          "slot": slot, "x": l + 6, "y": t + 2,
                                          "width": 20, "height": 30})
        prep = render.prepare_project(self.write_project())
        result = render.finish_project(self.path, self.background_path)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(len(prep["cards"]), 3)
        self.assert_pixels(Image.open(result["background"]), self.background)
        preview, cutout = Image.open(result["preview"]), Image.open(result["cutout"])
        for layer in manifest["cards"]:
            self.assert_pixels(preview.crop(layer["box"]), self.card)
            self.assertEqual(cutout.getpixel(tuple(layer["box"][:2]))[3], 0)
        self.assertEqual({t["index"] for t in manifest["tiles"] if t["reserved_for_original_card"]}, {1, 5, 9})
        folder = Path(result["output_dir"]) / "print-background"
        self.assertEqual({p.name for p in folder.glob("*.png")},
                         {f"background-{i:02d}.png" for i in [2, 3, 4, 6, 7, 8]})
        rebuilt = Image.new("RGB", self.background.size)
        for tile in manifest["tiles"]:
            rebuilt.paste(Image.open(Path(result["output_dir"]) / tile["file"]), tuple(tile["bounds"][:2]))
        self.assert_pixels(rebuilt, self.background)
        with zipfile.ZipFile(result["zip"]) as archive:
            self.assertEqual(len([name for name in archive.namelist() if name.startswith("print-background/")]), 6)

    def test_card_sized_grid_fills_cells_and_exports_six_background_tiles(self):
        self.project["mode"] = "grid"
        self.project["canvas"].update(width=60, height=90)
        self.project["grid"].update(layout="card-sized", cellWidth=20, cellHeight=30)
        self.project["cards"] = [{"id": f"card-{slot}", "src": "inputs/first.png", "slot": slot,
                                   "x": (slot % 3) * 20, "y": (slot // 3) * 30,
                                   "width": 20, "height": 30} for slot in [0, 4, 8]]
        background = self.background.resize((60, 90))
        background.save(self.background_path)
        prep = render.prepare_project(self.write_project())
        result = render.finish_project(self.path, self.background_path)
        preview = Image.open(result["preview"])
        for layer in prep["cards"]:
            self.assert_pixels(preview.crop(layer["box"]), self.card)
        self.assert_pixels(Image.open(result["background"]), background)
        self.assertEqual(len(list((Path(result["output_dir"]) / "print-background").glob("*.png"))), 6)
        # Movement and resizing within a locked cell are rejected even if contained.
        self.project["cards"][1].update(x=21, y=31, width=18, height=27)
        with self.assertRaisesRegex(render.ProjectError, "fitted and centred"):
            render.validate_project(self.write_project())

    def test_card_sized_grid_requires_equal_cells(self):
        self.project["mode"] = "grid"
        self.project["grid"].update(layout="card-sized", cellWidth=33, cellHeight=34)
        with self.assertRaisesRegex(render.ProjectError, "nine equal"):
            render.validate_project(self.write_project())

    def test_multiple_grid_cards_require_distinct_explicit_slots(self):
        self.project["mode"] = "grid"
        self.project["cards"][0].update(slot=4, x=40, y=36, width=20, height=30)
        second = {**self.project["cards"][0], "id": "two"}
        self.project["cards"].append(second)
        with self.assertRaisesRegex(render.ProjectError, "different reserved slot"):
            render.validate_project(self.write_project())
        second.pop("slot")
        with self.assertRaisesRegex(render.ProjectError, "Card grid slot"):
            render.validate_project(self.write_project())

    def test_mismatched_background_requires_explicit_fit(self):
        Image.new("RGB", (200, 100), "blue").save(self.background_path)
        self.write_project()
        with self.assertRaisesRegex(render.ProjectError, "aspect ratio"):
            render.finish_project(self.path, self.background_path)
        for fit in ["contain", "cover", "stretch"]:
            result = render.finish_project(self.path, self.background_path, self.root / fit, fit)
            with Image.open(result["background"]) as background:
                self.assertEqual(background.size, (101, 103))
            manifest = json.loads(Path(result["manifest"]).read_text())
            self.assertIn("explicit", manifest["background_fit"])

    def test_transparency_rejected_for_final_scene(self):
        Image.new("RGBA", (101, 103), (0, 0, 0, 0)).save(self.background_path)
        self.write_project()
        with self.assertRaisesRegex(render.ProjectError, "fully opaque"):
            render.finish_project(self.path, self.background_path)

    def test_invalid_geometry_count_paths_and_grid_are_rejected(self):
        mutations = [
            lambda p: p["cards"][0].update(x=-1),
            lambda p: p["cards"][0].update(width=999),
            lambda p: p["cards"][0].update(x=float("nan")),
            lambda p: p["cards"][0].update(src="../outside.png"),
            lambda p: p["cards"][0].update(src="prepare/layout-reference.png"),
            lambda p: p["cards"].clear(),
            lambda p: p["cards"].append(dict(p["cards"][0])),
            lambda p: p.update(mode="invalid"),
            lambda p: p.update(mode={}),
            lambda p: p.update(version=True),
            lambda p: p["style"].update(backgroundOnly=False),
            lambda p: p["style"].update(preset=[]),
            lambda p: p["canvas"].update(width=True),
            lambda p: p["canvas"].update(width=101.0),
            lambda p: p["canvas"].update(width=8192, height=8192),
        ]
        original = json.dumps(self.project)
        for mutate in mutations:
            self.project = json.loads(original)
            mutate(self.project)
            with self.assertRaises(render.ProjectError):
                render.validate_project(self.write_project())
        self.project = json.loads(original)
        self.project["mode"] = "grid"
        with self.assertRaisesRegex(render.ProjectError, "reserved slot"):
            render.validate_project(self.write_project())
        self.project["grid"]["slot"] = 9
        with self.assertRaises(render.ProjectError):
            render.validate_project(self.write_project())

    def test_existing_outputs_and_source_directory_cannot_be_overwritten(self):
        self.write_project()
        render.finish_project(self.path, self.background_path)
        with self.assertRaisesRegex(render.ProjectError, "not empty"):
            render.finish_project(self.path, self.background_path)
        with self.assertRaisesRegex(render.ProjectError, "inputs"):
            render.finish_project(self.path, self.background_path, self.root / "inputs" / "new")

    def test_sources_cannot_escape_inputs_by_traversal_or_symlink(self):
        outside_inputs = self.root / "escape.png"
        self.card.save(outside_inputs)
        self.project["cards"][0]["src"] = "inputs/../escape.png"
        with self.assertRaisesRegex(render.ProjectError, "inputs/"):
            render.validate_project(self.write_project())
        link = self.root / "inputs" / "escape-link.png"
        link.symlink_to(outside_inputs)
        self.project["cards"][0]["src"] = "inputs/escape-link.png"
        with self.assertRaisesRegex(render.ProjectError, "inputs/"):
            render.validate_project(self.write_project())
        # A symlink to another actual file within inputs is harmless and allowed.
        link.unlink()
        link.symlink_to(self.root / "inputs" / "first.png")
        render.validate_project(self.write_project())

    def configure_hexagon(self, shape):
        width, height = (173, 200) if shape == "hex-pointy" else (200, 173)
        self.project["canvas"].update(width=width, height=height, shape=shape, preset=shape)
        self.project["cards"][0].update(x=(width - 20) // 2, y=(height - 30) // 2, width=20, height=30)
        self.background = Image.new("RGB", (width, height))
        for y in range(height):
            for x in range(width):
                self.background.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
        self.background.save(self.background_path)
        return width, height

    def test_both_regular_hexagon_orientations_have_transparent_corners_and_complete_card(self):
        for shape in ["hex-pointy", "hex-flat"]:
            width, height = self.configure_hexagon(shape)
            vertices = render.shape_vertices(width, height, shape)
            sides = [math.dist(vertices[i], vertices[(i + 1) % 6]) for i in range(6)]
            self.assertLess(max(sides) - min(sides), 1)
            prep = render.prepare_project(self.write_project())
            with Image.open(prep["mask"]) as mask:
                self.assertEqual(mask.getpixel((0, 0)), 0)
                self.assertEqual(mask.getpixel((width // 2, height // 2)), 0)
                self.assertEqual(mask.getpixel((width // 2, 5)), 255)
            result = render.finish_project(self.path, self.background_path, self.root / shape)
            with Image.open(result["background"]) as output, Image.open(result["preview"]) as preview, Image.open(result["cutout"]) as cutout:
                self.assertEqual(output.mode, "RGBA")
                self.assertEqual(preview.mode, "RGBA")
                for point in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]:
                    self.assertEqual(output.getpixel(point)[3], 0)
                    self.assertEqual(preview.getpixel(point)[3], 0)
                    self.assertEqual(cutout.getpixel(point)[3], 0)
                x, y = self.project["cards"][0]["x"], self.project["cards"][0]["y"]
                crop = preview.crop((x, y, x + 20, y + 30))
                self.assertEqual(crop.tobytes(), self.card.convert("RGBA").tobytes())
                self.assertEqual(cutout.getpixel((x, y))[3], 0)
                self.assert_pixels(output, self.background)
                opaque_area = output.getchannel("A").histogram()[255]
                self.assertAlmostEqual(opaque_area / (width * height), 0.75, delta=0.01)
            manifest = json.loads(Path(result["manifest"]).read_text())
            self.assertEqual(manifest["shape"], shape)
            self.assertEqual(manifest["canvas"]["shape"], shape)
            self.assertEqual(len(manifest["shape_vertices"]), 6)

    def test_hexagon_multicard_keeps_both_originals_and_shape(self):
        self.configure_hexagon("hex-pointy")
        self.project["mode"] = "multi"
        self.project["cards"][0].update(x=40, y=60)
        self.project["cards"].append({"id": "two", "src": "inputs/second.png", "x": 110, "y": 110, "width": 20, "height": 30})
        result = render.finish_project(self.write_project(), self.background_path)
        with Image.open(result["preview"]) as preview:
            self.assertEqual(preview.crop((40, 60, 60, 90)).tobytes(), self.card.convert("RGBA").tobytes())
            self.assertEqual(preview.crop((110, 110, 130, 140)).tobytes(), self.second.convert("RGBA").tobytes())
            self.assertEqual(preview.getpixel((0, 0))[3], 0)

    def test_hexagon_grid_rgba_tiles_reassemble_and_print_set_excludes_reserved_slot(self):
        for shape in ["hex-pointy", "hex-flat"]:
            size = self.configure_hexagon(shape)
            self.project["mode"] = "grid"
            result = render.finish_project(self.write_project(), self.background_path, self.root / shape)
            manifest = json.loads(Path(result["manifest"]).read_text())
            rebuilt = Image.new("RGBA", size)
            for tile in manifest["tiles"]:
                with Image.open(Path(result["output_dir"]) / tile["file"]) as pixels:
                    self.assertEqual(pixels.mode, "RGBA")
                    rebuilt.paste(pixels, tuple(tile["bounds"][:2]))
            with Image.open(result["background"]) as background:
                self.assertEqual(rebuilt.tobytes(), background.tobytes())
            self.assertEqual(rebuilt.getpixel((0, 0))[3], 0)
            self.assertEqual(len(list((Path(result["output_dir"]) / "print-eight").glob("*.png"))), 8)
            self.assertFalse((Path(result["output_dir"]) / "print-eight" / "background-05.png").exists())

    def test_hexagon_all_grid_slots_accept_smaller_uncropped_cards(self):
        positions = {
            "hex-pointy": [(30, 35, 20, 30), (76, 10, 20, 30), (123, 35, 20, 30),
                            (8, 85, 20, 30), (76, 85, 20, 30), (145, 85, 20, 30),
                            (30, 135, 20, 30), (76, 160, 20, 30), (123, 135, 20, 30)],
            "hex-flat": [(35, 30, 20, 20), (90, 8, 20, 30), (145, 30, 20, 20),
                          (8, 76, 20, 20), (90, 76, 20, 20), (172, 76, 20, 20),
                          (35, 123, 20, 20), (90, 135, 20, 30), (145, 123, 20, 20)]
        }
        for shape, slots in positions.items():
            width, height = self.configure_hexagon(shape)
            self.project["mode"] = "grid"
            for slot, (x, y, w, h) in enumerate(slots):
                self.project["grid"]["slot"] = slot
                self.project["cards"][0].update(x=x, y=y, width=w, height=h)
                prep = render.prepare_project(self.write_project())
                box = prep["cards"][0]["box"]
                silhouette = render.shape_mask(width, height, shape)
                self.assertEqual(silhouette.crop(box).getextrema(), (255, 255))

    def test_invalid_shapes_distorted_hexagons_and_card_corners_are_rejected(self):
        self.project["canvas"]["shape"] = "hexagon"
        with self.assertRaisesRegex(render.ProjectError, "Canvas shape"):
            render.validate_project(self.write_project())
        self.project["canvas"]["shape"] = []
        with self.assertRaises(render.ProjectError):
            render.validate_project(self.write_project())
        self.configure_hexagon("hex-flat")
        self.project["canvas"]["width"] = 180
        with self.assertRaisesRegex(render.ProjectError, "regular hexagon"):
            render.validate_project(self.write_project())
        self.configure_hexagon("hex-pointy")
        self.project["cards"][0].update(x=0, y=0)
        with self.assertRaisesRegex(render.ProjectError, "four card corners"):
            render.validate_project(self.write_project())
        self.configure_hexagon("hex-flat")
        self.project["canvas"]["preset"] = "unknown"
        with self.assertRaisesRegex(render.ProjectError, "canvas preset"):
            render.validate_project(self.write_project())

    def test_rectangle_presets_and_legacy_shape_default(self):
        sizes = {"1:1": (120, 120), "1:2": (120, 240), "2:1": (240, 120),
                 "16:9": (240, 135), "9:16": (135, 240)}
        for preset, (width, height) in sizes.items():
            self.project["canvas"].update(width=width, height=height, preset=preset)
            self.assertEqual(render.validate_project(self.write_project())["canvas"]["preset"], preset)
        self.project["canvas"] = {"width": 101, "height": 103, "dpi": 300}
        result = render.finish_project(self.write_project(), self.background_path)
        with Image.open(result["background"]) as background:
            self.assertEqual(background.mode, "RGB")
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["shape"], "rectangle")


if __name__ == "__main__":
    unittest.main()
