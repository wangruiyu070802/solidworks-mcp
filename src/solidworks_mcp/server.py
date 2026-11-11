"""MCP server: drive SOLIDWORKS via COM for FDM-ready parameterized parts.

Verified against SOLIDWORKS 2024 SP0.1 (revision 32.0.1) with pywin32 late binding.

Design rules (all established experimentally, see README "本机 COM 关键经验"):
  * The model is always REBUILT from parameters (template instantiation), never
    patched. One sketch (outer loop + hole circles), one extrusion. FeatureCut4
    is avoided entirely (it rejects every argument count tried).
  * State survives server restarts: parameters persist to <out>/model_state.json,
    and the model lives in SOLIDWORKS's own process (re-attached via ActiveDoc).
  * Nearly every ModelDoc2/Feature member surfaces as a PROPERTY in this build:
    GetFeatureCount -> int, FirstFeature -> CDispatch, GetTypeName2 -> str.
  * SelectByID2 raises a COM type mismatch under late binding -> never use it.
    The default plane is found by walking the feature tree for "RefPlane" and
    calling Feature.Select2(False, 0). (Chinese UI names it 前视基准面.)
  * FeatureExtrusion2 returns None unless the SKETCH IS SELECTED first.
  * ModelDoc2.SaveAs(path) is the only working save/export call.
  * STL export exports the SELECTION when anything is selected
    -> always ClearSelection2(True) first.
  * API lengths are metres; this server speaks millimetres.
  * SolidWorks writes STL in millimetres; units are auto-detected anyway.

Configuration (environment variables; all optional):
  SOLIDWORKS_TEMPLATE   .prtdot part template path (else probed)
  BAMBU_EXE             Bambu Studio executable path (else probed)
  SOLIDWORKS_OUT_DIR    output directory (default: <cwd>/out)
"""

from __future__ import annotations

import io
import json
import math
import os
import secrets
import struct
import sys
import threading
import traceback

if "--selftest" in sys.argv and hasattr(sys.stdout, "buffer"):
    # human-facing selftest only; NEVER rewrap stdout while serving MCP
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

NOZZLE_MM = 0.4
MIN_WALL_MM = 0.8          # 2 perimeters at 0.4 nozzle
MIN_HOLE_MM = 1.2          # below this FDM holes close up
CLEARANCE_MM = 0.15        # mating clearance per side

TEMPLATE_CANDIDATES = [
    os.environ.get("SOLIDWORKS_TEMPLATE") or "",
    r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\gb_part.prtdot",
    r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\Part.prtdot",
    r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\gb_part.prtdot",
    r"C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\Part.prtdot",
]

BAMBU_CANDIDATES = [
    os.environ.get("BAMBU_EXE") or "",
    r"D:\02_Apps\Dev\Bambu Studio\bambu-studio.exe",          # this machine
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Bambu Studio\bambu-studio.exe"),
    r"C:\Program Files\Bambu Studio\bambu-studio.exe",
]


def default_template() -> str:
    for p in TEMPLATE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return TEMPLATE_CANDIDATES[1] if TEMPLATE_CANDIDATES[1] else ""


def bambu_exe() -> str:
    for p in BAMBU_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return BAMBU_CANDIDATES[1] if BAMBU_CANDIDATES[1] else ""


def out_dir() -> str:
    d = os.environ.get("SOLIDWORKS_OUT_DIR") or os.path.join(os.getcwd(), "out")
    os.makedirs(d, exist_ok=True)
    return d


STATE_FILE = os.path.join(out_dir(), "model_state.json")

_com_lock = threading.Lock()


def _log(*parts):
    try:
        with open(os.path.join(out_dir(), "server_debug.log"), "a", encoding="utf-8") as f:
            f.write(" ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


def _is_within(path, base):
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:
        return False


def _resolve_output_path(path, ext, name):
    """Constrain an output path to the approved out_dir (or an explicit override).

    Defaults to <out_dir>/<name>.<ext>. A caller-supplied path must live inside
    the output directory unless SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT=1.
    """
    base = os.path.abspath(out_dir())
    if not path:
        return os.path.join(base, f"{name}.{ext}")
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if not _is_within(path, base) and os.environ.get("SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT") != "1":
        raise ValueError(
            f"输出路径必须在输出目录内: {base}"
            "（设 SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT=1 可允许任意路径）"
        )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


def _atomic_export(target, writer, overwrite):
    """Write via writer(tmp_path) then atomically replace target.

    - Refuses to overwrite an existing target unless overwrite=True.
    - On any failure the original target is left untouched.
    """
    if os.path.exists(target) and not overwrite:
        raise FileExistsError(f"目标已存在: {target}（传 overwrite=true 才覆盖）")
    ext = os.path.splitext(target)[1]
    stem = os.path.splitext(os.path.basename(target))[0]
    tmp = os.path.join(os.path.dirname(target), f".{stem}.{secrets.token_hex(6)}{ext}")
    try:
        writer(tmp)
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError("导出失败（产物为空）")
        os.replace(tmp, target)
        return target
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        raise


def _require_finite(value, label):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 必须是数值（收到 {value!r}）")
    if not math.isfinite(v):
        raise ValueError(f"{label} 必须为有限数值（收到 {v}）")
    return v


def _require_finite_positive(value, label, max_value=None):
    v = _require_finite(value, label)
    if v <= 0:
        raise ValueError(f"{label} 必须为正数（收到 {v}）")
    if max_value is not None and v > max_value:
        raise ValueError(f"{label} 过大（收到 {v}，上限 {max_value}）")
    return v


# ---------------------------------------------------------------- COM helpers

def _cv(obj, name):
    """Read a member that may surface as a property or a method wrapper."""
    v = getattr(obj, name)
    if type(v).__name__ in ("_MethodImplementation", "MethodImplementation"):
        return v()
    return v


_owned_titles: set = set()     # titles of documents created by THIS server process


def _track_owned(doc, state=None):
    """Record a document we just created as service-owned (so we may close it)."""
    try:
        title = doc.GetTitle
        if title:
            _owned_titles.add(title)
            if state is not None:
                state["owned_title"] = title
    except Exception:
        pass


def _is_owned(doc, state=None):
    """True only if the doc is one this service created (this process or persisted)."""
    try:
        title = doc.GetTitle
    except Exception:
        return False
    if not title:
        return False
    if title in _owned_titles:
        return True
    return bool(state) and title == state.get("owned_title")


def _close_owned(sw):
    """Close only documents this service created. Never touch unrelated documents
    (so a user's unsaved parts are never closed or lost)."""
    try:
        for d in (_cv(sw, "GetDocuments") or []):
            try:
                if d.GetTitle in _owned_titles:
                    sw.CloseDoc(d.GetTitle)
            except Exception:
                pass
    except Exception:
        pass


def _new_doc(sw, template=None, state=None):
    """Create a fresh part and mark it service-owned."""
    doc = sw.NewDocument(template or default_template(), 0, 0, 0)
    if doc is None:
        raise RuntimeError("NewDocument 返回 None（模板或许可问题）")
    _track_owned(doc, state)
    return doc


def _refplane(doc):
    f = _cv(doc, "FirstFeature")
    while f is not None:
        if _cv(f, "GetTypeName2") == "RefPlane":
            return f
        f = _cv(f, "GetNextFeature")
    return None


def _last_feature(doc):
    f = _cv(doc, "FirstFeature")
    last = None
    while f is not None:
        last = f
        f = _cv(f, "GetNextFeature")
    return last


def _body_count(doc):
    try:
        bodies = doc.GetBodies2(0, False)
        return len(bodies) if bodies else 0
    except Exception:
        return 0


def _connect():
    import pythoncom
    import win32com.client as win32
    pythoncom.CoInitialize()
    sw = win32.Dispatch("SldWorks.Application")
    try:
        sw.Visible = True
    except Exception:
        pass
    return sw


def _active_part(sw):
    """The active doc, if it is a part with a body."""
    try:
        doc = sw.ActiveDoc
        if doc is not None and _cv(doc, "GetType") == 1 and _body_count(doc) > 0:
            return doc
    except Exception:
        pass
    return None


def _stl_stats(path):
    """Parse a binary STL; report millimetres regardless of source unit."""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 84:
        return {"triangles": 0}
    n = struct.unpack("<I", data[80:84])[0]
    off, pts, vol = 84, [], 0.0
    for _ in range(n):
        vals = struct.unpack("<12f", data[off:off + 48])
        a, b, c = vals[3:6], vals[6:9], vals[9:12]
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1])
                - a[1] * (b[0] * c[2] - b[2] * c[0])
                + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
        pts.extend([a, b, c])
        off += 50
    if not pts:
        return {"triangles": n}
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    scale = 1000.0 if span < 1.0 else 1.0
    return {
        "triangles": n,
        "unit_detected": "m" if scale == 1000.0 else "mm",
        "volume_mm3": round(abs(vol) * scale ** 3, 1),
        "bbox_mm": {
            "x": [round(min(xs) * scale, 2), round(max(xs) * scale, 2)],
            "y": [round(min(ys) * scale, 2), round(max(ys) * scale, 2)],
            "z": [round(min(zs) * scale, 2), round(max(zs) * scale, 2)],
        },
        "size_mm": [round((max(xs) - min(xs)) * scale, 2),
                    round((max(ys) - min(ys)) * scale, 2),
                    round((max(zs) - min(zs)) * scale, 2)],
    }


_CT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '</Types>'
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    '</Relationships>'
)


def _stl_to_3mf(stl_path, mf_path, title="model", unit="millimeter"):
    """Convert a binary STL into a minimal, spec-valid 3MF package.

    Bambu Studio / PrusaSlicer open plain-model 3MF fine; this keeps the
    deliverable a single portable file (mesh + units), no slicer config needed.
    """
    import zipfile

    with open(stl_path, "rb") as fh:
        data = fh.read()
    if len(data) < 84:
        raise ValueError("STL 太小")
    n = struct.unpack("<I", data[80:84])[0]
    off = 84
    index, verts, tris = {}, [], []
    for _ in range(n):
        vals = struct.unpack("<12f", data[off:off + 48])
        off += 50
        tri = []
        for j in (3, 6, 9):
            p = (round(vals[j], 4), round(vals[j + 1], 4), round(vals[j + 2], 4))
            k = index.get(p)
            if k is None:
                k = len(verts)
                index[p] = k
                verts.append(p)
            tri.append(k)
        if tri[0] == tri[1] or tri[1] == tri[2] or tri[0] == tri[2]:
            continue                       # degenerate triangle, skip
        tris.append(tri)

    vx = "".join(f'<vertex x="{v[0]:.4f}" y="{v[1]:.4f}" z="{v[2]:.4f}"/>' for v in verts)
    tx = "".join(f'<triangle v1="{t[0]}" v2="{t[1]}" v3="{t[2]}"/>' for t in tris)
    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="%s" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<metadata name="Title">%s</metadata>'
        '<metadata name="Application">solidworks-mcp</metadata>'
        '<resources><object id="1" type="model"><mesh><vertices>%s</vertices>'
        '<triangles>%s</triangles></mesh></object></resources>'
        '<build><item objectid="1"/></build></model>'
    ) % (unit, title.replace("&", "&amp;").replace("<", "&lt;"), vx, tx)

    with zipfile.ZipFile(mf_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT_XML)
        z.writestr("_rels/.rels", _RELS_XML)
        z.writestr("3D/3dmodel.model", model_xml)
    return {"vertices": len(verts), "triangles": len(tris)}


# ---------------------------------------------------------------- state file
def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("mode") == "boxes" and d.get("boxes"):
            return d
        if d.get("mode") == "prism" and d.get("ops"):
            return d
        if all(k in d for k in ("length", "width", "thickness")):
            d.setdefault("mode", "plate")
            d.setdefault("boxes", [])
            d.setdefault("name", "agent_part")
            return d
    except Exception:
        pass
    return None


def _save_state(S):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(S, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log("save_state failed:", e)


# ---------------------------------------------------------------- print rules

def print_warnings(thickness_mm=None, holes=None):
    """Advisory printability checks for a 0.4mm-nozzle FDM printer.

    Pure function (unit-testable). Returns WARN-level suggestions only — these
    are NOT enforced, and a PASS is not a manufacturing-safety guarantee.
    """
    warns = []
    if thickness_mm is not None and thickness_mm < MIN_WALL_MM:
        warns.append(f"厚度 {thickness_mm}mm < 最小壁厚 {MIN_WALL_MM}mm（0.4 喷嘴打不出 2 道外墙）")
    for h in holes or []:
        if h["dia"] < MIN_HOLE_MM:
            warns.append(f"孔 Ø{h['dia']}mm < {MIN_HOLE_MM}mm，FDM 会缩孔，建议打印后钻孔")
        for k in ("clearance_x", "clearance_y"):
            v = h.get(k)
            if v is not None and v < MIN_WALL_MM:
                warns.append(f"孔 Ø{h['dia']}mm 距板边仅 {v:.2f}mm，低于 {MIN_WALL_MM}mm 会破边")
    return warns


# ---------------------------------------------------------------- modelling

def _rebuild(sw, length, width, thickness, holes, template=None, state=None):
    """Fresh part from parameters: one sketch (rect + circles), one extrusion."""
    _close_owned(sw)
    doc = _new_doc(sw, template, state)

    hx, hy, t = length / 2000.0, width / 2000.0, thickness / 1000.0

    doc.ClearSelection2(True)
    plane = _refplane(doc)
    if plane is None:
        raise RuntimeError("找不到基准面 RefPlane")
    plane.Select2(False, 0)

    doc.SketchManager.InsertSketch(True)
    doc.SketchManager.CreateCornerRectangle(-hx, -hy, 0, hx, hy, 0)
    for h in holes:
        cx, cy, r = h["x"] / 1000.0, h["y"] / 1000.0, h["dia"] / 2000.0
        doc.SketchManager.CreateCircle(cx, cy, 0, cx + r, cy, 0)
    doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)

    sk = _last_feature(doc)
    sk.Select2(False, 0)                 # required, else extrude returns None
    feat = doc.FeatureManager.FeatureExtrusion2(
        True, False, False, 0, 0, t, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, False, True,
        0, 0, False)
    doc.ClearSelection2(True)

    if feat is None:
        raise RuntimeError("拉伸失败（FeatureExtrusion2 返回 None）")
    return doc


def _sketch_extrude(doc, x0, y0, x1, y1, height_m):
    """One rectangle on the front plane, extruded +Z from z=0 to height_m.

    Proven call sequence: select the RefPlane, enter/exit the sketch, then
    SELECT THE SKETCH and call FeatureExtrusion2 (returns None otherwise).
    Everything extruded this way starts at z=0, so overlapping boxes simply
    merge into one solid and no box can float off the build plate.
    """
    doc.ClearSelection2(True)
    pl = _refplane(doc)
    if pl is None:
        raise RuntimeError("找不到基准面 RefPlane")
    pl.Select2(False, 0)
    doc.SketchManager.InsertSketch(True)
    doc.SketchManager.CreateCornerRectangle(x0, y0, 0.0, x1, y1, 0.0)
    doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    sk = _last_feature(doc)
    sk.Select2(False, 0)
    feat = doc.FeatureManager.FeatureExtrusion2(
        True, False, False, 0, 0, height_m, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, False, True,
        0, 0, False)
    doc.ClearSelection2(True)
    if feat is None:
        raise RuntimeError(f"拉伸失败 box=({x0},{y0})-({x1},{y1}) h={height_m}")
    return feat


def _rebuild_boxes(sw, boxes, template=None, state=None):
    """Fresh part built from a list of XY rectangles, each extruded from z=0."""
    _close_owned(sw)
    doc = _new_doc(sw, template, state)
    for b in boxes:
        cx, cy, w, d, h = b["x"], b["y"], b["w"], b["d"], b["h"]
        _sketch_extrude(
            doc,
            (cx - w / 2.0) / 1000.0, (cy - d / 2.0) / 1000.0,
            (cx + w / 2.0) / 1000.0, (cy + d / 2.0) / 1000.0,
            h / 1000.0,
        )
    return doc


# ------------------------------------------------- prism kernel (arbitrary parts)
#
# Measured sketch frames on this install (SW 2024 SP0.1, gb_part template); the
# signs were resolved empirically by unioning two mutually offset prisms and
# reading the resulting span/volume out of the exported mesh:
#     plane "XY" (Front, tree idx 0): local (a,b) -> model ( a,  b, 0)  extrude +Z
#     plane "XZ" (Top,   tree idx 1): local (a,b) -> model ( a,  0,-b)  extrude along Y
#     plane "YZ" (Right, tree idx 2): local (a,b) -> model ( 0,  b,-a)  extrude +X
#
# Two API traps that cost the most time here:
#   * FeatureCut4 SILENTLY RETURNS None unless the 3rd argument (Dir) is True.
#     With Dir=False no argument count or end-condition ever produces a cut.
#   * A blind cut/boss starts at the sketch plane unless T0=3 (start offset) is
#     used, in which case StartOffset shifts it along +normal in metres.
_PLANE_INDEX = {"XY": 0, "XZ": 1, "YZ": 2}


def _refplanes(doc):
    out, f = [], _cv(doc, "FirstFeature")
    while f is not None:
        if _cv(f, "GetTypeName2") == "RefPlane":
            out.append(f)
        f = _cv(f, "GetNextFeature")
    return out


_CIRCLE_SEGS = 8           # bore circles drawn as octagons; see _circle_pts
# The acceptable segment count for a polyline hole depends on the OUTER
# profile on this install (measured): rod 8x14 accepts 8 or 48 but rejects 16;
# leg 10x10 accepts 8-16 but rejects 24 and 48. N=8 is the only count that
# works for both, and the circumscribed construction keeps the inscribed
# circle at exactly the nominal diameter (a O4 mm shaft still passes O4.2).


def _circle_pts(cx, cy, r, n=_CIRCLE_SEGS):
    """Closed polygon standing in for a circle of radius r.

    SketchManager.CreateCircle is unreliable on this install: asking for r=2.1
    yields a Ø5.0 hole and r=1.0 yields no hole at all (silently), while closed
    polylines reproduce the requested size exactly. The radius is the
    CIRCUMSCRIBED one, so the flats sit exactly on the nominal circle and a
    Ø4 mm shaft still passes a "Ø4.2" bore.
    """
    import math

    rr = r / math.cos(math.pi / n)
    return [(cx + rr * math.cos(2 * math.pi * i / n),
             cy + rr * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _sketch_profile(doc, plane, pts, loops, cut=False):
    """Closed polyline + extra closed loops ((a,b,r) circles or point lists).

    Circles are drawn as polylines: SketchManager.CreateCircle is unreliable
    on this install (r=2.1 silently becomes a O5.0 hole; r=1.0 becomes nothing).
    Boss inner loops use 8 segments (the only count accepted by both the rod
    and the leg outer profiles); CUT profiles use 12 (>=16 is rejected by
    FeatureCut4 on thin parts).
    """
    planes = _refplanes(doc)
    idx = _PLANE_INDEX.get(plane, 0)
    if idx >= len(planes):
        raise RuntimeError(f"基准面不足: 需要 {plane}")
    doc.ClearSelection2(True)
    planes[idx].Select2(False, 0)
    doc.SketchManager.InsertSketch(True)
    sm = doc.SketchManager

    def loop(points):
        n = len(points)
        for i in range(n):
            a, b = points[i], points[(i + 1) % n]
            sm.CreateLine(a[0] / 1000.0, a[1] / 1000.0, 0.0,
                          b[0] / 1000.0, b[1] / 1000.0, 0.0)

    if pts:
        loop(pts)
    for it in loops or []:
        if it and isinstance(it[0], (list, tuple)):
            loop(it)                                   # polygon loop
        else:
            ca, cb, r = it
            loop(_circle_pts(ca, cb, r, 12 if cut else _CIRCLE_SEGS))
    doc.SketchManager.InsertSketch(True)
    doc.ClearSelection2(True)
    return _last_feature(doc)


def _boss_prism(doc, sk, depth_mm, start_mm=0.0):
    doc.ClearSelection2(True)
    sk.Select2(False, 0)
    feat = doc.FeatureManager.FeatureExtrusion2(
        True, False, False, 0, 0, depth_mm / 1000.0, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, False, True,
        3, start_mm / 1000.0, False)
    doc.ClearSelection2(True)
    return feat


def _cut_prism(doc, sk, depth_mm=100.0, start_mm=0.0, through_all=False):
    t1 = 6 if through_all else 0                    # 6 = through all both
    d = depth_mm / 1000.0
    doc.ClearSelection2(True)
    sk.Select2(False, 0)
    feat = doc.FeatureManager.FeatureCut4(
        True, False, True, t1, t1, d, d,
        False, False, False, False, 0.01745, 0.01745,
        False, False, False, False,
        False, True, True, False, False, False,
        0 if through_all else 3, start_mm / 1000.0, False, False)
    doc.ClearSelection2(True)
    return feat


def _rebuild_ops(sw, ops, template=None, state=None):
    """Fresh part from an ordered op list: boss/cut prisms on the 3 base planes."""
    _close_owned(sw)
    doc = _new_doc(sw, template, state)
    for op in ops:
        kind = op.get("kind", "boss")
        sk = _sketch_profile(doc, op.get("plane", "XY"),
                             op.get("pts") or [], op.get("holes") or [],
                             cut=(kind == "cut"))
        if kind == "boss":
            feat = _boss_prism(doc, sk, float(op["depth"]), float(op.get("start", 0.0)))
        elif kind == "cut":
            feat = _cut_prism(doc, sk, float(op.get("depth", 100.0)),
                              float(op.get("start", 0.0)),
                              bool(op.get("through_all")))
        else:
            raise RuntimeError(f"未知 op 类型: {kind}")
        if feat is None:
            raise RuntimeError(f"{kind} 失败: {op.get('role')}")
    for call in ("ForceRebuild3", "EditRebuild3"):
        fn = getattr(doc, call, None)
        if fn is not None:
            try:
                fn(True) if call == "ForceRebuild3" else fn()
                break
            except Exception:
                pass
    doc.ClearSelection2(True)
    return doc


# ------------------------------------------------- 4-piece V-fork phone stand

def _oct(w, h, c):
    """CCW octagon, width w, height h, 45-degree corner chamfer c."""
    hw, hh = w / 2.0, h / 2.0
    return [(-hw + c, -hh), (hw - c, -hh), (hw, -hh + c), (hw, hh - c),
            (hw - c, hh), (-hw + c, hh), (-hw, hh - c), (-hw, -hh + c)]


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _strip(origin, direction, half_w, s0, s1):
    """Rectangle swept along a 2D direction (for the angled V-fork slots)."""
    ox, oy = origin
    dx, dy = direction
    nx, ny = -dy, dx
    return [(ox + s * dx + t * nx, oy + s * dy + t * ny)
            for s, t in ((s0, -half_w), (s1, -half_w), (s1, half_w), (s0, half_w))]


PART_NAMES = ("01_Spine_Rod", "02_Base_Leg", "03_Tail_Hub", "04_Gravity_Cradle")


def part_spine_rod(rod_len=155.0, bore_mm=4.2, pin_z=18.0):
    """8x14 octagonal spine with a O4.2 core bore and a O3.2 cross pin hole."""
    return [
        {"kind": "boss", "plane": "XY", "role": "主脊柱杆 8x14 L155",
         "pts": _oct(8.0, 14.0, 2.0), "holes": [_circle_pts(0, 0, bore_mm / 2.0, 48)],
         "start": 0.0, "depth": rod_len},
        # local a = -Z, b = +Y on the Right plane -> pin hole at Z=18, Y=0
        {"kind": "cut", "plane": "YZ", "role": "根部定位销孔 Ø3.2 @Z18",
         "pts": [], "holes": [(-pin_z, 0.0, 1.6)], "through_all": True},
    ]


def part_base_leg(leg_len=135.0, bore_mm=4.2, pivot_z=14.0, grooves=(35.0, 115.0)):
    """10x10 flat-bottom octagonal leg: core bore, hinge pin hole, grip grooves."""
    ops = [
        {"kind": "boss", "plane": "XY", "role": "贴地底杆 10x10 L135",
         "pts": _oct(10.0, 10.0, 2.5), "holes": [_oct(bore_mm, bore_mm, 1.0)],
         "start": 0.0, "depth": leg_len},
        # Top plane: local a = +X, b = -Z -> hinge hole at X=0, Z=14 along Y
        {"kind": "cut", "plane": "XZ", "role": "折叠转轴孔 Ø3.2 @Z14",
         "pts": [], "holes": [(0.0, -pivot_z, 1.6)], "through_all": True},
    ]
    for z in grooves:
        # 3 mm wide x 0.8 mm deep ring seat on the Y=-5 face, full X width
        ops.append({"kind": "cut", "plane": "YZ",
                    "role": f"防滑胶圈定位槽 z={z:g}",
                    "pts": _rect(-(z + 1.5), -5.0, -(z - 1.5), -4.2),
                    "holes": [], "start": -6.0, "depth": 12.0})
    return ops


def part_tail_hub(width=56.0, prof=((0.0, 0.0), (48.0, 0.0), (48.0, 14.0),
                                   (42.0, 38.0), (0.0, 38.0)),
                  slot_floor=(14.0, 8.0), slot_tilt=28.0, slot_w=8.3,
                  slot_h=14.3, slot_len=45.0, pin_at=(22.45, 19.90),
                  channel=10.3, leg_w=10.3, splay=30.0,
                  leg_x=14.0, leg_y=18.0):
    """Tail hub: 38 mm deep 28-degree dead-stop slot, pin bore, V-fork slots.

    prof is given as (Y, Z) pairs. The one deviation from the brief: the top
    front corner is (42, 38) instead of (26, 38). With a 26 mm top face the
    28-degree slot breaks out through the slanted front face and would leave no
    front lip at all; 42 mm keeps a 5.7 mm dead-stop lip over the rod.
    slot_floor is (Y, Z) of the slot's bottom face centre; Z=8 puts the lowest
    slot corner at Z=4.64, i.e. a >4 mm solid floor as required.
    """
    import math

    ops = [{"kind": "boss", "plane": "YZ", "role": "尾座鞍座主体 56 宽",
            "pts": [(-z, y) for (y, z) in prof], "holes": [],
            "start": -width / 2.0, "depth": width}]

    b = math.radians(slot_tilt)
    u = (math.sin(b), math.cos(b))              # (dY, dZ) up-and-forward
    n = (math.cos(b), -math.sin(b))             # slot half-height direction
    hh = slot_h / 2.0
    cy, cz = slot_floor
    corners = [(cy + s * u[0] + t * n[0], cz + s * u[1] + t * n[1])
               for s, t in ((0.0, -hh), (slot_len, -hh), (slot_len, hh), (0.0, hh))]
    ops.append({"kind": "cut", "plane": "YZ", "role": "38mm 深埋死点斜槽 28deg",
                "pts": [(-z, y) for (y, z) in corners], "holes": [],
                "start": -slot_w / 2.0, "depth": slot_w})

    ops.append({"kind": "cut", "plane": "YZ", "role": "主脊柱锁死销孔 Ø3.2",
                "pts": [], "holes": [(-pin_at[1], pin_at[0], 1.6)],
                "through_all": True})

    hw = leg_w / 2.0
    ang = math.radians(splay)
    for sx in (-1.0, 1.0):
        d = (sx * math.sin(ang), math.cos(ang))
        ops.append({"kind": "cut", "plane": "XY",
                    "role": f"V 叉贴地插槽 {'左' if sx < 0 else '右'} {-sx*splay:+.0f}deg",
                    "pts": _strip((sx * leg_x, leg_y), d, hw, 0.0, 40.0),
                    "holes": [], "start": 0.0, "depth": channel})
        ops.append({"kind": "cut", "plane": "XY",
                    "role": f"支点竖直销孔 {'左' if sx < 0 else '右'} Ø3.2",
                    "pts": [], "holes": [(sx * leg_x, leg_y, 1.6)],
                    "start": 0.0, "depth": 40.0})
    return ops


def part_gravity_cradle():
    """Gravity self-locking sleeve + two hooks with a 44 mm charging window."""
    ops = [
        {"kind": "boss", "plane": "XY", "role": "重力自锁滑套 18x20x24",
         "pts": _rect(-9.0, -10.0, 9.0, 10.0),
         "holes": [_oct(8.35, 14.5, 2.0)], "start": 0.0, "depth": 24.0},
        {"kind": "boss", "plane": "XY", "role": "承托横梁 70x8x10",
         "pts": _rect(-35.0, 7.5, 35.0, 15.5), "holes": [],
         "start": 0.0, "depth": 10.0},
    ]
    for sx in (-1.0, 1.0):
        x0, x1 = (22.0, 35.0) if sx > 0 else (-35.0, -22.0)
        side = "右" if sx > 0 else "左"
        ops.append({"kind": "boss", "plane": "XY", "role": f"{side}托臂 13x15x5",
                    "pts": _rect(x0, 13.0, x1, 28.0), "holes": [],
                    "start": 0.0, "depth": 5.0})
        ops.append({"kind": "boss", "plane": "XY", "role": f"{side}防滑挡唇 13x6x16",
                    "pts": _rect(x0, 27.0, x1, 33.0), "holes": [],
                    "start": 0.0, "depth": 16.0})
    return ops


PART_BUILDERS = {
    "01_Spine_Rod": part_spine_rod,
    "02_Base_Leg": part_base_leg,
    "03_Tail_Hub": part_tail_hub,
    "04_Gravity_Cradle": part_gravity_cradle,
}


def prism_print_warnings(ops):
    """Advisory printability checks for a prism-built part.

    Every prism is a constant cross-section swept along its sketch normal, so
    there is no overhang anywhere; the only warnings are sub-nozzle features
    and zero-height operations.
    """
    warns = []
    for o in ops:
        d = o.get("depth")
        if d is not None and float(d) < MIN_WALL_MM:
            warns.append(f"{o.get('role')} 拉伸/切除深度 {d}mm < {MIN_WALL_MM}mm")
    return warns


def _ensure_doc(sw, S):
    """Attach to a service-owned active part; otherwise rebuild from saved params.

    Never silently save/export an unrelated active part: only a document whose
    title matches the service's tracked ownership is accepted as-is.
    """
    doc = _active_part(sw)
    if doc is not None and _is_owned(doc, S):
        return doc, False
    try:
        if S.get("mode") == "prism" and S.get("ops"):
            return _rebuild_ops(sw, S["ops"], state=S), True
        if S.get("mode") == "boxes" and S.get("boxes"):
            return _rebuild_boxes(sw, S["boxes"], state=S), True
        if S.get("length") is None:
            return None, False
        doc = _rebuild(sw, S["length"], S["width"], S["thickness"], S["holes"], state=S)
        return doc, True
    except Exception as e:
        _log("_ensure_doc rebuild failed:", e)
        return None, False


def _hole_clearance(hole, length, width):
    cx, cy = hole["x"], hole["y"]
    return {
        "clearance_x": min(cx + length / 2, length / 2 - cx) - hole["dia"] / 2,
        "clearance_y": min(cy + width / 2, width / 2 - cy) - hole["dia"] / 2,
    }


# ------------------------------------------------- phone stand (real part)

PHONE_STAND_DEFAULTS = {
    "angle_deg": 30.0,        # back rest angle measured from VERTICAL
    "width_mm": 120.0,        # base width  (X)
    "depth_mm": None,         # base depth  (Y); None = derive from the ridge fan
    "phone_len_mm": 164.0,    # Mate 70 160.9 + silicone case
    "phone_w_mm": 79.0,
    "phone_t_mm": 11.0,
    "base_h_mm": 3.0,
    "lip_depth_mm": 8.0,      # front stop depth
    "lip_h_mm": 16.0,         # front stop height
    "channel_mm": 22.0,       # charging channel width through the front stop
    "fin_t_mm": 4.0,          # back-rest ridge thickness
    "fin_w_mm": 110.0,        # back-rest ridge width
    "support_z_mm": [28.0, 80.0],   # ridge heights (2 is enough: 2 lines define a plane)
    "gusset_d_mm": 6.0,       # stiffening web depth behind each ridge
    "gusset_h_mm": 10.0,
}


def phone_stand_boxes(**kw):
    """Box list for the one-piece phone stand.

    Structure (all vertical prisms standing on the base, so zero overhangs):
      * base plate
      * front stop, split in two with a 22 mm channel -> a plugged-in cable
        (and its connector) drops straight through and runs out the front
      * a fan of back-rest ridges whose top-front corners all lie on one
        straight line at `angle_deg` from vertical -> the phone's back rests
        on every ridge at once (two line contacts, not a knife edge)

    Why the ridges are "fanned" and not a staircase: a rigid one-piece stand
    can only hold ONE lean angle. Any material behind the phone's back line
    blocks it, so the support must be the outermost surface in that direction
    -- which fixes a single angle. Want another angle -> change `angle_deg`
    and print a second stand.
    """
    import math

    p = dict(PHONE_STAND_DEFAULTS)
    p.update({k: v for k, v in kw.items() if v is not None})
    beta = math.radians(float(p["angle_deg"]))
    tanb = math.tan(beta)

    W = float(p["width_mm"])
    bh = float(p["base_h_mm"])
    lip_d, lip_h = float(p["lip_depth_mm"]), float(p["lip_h_mm"])
    chan = float(p["channel_mm"])
    ft, fw = float(p["fin_t_mm"]), float(p["fin_w_mm"])
    gd, gh = float(p["gusset_d_mm"]), float(p["gusset_h_mm"])
    support_z = sorted(float(z) for z in p["support_z_mm"] if float(z) > bh)

    # base depth: big enough to carry the whole ridge fan, never less than a
    # stable footprint
    reach = (support_z[-1] - bh) * tanb if support_z else 0.0
    auto_d = lip_d + reach + ft + gd + 6.0
    D = max(float(p["depth_mm"] or 0.0), auto_d, 70.0)

    boxes = [{"x": 0.0, "y": 0.0, "w": W, "d": D, "h": bh, "role": "base"}]

    # --- front stop, two segments leaving a charging channel in the middle
    y_front = -D / 2.0
    lip_cy = y_front + lip_d / 2.0
    seg_w = W / 2.0 - chan / 2.0
    if seg_w > 0:
        for sgn in (-1, 1):
            boxes.append({"x": sgn * (chan / 2.0 + seg_w / 2.0), "y": lip_cy,
                          "w": seg_w, "d": lip_d, "h": lip_h,
                          "role": "front stop"})
    phone_bottom_y = y_front + lip_d     # the phone's bottom edge rests here

    # --- back-rest ridges + stiffening webs
    for z in support_z:
        yc = phone_bottom_y + (z - bh) * tanb    # ridge front face on the ray
        boxes.append({"x": 0.0, "y": yc + ft / 2.0, "w": fw, "d": ft, "h": z,
                      "role": f"ridge z={z:g}"})
        if gd > 0 and gh > 0:
            boxes.append({"x": 0.0, "y": yc + ft + gd / 2.0, "w": fw, "d": gd,
                          "h": min(gh, z), "role": f"web z={z:g}"})
    return boxes


def boxes_volume_mm3(boxes):
    """Upper bound on volume (overlapping boxes counted twice)."""
    return sum(b["w"] * b["d"] * b["h"] for b in boxes)


def _boxes_bbox(boxes):
    if not boxes:
        return None
    xs, ys, zs = [], [], []
    for b in boxes:
        xs += [b["x"] - b["w"] / 2, b["x"] + b["w"] / 2]
        ys += [b["y"] - b["d"] / 2, b["y"] + b["d"] / 2]
        zs += [0.0, b["h"]]
    return {"x": [round(min(xs), 2), round(max(xs), 2)],
            "y": [round(min(ys), 2), round(max(ys), 2)],
            "z": [round(min(zs), 2), round(max(zs), 2)]}


def boxes_print_warnings(boxes):
    """Advisory printability checks for a box-built part. Every box stands on z=0 => no overhang."""
    warns = []
    for b in boxes:
        thin = min(b["w"], b["d"])
        if thin < MIN_WALL_MM:
            warns.append(f"{b.get('role', 'block')} 最小截面 {thin:.2f}mm "
                         f"< 最小壁厚 {MIN_WALL_MM}mm")
        if b["h"] <= 0:
            warns.append(f"{b.get('role', 'block')} 高度为 0")
    # narrow footprint blocks (thin walls standing alone) are a bed-adhesion risk
    for b in boxes:
        if b["h"] > 60 and min(b["w"], b["d"]) < 3.0:
            warns.append(f"{b.get('role', 'block')} 高 {b['h']:.0f}mm 但只有 "
                         f"{min(b['w'], b['d']):.1f}mm 宽，立不稳，建议加宽或加筋")
    return warns


# ---------------------------------------------------------------- MCP server

def build_server():
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    mcp = MCPServer(
        "solidworks",
        title="SolidWorks 参数化建模",
        description="通过 COM 驱动本机 SOLIDWORKS：参数化平板、孔、导出 STL、打印可行性建议（非强制）、交给 Bambu Studio。",
    )

    S = _load_state() or {"mode": "plate", "length": 60.0, "width": 40.0,
                          "thickness": 5.0, "holes": [], "boxes": [],
                          "name": "agent_part"}
    S.setdefault("mode", "plate")
    S.setdefault("boxes", [])
    S.setdefault("name", "agent_part")
    S.setdefault("owned_title", None)
    _log("server starting, state:", S)

    def _holes_with_clearance():
        return [dict(h, **_hole_clearance(h, S["length"], S["width"])) for h in S["holes"]]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    def sw_status() -> dict:
        """检查 SolidWorks 连接状态：是否在运行、当前文档、实体数量。

        只读查询，但可能通过 COM 激活（启动）本机 SOLIDWORKS，不修改任何文档。
        """
        with _com_lock:
            sw = _connect()
            docs = _cv(sw, "GetDocuments") or []
            active = sw.ActiveDoc
            return {
                "connected": True,
                "revision": str(sw.RevisionNumber),
                "open_documents": [d.GetTitle for d in docs],
                "active_document": active.GetTitle if active else None,
                "solid_bodies": _body_count(active) if active else 0,
                "template": default_template(),
                "template_exists": os.path.exists(default_template()),
                "bambu_exe": bambu_exe(),
                "bambu_exe_exists": bool(bambu_exe()),
                "out_dir": out_dir(),
                "mode": S.get("mode"),
                "model_name": S.get("name"),
                "boxes": len(S.get("boxes") or []),
                "model_params": {"length": S["length"], "width": S["width"],
                                 "thickness": S["thickness"], "holes": S["holes"]},
            }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_plate(length_mm: float = 60, width_mm: float = 40, thickness_mm: float = 5) -> dict:
        """建一块长方形平板（原点居中，厚度沿 +Z 拉伸）。

        会重建并替换本服务自己的模型文档（从不关闭或改动你的其他文档），
        并清空已有孔。写模型状态文件到输出目录。
        """
        try:
            length = _require_finite_positive(length_mm, "length_mm", 2000.0)
            width = _require_finite_positive(width_mm, "width_mm", 2000.0)
            thickness = _require_finite_positive(thickness_mm, "thickness_mm", 500.0)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        with _com_lock:
            S.update({"mode": "plate", "name": "agent_part", "boxes": [],
                      "length": length, "width": width,
                      "thickness": thickness, "holes": []})
            try:
                sw = _connect()
                doc = _rebuild(sw, S["length"], S["width"], S["thickness"], S["holes"], state=S)
            except Exception as e:
                _log("sw_plate:", traceback.format_exc())
                return {"ok": False, "error": str(e)}
            _save_state(S)
            return {
                "ok": True,
                "solid_bodies": _body_count(doc),
                "dimensions": {"length": length, "width": width, "thickness": thickness},
                "warnings": print_warnings(thickness_mm=thickness),
            }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_hole(diameter_mm: float = 8, x_mm: float = 0, y_mm: float = 0) -> dict:
        """在平板上加一个通孔（孔心相对板中心偏移 x/y）。模型会整体重建。"""
        try:
            dia = _require_finite_positive(diameter_mm, "diameter_mm", 1000.0)
            x = _require_finite(x_mm, "x_mm")
            y = _require_finite(y_mm, "y_mm")
        except ValueError as e:
            return {"ok": False, "error": str(e), "holes_kept": S["holes"]}
        half_l, half_w = S["length"] / 2.0, S["width"] / 2.0
        if abs(x) > half_l or abs(y) > half_w:
            return {"ok": False,
                    "error": f"孔心 ({x}, {y}) 超出平板范围 ±{half_l}×±{half_w}",
                    "holes_kept": S["holes"]}
        with _com_lock:
            holes = S["holes"] + [{"dia": dia, "x": x, "y": y}]
            try:
                sw = _connect()
                doc = _rebuild(sw, S["length"], S["width"], S["thickness"], holes, state=S)
            except Exception as e:
                _log("sw_hole:", traceback.format_exc())
                return {"ok": False, "error": str(e), "holes_kept": S["holes"]}
            S["holes"] = holes
            _save_state(S)
            return {
                "ok": True,
                "holes": _holes_with_clearance(),
                "solid_bodies": _body_count(doc),
                "warnings": print_warnings(thickness_mm=S["thickness"],
                                           holes=_holes_with_clearance()),
            }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_holes_clear() -> dict:
        """清空所有孔并重建模型。"""
        with _com_lock:
            S["holes"] = []
            try:
                sw = _connect()
                _rebuild(sw, S["length"], S["width"], S["thickness"], [], state=S)
            except Exception as e:
                _log("sw_holes_clear:", traceback.format_exc())
                return {"ok": False, "error": str(e)}
            _save_state(S)
            return {"ok": True, "holes": []}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def sw_check_printability() -> dict:
        """检查当前模型是否适合 0.4 喷嘴 FDM 打印。

        返回建议性告警（WARN），不强制执行；PASS 不代表制造安全保证。
        纯本地状态读取，不连接 SOLIDWORKS、不写文件。
        """
        if S.get("mode") == "prism" and S.get("ops"):
            warns = prism_print_warnings(S["ops"])
            return {
                "nozzle_mm": NOZZLE_MM,
                "mode": "prism",
                "model_name": S.get("name"),
                "rules": {"min_wall_mm": MIN_WALL_MM, "min_hole_mm": MIN_HOLE_MM},
                "stats": {"features": len(S["ops"]), "overhang_deg": 0,
                          "supports_needed": False},
                "warnings": warns,
                "verdict": "PASS" if not warns else "WARN",
            }
        if S.get("mode") == "boxes" and S.get("boxes"):
            boxes = S["boxes"]
            warns = boxes_print_warnings(boxes)
            vol = boxes_volume_mm3(boxes)
            return {
                "nozzle_mm": NOZZLE_MM,
                "mode": "boxes",
                "model_name": S.get("name"),
                "rules": {"min_wall_mm": MIN_WALL_MM, "min_hole_mm": MIN_HOLE_MM},
                "stats": {
                    "blocks": len(boxes),
                    "bbox_mm": _boxes_bbox(boxes),
                    "volume_mm3_upper_bound": round(vol, 0),
                    "filament_g_pla_estimate": round(vol * 1.24 / 1000.0, 0),
                    "overhang_deg": 0,
                },
                "warnings": warns,
                "verdict": "PASS" if not warns else "WARN",
            }
        holes = _holes_with_clearance()
        warns = print_warnings(thickness_mm=S["thickness"], holes=holes)
        return {
            "nozzle_mm": NOZZLE_MM,
            "mode": "plate",
            "rules": {"min_wall_mm": MIN_WALL_MM, "min_hole_mm": MIN_HOLE_MM,
                      "clearance_mm": CLEARANCE_MM},
            "dimensions": {"length": S["length"], "width": S["width"],
                           "thickness": S["thickness"], "holes": holes},
            "warnings": warns,
            "verdict": "PASS" if not warns else "WARN",
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_phone_stand(angle_deg: float = 30.0, width_mm: float = 120.0,
                       depth_mm: float = 0.0, channel_mm: float = 22.0,
                       support_z_mm: str = "") -> dict:
        """建一个一体打印手机支架（华为 Mate 70 带壳尺寸调过）。

        angle_deg   靠背倾角（从竖直方向量，30° = 通用看视频角度）
        结构：底板 + 中间开口的前挡筋（充电线/插头能直接穿过去）+ 一列
        顶角共线的靠背棱（手机背面同时压在所有棱上，不是刀尖平衡）。
        全部是立在底板上的竖直棱柱，零悬垂、零支撑、零装配。
        depth_mm 0 = 自动（按倾角算到刚好托住靠背棱）。
        support_z_mm 可选，逗号分隔的棱高，例如 "28,80"。
        """
        try:
            angle = _require_finite(angle_deg, "angle_deg")
            if not (5.0 <= angle <= 80.0):
                return {"ok": False, "error": f"angle_deg 需在 5~80 之间（收到 {angle}）"}
            width = _require_finite_positive(width_mm, "width_mm", 500.0)
            channel = _require_finite_positive(channel_mm, "channel_mm", 200.0)
            if depth_mm:
                _require_finite_positive(depth_mm, "depth_mm", 2000.0)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        with _com_lock:
            try:
                zs = ([float(x) for x in support_z_mm.replace(" ", "").split(",") if x]
                      if support_z_mm.strip() else None)
            except ValueError:
                return {"ok": False, "error": f"support_z_mm 解析失败: {support_z_mm!r}"}
            kw = {"angle_deg": angle, "width_mm": width,
                  "channel_mm": channel}
            if depth_mm and depth_mm > 0:
                kw["depth_mm"] = depth_mm
            if zs:
                kw["support_z_mm"] = zs
            boxes = phone_stand_boxes(**kw)
            S.update({"mode": "boxes", "boxes": boxes,
                      "name": f"phone_stand_{int(round(angle))}deg",
                      "length": 0.0, "width": 0.0, "thickness": 0.0, "holes": []})
            try:
                sw = _connect()
                doc = _rebuild_boxes(sw, boxes, state=S)
            except Exception as e:
                _log("sw_phone_stand:", traceback.format_exc())
                return {"ok": False, "error": str(e), "boxes": len(boxes)}
            _save_state(S)
            return {
                "ok": True,
                "model_name": S["name"],
                "blocks": len(boxes),
                "bbox_mm": _boxes_bbox(boxes),
                "solid_bodies": _body_count(doc),
                "volume_mm3_upper_bound": round(boxes_volume_mm3(boxes), 0),
                "geometry": boxes,
                "warnings": boxes_print_warnings(boxes),
            }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_part(name: str = "01_Spine_Rod") -> dict:
        """建 4 件套「贴地 V 叉 + 单脊柱」手机支架的任意一件并留在 SW 里。

        name 可选（必须精确）：
          01_Spine_Rod       8×14 八角主脊柱杆，Ø4.2 贯通孔 + Ø3.2 横销孔，155 长
          02_Base_Leg        10×10 平底八角贴地杆，Ø4.2 贯通孔 + Ø3.2 转轴孔
                             + 两处 3×0.8 防滑圈槽，135 长（需打 2 根）
          03_Tail_Hub        56 宽鞍座：38mm 深 28° 死点斜槽 + 锁死销孔
                             + ±30° V 叉贴地插槽 + 支点竖直销孔
          04_Gravity_Cradle  重力自锁滑套 + 双钩托架，中间 44mm 充电净空
        全部由「草图多边形 + 拉伸/切除棱柱」构成，无放样、无扫描、无圆角。
        建完用 sw_save_part / sw_export_stl 落盘（默认文件名就是零件名）。
        """
        with _com_lock:
            fn = PART_BUILDERS.get(name)
            if fn is None:
                return {"ok": False, "error": f"未知零件 {name!r}",
                        "available": list(PART_BUILDERS)}
            ops = fn()
            S.clear()
            S.update({"mode": "prism", "name": name, "ops": ops, "boxes": [],
                      "length": 0.0, "width": 0.0, "thickness": 0.0, "holes": []})
            try:
                sw = _connect()
                doc = _rebuild_ops(sw, ops, state=S)
            except Exception as e:
                _log("sw_part:", traceback.format_exc())
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "ops": len(ops), "part": name}
            _save_state(S)
            return {
                "ok": True,
                "part": name,
                "solid_bodies": _body_count(doc),
                "features": len(ops),
                "detail": [{"op": o["kind"], "plane": o["plane"],
                            "role": o.get("role")} for o in ops],
                "warnings": prism_print_warnings(ops),
            }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_save_part(path: str = "", overwrite: bool = False) -> dict:
        """保存 .SLDPRT 到磁盘（默认 <out>/<模型名>.SLDPRT）。

        输出限定在输出目录内；默认不覆盖已存在文件（传 overwrite=true 才覆盖），
        先写临时文件再原子替换，保存失败不会破坏原文件。
        """
        with _com_lock:
            sw = _connect()
            doc, _ = _ensure_doc(sw, S)
            if doc is None:
                return {"ok": False, "error": "没有模型，请先 sw_plate"}
            try:
                target = _resolve_output_path(path, "SLDPRT", S.get("name", "agent_part"))
            except ValueError as e:
                return {"ok": False, "error": str(e)}

            def writer(tmp):
                doc.ClearSelection2(True)
                ok = doc.SaveAs(tmp)
                if not ok or not os.path.exists(tmp):
                    raise RuntimeError(f"保存失败 ret={ok}")

            try:
                _atomic_export(target, writer, overwrite)
            except (FileExistsError, RuntimeError, ValueError) as e:
                return {"ok": False, "error": str(e), "path": target}
            return {"ok": True, "path": target, "bytes": os.path.getsize(target)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_export_stl(path: str = "", overwrite: bool = False) -> dict:
        """导出 STL（默认 <out>/<模型名>.STL）。

        导出前清空选择，避免只导出选中面。输出限定在输出目录内；默认不覆盖
        已存在文件；先写临时文件再原子替换，失败不破坏原文件。
        """
        try:
            return _sw_export_stl_impl(path, overwrite)
        except Exception as e:
            _log("sw_export_stl:", traceback.format_exc())
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _sw_export_stl_impl(path: str = "", overwrite: bool = False) -> dict:
        with _com_lock:
            sw = _connect()
            doc, rebuilt = _ensure_doc(sw, S)
            if doc is None:
                return {"ok": False, "error": "没有模型，请先 sw_plate"}
            try:
                target = _resolve_output_path(path, "STL", S.get("name", "agent_part"))
            except ValueError as e:
                return {"ok": False, "error": str(e)}

            stats_box = {}

            def writer(tmp):
                doc.ClearSelection2(True)
                ok = doc.SaveAs(tmp)
                if not os.path.exists(tmp):
                    raise RuntimeError(f"导出失败 ret={ok}")
                stats_box["stats"] = _stl_stats(tmp)
                if stats_box["stats"].get("triangles", 0) <= 0:
                    raise RuntimeError("导出失败（网格为空）")

            try:
                _atomic_export(target, writer, overwrite)
            except (FileExistsError, RuntimeError, ValueError) as e:
                return {"ok": False, "error": str(e), "path": target}
            stats = stats_box["stats"]
            return {"ok": True, "path": target, "bytes": os.path.getsize(target),
                    "mesh": stats, "rebuilt": rebuilt,
                    "verdict": "OK" if stats.get("triangles", 0) > 0 else "EMPTY"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    def sw_export_3mf(path: str = "", overwrite: bool = False) -> dict:
        """导出 3MF（默认 <out>/<模型名>.3mf）。

        先让 SOLIDWORKS 出 STL，再把网格重新打包成一个规范 3MF 包
        （单位 mm，顶点去重），Bambu Studio 可直接打开/切片。
        默认不覆盖已存在文件；先写临时文件再原子替换。
        """
        tmp_stl = None
        try:
            tmp_stl = os.path.join(out_dir(), f".tmp_{secrets.token_hex(8)}.STL")
            stl = _sw_export_stl_impl(tmp_stl, overwrite=True)
            if not stl.get("ok"):
                return {"ok": False, "step": "stl", "error": stl.get("error")}
            try:
                target = _resolve_output_path(path, "3mf", S.get("name", "agent_part"))
            except ValueError as e:
                return {"ok": False, "error": str(e)}

            info = {}

            def writer(tmp):
                info.update(_stl_to_3mf(stl["path"], tmp, title=S.get("name", "model")))

            try:
                _atomic_export(target, writer, overwrite)
            except (FileExistsError, RuntimeError, ValueError) as e:
                return {"ok": False, "error": str(e), "path": target}
            return {"ok": True, "path": target,
                    "bytes": os.path.getsize(target),
                    "units": "millimeter",
                    "mesh": {"triangles": info.get("triangles")}}
        except Exception as e:
            _log("sw_export_3mf:", traceback.format_exc())
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            if tmp_stl and os.path.exists(tmp_stl):
                try:
                    os.remove(tmp_stl)
                except Exception:
                    pass

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    def sw_open_in_bambu(stl_path: str = "") -> dict:
        """把 STL 交给 Bambu Studio 界面打开，供人工切片与打印。

        会拉起 Bambu Studio GUI（副作用：新窗口）。部分 Bambu Studio 版本
        （如 02.09.01.70）无命令行切片能力，因此采用
        「自动打开 GUI + 人工点切片/打印」的半自动方案。
        """
        import subprocess
        stl_path = stl_path or os.path.join(out_dir(), f"{S.get('name', 'agent_part')}.STL")
        if not os.path.exists(stl_path):
            return {"ok": False, "error": f"STL 不存在: {stl_path}，请先 sw_export_stl"}
        exe = bambu_exe()
        if not exe:
            return {"ok": False, "error": "未找到 Bambu Studio，请设置环境变量 BAMBU_EXE"}
        subprocess.Popen([exe, stl_path], close_fds=True)
        return {"ok": True, "launched": exe, "file": stl_path,
                "next_steps": ["等 20-30 秒启动", "确认模型在盘面上且已选机型",
                               "点「切片」，检查预览", "点「打印」发送到打印机"],
                "note": "本机 Bambu CLI 不可用，切片必须走 GUI"}

    return mcp


# ---------------------------------------------------------------- self test

def selftest() -> int:
    """Integration test: really drives SOLIDWORKS on this machine.

    Destructive only to DISPOSABLE documents this test creates; it never closes
    unrelated documents. Run against a throwaway SOLIDWORKS session with no
    unsaved user work.
    """
    import pythoncom
    import win32com.client as win32
    pythoncom.CoInitialize()
    sw = win32.Dispatch("SldWorks.Application")
    sw.Visible = True
    os.makedirs(out_dir(), exist_ok=True)
    fails = []

    def check(label, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} {detail}", flush=True)
        if not cond:
            fails.append(label)

    print("== selftest ==", flush=True)

    state = {"length": 60.0, "width": 40.0, "thickness": 5.0, "holes": [],
             "mode": "plate", "name": "agent_part", "boxes": []}
    holes = [{"dia": 8, "x": 0, "y": 0}, {"dia": 4, "x": 20, "y": 10}]

    # --- isolation: a foreign document must never be closed by rebuild/close
    foreign = sw.NewDocument(default_template(), 0, 0, 0)
    try:
        foreign_title = foreign.GetTitle
    except Exception:
        foreign_title = None

    doc = _rebuild(sw, 60, 40, 5, holes, state=state)
    check("rebuild plate+2 holes", _body_count(doc) == 1, f"bodies={_body_count(doc)}")
    check("owned title tracked", bool(state.get("owned_title")),
          f"title={state.get('owned_title')}")

    _close_owned(sw)
    open_titles = [d.GetTitle for d in (_cv(sw, "GetDocuments") or [])]
    check("foreign doc not closed", foreign_title in open_titles, f"open={open_titles}")
    check("owned doc closed", doc.GetTitle not in open_titles, f"open={open_titles}")

    doc = _rebuild(sw, 60, 40, 5, holes, state=state)

    # --- atomic export + overwrite refusal
    stl = os.path.join(out_dir(), "selftest_plate.STL")
    if os.path.exists(stl):
        os.remove(stl)

    def _save_doc(tmp):
        doc.ClearSelection2(True)
        doc.SaveAs(tmp)

    _atomic_export(stl, _save_doc, overwrite=False)
    size = os.path.getsize(stl) if os.path.exists(stl) else 0
    stats = _stl_stats(stl) if size else {}
    check("STL exported", size > 1000, f"{size} bytes")
    check("STL has holes tessellated", stats.get("triangles", 0) > 20,
          f"triangles={stats.get('triangles')}")
    sz = stats.get("size_mm")
    if sz:
        check("STL size 60x40x5mm",
              all(abs(a - b) < 0.5 for a, b in zip(sz, [60, 40, 5])), f"{sz}")

    before = size
    try:
        _atomic_export(stl, _save_doc, overwrite=False)
        check("overwrite refused by default", False, "no FileExistsError raised")
    except FileExistsError:
        check("overwrite refused by default", os.path.getsize(stl) == before)

    part = os.path.join(out_dir(), "selftest_plate.SLDPRT")
    if os.path.exists(part):
        os.remove(part)
    _atomic_export(part, _save_doc, overwrite=False)
    check("SLDPRT saved", os.path.exists(part),
          f"{os.path.getsize(part) if os.path.exists(part) else 0} bytes")

    warns = print_warnings(thickness_mm=0.5, holes=[{"dia": 0.8}])
    check("advisory warns on thin/holey", len(warns) == 2, f"{warns}")

    _save_state({"length": 60.0, "width": 40.0, "thickness": 5.0, "holes": [],
                 "mode": "plate", "name": "agent_part", "boxes": []})
    loaded = _load_state()
    check("state file round-trip",
          loaded and loaded.get("length") == 60.0 and loaded.get("width") == 40.0
          and loaded.get("thickness") == 5.0 and loaded.get("holes") == [])

    print("== selftest", "PASSED" if not fails else f"FAILED ({fails})", "==", flush=True)
    return 0 if not fails else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    build_server().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
