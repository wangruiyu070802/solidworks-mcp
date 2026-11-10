"""Pure-function tests: no SOLIDWORKS required, safe for CI."""
import io
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from solidworks_mcp.server import (  # noqa: E402
    MIN_HOLE_MM,
    MIN_WALL_MM,
    _hole_clearance,
    _stl_stats,
    print_warnings,
)


def make_stl(path, size_mm=20.0, unit_scale=1.0):
    """Write a binary STL box of the given size. unit_scale=0.001 simulates metres."""
    h = size_mm * unit_scale / 2.0
    v = [(-h, -h, -h), (h, -h, -h), (h, h, -h), (-h, h, -h),
         (-h, -h, h), (h, -h, h), (h, h, h), (-h, h, h)]
    faces = [
        ((0, 3, 2), (0, 2, 1), (0, 0, -1)), ((4, 5, 6), (4, 6, 7), (0, 0, 1)),
        ((0, 1, 5), (0, 5, 4), (0, -1, 0)), ((1, 2, 6), (1, 6, 5), (1, 0, 0)),
        ((2, 3, 7), (2, 7, 6), (0, 1, 0)), ((3, 0, 4), (3, 4, 7), (-1, 0, 0)),
    ]
    with open(path, "wb") as f:
        f.write(b"\0" * 80)
        f.write(struct.pack("<I", 12))
        for a, b, n in faces:
            for tri in (a, b):          # one full record per triangle
                f.write(struct.pack("<3f", *n))
                for i in tri:
                    f.write(struct.pack("<3f", *v[i]))
                f.write(struct.pack("<H", 0))


class TestStlStats(unittest.TestCase):
    def test_mm_stl(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "box.STL")
            make_stl(p, size_mm=20.0, unit_scale=1.0)     # coordinates already in mm
            s = _stl_stats(p)
            self.assertEqual(s["triangles"], 12)
            self.assertEqual(s["size_mm"], [20.0, 20.0, 20.0])
            self.assertEqual(s["unit_detected"], "mm")

    def test_metre_stl_auto_scaled(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "box_m.STL")
            make_stl(p, size_mm=20.0, unit_scale=0.001)   # coords in metres (0.02)
            s = _stl_stats(p)
            self.assertEqual(s["size_mm"], [20.0, 20.0, 20.0])
            self.assertEqual(s["unit_detected"], "m")

    def test_truncated_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "junk.STL")
            with open(p, "wb") as f:
                f.write(b"\0" * 10)
            self.assertEqual(_stl_stats(p)["triangles"], 0)


class TestPrintWarnings(unittest.TestCase):
    def test_clean_model_passes(self):
        self.assertEqual(print_warnings(thickness_mm=5.0, holes=[]), [])

    def test_thin_and_tiny_hole_warn(self):
        w = print_warnings(thickness_mm=0.5, holes=[{"dia": 0.8, "x": 0, "y": 0}])
        self.assertEqual(len(w), 2)
        self.assertIn("0.5", w[0])
        self.assertIn("0.8", w[1])

    def test_hole_too_close_to_edge(self):
        w = print_warnings(
            thickness_mm=5.0,
            holes=[{"dia": 8, "x": 0, "y": 0, "clearance_x": 0.3, "clearance_y": 10}],
        )
        self.assertEqual(len(w), 1)
        self.assertIn("距板边", w[0])


class TestHoleClearance(unittest.TestCase):
    def test_center_hole(self):
        c = _hole_clearance({"dia": 8, "x": 0, "y": 0}, 60, 40)
        self.assertEqual(c["clearance_x"], 26.0)
        self.assertEqual(c["clearance_y"], 16.0)

    def test_offset_hole(self):
        c = _hole_clearance({"dia": 6, "x": 20, "y": 5}, 60, 40)
        self.assertEqual(c["clearance_x"], 7.0)   # min(50,10) - 3
        self.assertEqual(c["clearance_y"], 12.0)  # min(25,15) - 3


class TestConstants(unittest.TestCase):
    def test_guardrail_constants(self):
        self.assertGreater(MIN_WALL_MM, 0)
        self.assertGreater(MIN_HOLE_MM, 0)
        self.assertLess(MIN_WALL_MM, MIN_HOLE_MM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
