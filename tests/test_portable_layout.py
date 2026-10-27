import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from create_project import create_project
from import_layout import import_layout


class PortableLayoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "original.png"
        Image.new("RGB", (733, 1024), (100, 140, 180)).save(self.image)

    def test_default_multi_is_650_staggered_and_separate(self):
        result = create_project(self.root, [self.image] * 2, mode="multi")
        project = json.loads(Path(result["project_path"]).read_text())
        self.assertEqual((project["canvas"]["width"], project["canvas"]["height"]), (1350, 2400))
        a, b = project["cards"]
        for card in (a, b):
            self.assertAlmostEqual(max(card["width"], card["height"]), 650)
        self.assertNotEqual(a["x"], b["x"])
        self.assertNotEqual(a["y"], b["y"])
        self.assertLess(a["x"] + a["width"], b["x"])

    def test_grid_example_matches_ascending_diagonal(self):
        result = create_project(self.root, [self.image] * 3, mode="grid")
        project = json.loads(Path(result["project_path"]).read_text())
        self.assertEqual([c["slot"] for c in project["cards"]], [6, 4, 2])
        self.assertEqual([(c["x"], c["y"]) for c in project["cards"]], [(0, 2048), (733, 1024), (1466, 0)])

    def test_import_preserves_original_pixels_geometry_and_rotation(self):
        pose = {"x": 400.125, "y": 700.5, "width": 465.2832, "height": 650, "rotation": 13.75}
        data = {"format": "card-scene-studio-layout", "version": 1, "mode": "single",
                "canvas": {"width": 1350, "height": 2400, "shape": "rectangle", "dpi": 300},
                "cards": [{"name": "original", "dataUrl": "data:image/png;base64," + base64.b64encode(self.image.read_bytes()).decode(), **pose}]}
        path = self.root / "portable.json"
        path.write_text(json.dumps(data))
        result = import_layout(path, self.root / "imported")
        project_path = Path(result["project_path"])
        card = json.loads(project_path.read_text())["cards"][0]
        self.assertEqual({key: card[key] for key in pose}, pose)
        with Image.open(project_path.parent / card["src"]) as restored, Image.open(self.image) as original:
            self.assertEqual(restored.convert("RGB").tobytes(), original.tobytes())
        self.assertFalse(result["handoff"]["accepted"])
        data["cards"][0]["x"] = -500
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            import_layout(path, self.root / "invalid")


if __name__ == "__main__":
    unittest.main()
