"""Pure-function tests: no SOLIDWORKS required, safe for CI."""
import io
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from solidworks_mcp.server import (  # noqa: E402
    MIN_HOLE_MM,
    MIN_WALL_MM,
    _atomic_export,
    _hole_clearance,
    _is_owned,
    _owned_titles,
    _require_finite,
    _require_finite_positive,
    _resolve_output_path,
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


class TestOutputPath(unittest.TestCase):
    def setUp(self):
        self._old_out = os.environ.get("SOLIDWORKS_OUT_DIR")
        self._old_ext = os.environ.get("SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT")
        self._tmp = tempfile.mkdtemp()
        self.out = os.path.join(self._tmp, "out")
        os.environ["SOLIDWORKS_OUT_DIR"] = self.out
        os.environ.pop("SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT", None)

    def tearDown(self):
        if self._old_out is None:
            os.environ.pop("SOLIDWORKS_OUT_DIR", None)
        else:
            os.environ["SOLIDWORKS_OUT_DIR"] = self._old_out
        if self._old_ext is None:
            os.environ.pop("SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT", None)
        else:
            os.environ["SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT"] = self._old_ext
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_default_path(self):
        p = _resolve_output_path("", "STL", "agent_part")
        self.assertEqual(p, os.path.join(self.out, "agent_part.STL"))

    def test_inside_path_allowed(self):
        target = os.path.join(self.out, "sub", "x.STL")
        self.assertEqual(_resolve_output_path(target, "STL", "n"), target)

    def test_outside_path_rejected(self):
        outside = os.path.join(self._tmp, "elsewhere.STL")
        with self.assertRaises(ValueError):
            _resolve_output_path(outside, "STL", "n")

    def test_outside_allowed_with_override(self):
        outside = os.path.join(self._tmp, "elsewhere.STL")
        os.environ["SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT"] = "1"
        self.assertEqual(_resolve_output_path(outside, "STL", "n"), outside)


class TestAtomicExport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _writer(self, payload=b"hello"):
        def w(tmp):
            with open(tmp, "wb") as f:
                f.write(payload)
        return w

    def _read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def test_creates_target(self):
        target = os.path.join(self._tmp, "a.STL")
        _atomic_export(target, self._writer(), overwrite=False)
        self.assertTrue(os.path.exists(target))

    def test_refuses_overwrite_and_preserves(self):
        target = os.path.join(self._tmp, "a.STL")
        _atomic_export(target, self._writer(b"original"), overwrite=False)
        before = self._read(target)
        with self.assertRaises(FileExistsError):
            _atomic_export(target, self._writer(b"new"), overwrite=False)
        self.assertEqual(self._read(target), before)

    def test_overwrite_true_replaces(self):
        target = os.path.join(self._tmp, "a.STL")
        _atomic_export(target, self._writer(b"old"), overwrite=False)
        _atomic_export(target, self._writer(b"new"), overwrite=True)
        self.assertEqual(self._read(target), b"new")

    def test_writer_failure_preserves_original(self):
        target = os.path.join(self._tmp, "a.STL")
        _atomic_export(target, self._writer(b"original"), overwrite=False)

        def bad(tmp):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            _atomic_export(target, bad, overwrite=True)
        self.assertEqual(self._read(target), b"original")
        leftovers = [n for n in os.listdir(self._tmp) if n != "a.STL"]
        self.assertEqual(leftovers, [])


class TestValidation(unittest.TestCase):
    def test_finite_ok(self):
        self.assertEqual(_require_finite(1.5, "x"), 1.5)
        self.assertEqual(_require_finite("2.5", "x"), 2.5)

    def test_finite_rejects_nan_inf(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                _require_finite(bad, "x")

    def test_finite_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            _require_finite("abc", "x")

    def test_positive_ok(self):
        self.assertEqual(_require_finite_positive(3, "x"), 3.0)

    def test_positive_rejects_zero_negative(self):
        for bad in (0, -1, 0.0):
            with self.assertRaises(ValueError):
                _require_finite_positive(bad, "x")

    def test_positive_rejects_over_max(self):
        with self.assertRaises(ValueError):
            _require_finite_positive(5000, "x", max_value=1000)


class TestOwnership(unittest.TestCase):
    def _doc(self, title):
        return types.SimpleNamespace(GetTitle=title)

    def test_state_title_match(self):
        self.assertTrue(_is_owned(self._doc("Part1"), {"owned_title": "Part1"}))

    def test_state_title_mismatch(self):
        self.assertFalse(_is_owned(self._doc("Part2"), {"owned_title": "Part1"}))

    def test_no_title(self):
        self.assertFalse(_is_owned(self._doc(""), {"owned_title": "Part1"}))
        self.assertFalse(_is_owned(self._doc(None), {"owned_title": "Part1"}))

    def test_in_process_owned(self):
        _owned_titles.add("Part7")
        try:
            self.assertTrue(_is_owned(self._doc("Part7"), {}))
        finally:
            _owned_titles.discard("Part7")


if __name__ == "__main__":
    unittest.main(verbosity=2)
