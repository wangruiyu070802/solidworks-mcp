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
import os
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


# ---------------------------------------------------------------- COM helpers

def _cv(obj, name):
    """Read a member that may surface as a property or a method wrapper."""
    v = getattr(obj, name)
    if type(v).__name__ in ("_MethodImplementation", "MethodImplementation"):
        return v()
    return v


def _close_all(sw):
    try:
        for d in (_cv(sw, "GetDocuments") or []):
            try:
                sw.CloseDoc(d.GetTitle)
            except Exception:
                pass
    except Exception:
        pass


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
    off, pts = 84, []
    for _ in range(n):
        vals = struct.unpack("<12f", data[off:off + 48])
        pts.extend([(vals[3], vals[4], vals[5]), (vals[6], vals[7], vals[8]),
                    (vals[9], vals[10], vals[11])])
        off += 50
    if not pts:
        return {"triangles": n}
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    scale = 1000.0 if span < 1.0 else 1.0
    return {
        "triangles": n,
        "unit_detected": "m" if scale == 1000.0 else "mm",
        "bbox_mm": {
            "x": [round(min(xs) * scale, 2), round(max(xs) * scale, 2)],
            "y": [round(min(ys) * scale, 2), round(max(ys) * scale, 2)],
            "z": [round(min(zs) * scale, 2), round(max(zs) * scale, 2)],
        },
        "size_mm": [round((max(xs) - min(xs)) * scale, 2),
                    round((max(ys) - min(ys)) * scale, 2),
                    round((max(zs) - min(zs)) * scale, 2)],
    }


# ---------------------------------------------------------------- state file

def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if all(k in d for k in ("length", "width", "thickness")):
            return d
    except Exception:
        pass
    return None


def _save_state(S):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"length": S["length"], "width": S["width"],
                       "thickness": S["thickness"], "holes": S["holes"]},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log("save_state failed:", e)


# ---------------------------------------------------------------- print rules

def print_warnings(thickness_mm=None, holes=None):
    """Hard guardrails for a 0.4mm-nozzle FDM printer. Pure function (unit-testable)."""
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

def _rebuild(sw, length, width, thickness, holes, template=None):
    """Fresh part from parameters: one sketch (rect + circles), one extrusion."""
    _close_all(sw)
    doc = sw.NewDocument(template or default_template(), 0, 0, 0)
    if doc is None:
        raise RuntimeError("NewDocument 返回 None（模板或许可问题）")

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


def _ensure_doc(sw, S):
    """Attach to SOLIDWORKS's active part; rebuild from saved params if needed."""
    doc = _active_part(sw)
    if doc is not None:
        return doc, False
    if S.get("length") is None:
        return None, False
    try:
        doc = _rebuild(sw, S["length"], S["width"], S["thickness"], S["holes"])
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


# ---------------------------------------------------------------- MCP server

def build_server():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(
        "solidworks",
        title="SolidWorks 参数化建模",
        description="通过 COM 驱动本机 SOLIDWORKS：参数化平板、孔、导出 STL、打印可行性护栏、交给 Bambu Studio。",
    )

    S = _load_state() or {"length": 60.0, "width": 40.0, "thickness": 5.0, "holes": []}
    _log("server starting, state:", S)

    def _holes_with_clearance():
        return [dict(h, **_hole_clearance(h, S["length"], S["width"])) for h in S["holes"]]

    @mcp.tool()
    def sw_status() -> dict:
        """检查 SolidWorks 连接状态：是否在运行、当前文档、实体数量。"""
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
                "model_params": {"length": S["length"], "width": S["width"],
                                 "thickness": S["thickness"], "holes": S["holes"]},
            }

    @mcp.tool()
    def sw_plate(length_mm: float = 60, width_mm: float = 40, thickness_mm: float = 5) -> dict:
        """建一块长方形平板（原点居中，厚度沿 +Z 拉伸）。会重建模型并清空已有孔。"""
        with _com_lock:
            S.update({"length": length_mm, "width": width_mm,
                      "thickness": thickness_mm, "holes": []})
            try:
                sw = _connect()
                doc = _rebuild(sw, S["length"], S["width"], S["thickness"], S["holes"])
            except Exception as e:
                _log("sw_plate:", traceback.format_exc())
                return {"ok": False, "error": str(e)}
            _save_state(S)
            return {
                "ok": True,
                "solid_bodies": _body_count(doc),
                "dimensions": {"length": length_mm, "width": width_mm, "thickness": thickness_mm},
                "warnings": print_warnings(thickness_mm=thickness_mm),
            }

    @mcp.tool()
    def sw_hole(diameter_mm: float = 8, x_mm: float = 0, y_mm: float = 0) -> dict:
        """在平板上加一个通孔（孔心相对板中心偏移 x/y）。模型会整体重建。"""
        with _com_lock:
            holes = S["holes"] + [{"dia": diameter_mm, "x": x_mm, "y": y_mm}]
            try:
                sw = _connect()
                doc = _rebuild(sw, S["length"], S["width"], S["thickness"], holes)
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

    @mcp.tool()
    def sw_holes_clear() -> dict:
        """清空所有孔并重建模型。"""
        with _com_lock:
            S["holes"] = []
            try:
                sw = _connect()
                _rebuild(sw, S["length"], S["width"], S["thickness"], [])
            except Exception as e:
                _log("sw_holes_clear:", traceback.format_exc())
                return {"ok": False, "error": str(e)}
            _save_state(S)
            return {"ok": True, "holes": []}

    @mcp.tool()
    def sw_check_printability() -> dict:
        """检查当前模型是否适合 0.4 喷嘴 FDM 打印，返回硬护栏告警。"""
        holes = _holes_with_clearance()
        warns = print_warnings(thickness_mm=S["thickness"], holes=holes)
        return {
            "nozzle_mm": NOZZLE_MM,
            "rules": {"min_wall_mm": MIN_WALL_MM, "min_hole_mm": MIN_HOLE_MM,
                      "clearance_mm": CLEARANCE_MM},
            "dimensions": {"length": S["length"], "width": S["width"],
                           "thickness": S["thickness"], "holes": holes},
            "warnings": warns,
            "verdict": "PASS" if not warns else "WARN",
        }

    @mcp.tool()
    def sw_save_part(path: str = "") -> dict:
        """保存 .SLDPRT 到磁盘（默认 <out>/agent_part.SLDPRT）。"""
        with _com_lock:
            sw = _connect()
            doc, _ = _ensure_doc(sw, S)
            if doc is None:
                return {"ok": False, "error": "没有模型，请先 sw_plate"}
            path = path or os.path.join(out_dir(), "agent_part.SLDPRT")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            doc.ClearSelection2(True)
            ok = doc.SaveAs(path)
            return {"ok": bool(ok) and os.path.exists(path), "path": path,
                    "bytes": os.path.getsize(path) if os.path.exists(path) else 0}

    @mcp.tool()
    def sw_export_stl(path: str = "") -> dict:
        """导出 STL（默认 <out>/agent_part.STL）。导出前清空选择，避免只导出选中面。"""
        try:
            return _sw_export_stl_impl(path)
        except Exception as e:
            _log("sw_export_stl:", traceback.format_exc())
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _sw_export_stl_impl(path: str = "") -> dict:
        with _com_lock:
            sw = _connect()
            doc, rebuilt = _ensure_doc(sw, S)
            if doc is None:
                return {"ok": False, "error": "没有模型，请先 sw_plate"}
            path = path or os.path.join(out_dir(), "agent_part.STL")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            if os.path.exists(path):
                os.remove(path)
            doc.ClearSelection2(True)
            ok = doc.SaveAs(path)
            if not os.path.exists(path):
                return {"ok": False, "error": f"导出失败 ret={ok}"}
            stats = _stl_stats(path)
            return {"ok": True, "path": path, "bytes": os.path.getsize(path),
                    "mesh": stats, "rebuilt": rebuilt,
                    "verdict": "OK" if stats.get("triangles", 0) > 0 else "EMPTY"}

    @mcp.tool()
    def sw_open_in_bambu(stl_path: str = "") -> dict:
        """把 STL 交给 Bambu Studio 界面打开，供人工切片与打印。

        部分 Bambu Studio 版本（如 02.09.01.70）无命令行切片能力，因此采用
        「自动打开 GUI + 人工点切片/打印」的半自动方案。
        """
        import subprocess
        stl_path = stl_path or os.path.join(out_dir(), "agent_part.STL")
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
    """Integration test: really drives SOLIDWORKS on this machine."""
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

    holes = [{"dia": 8, "x": 0, "y": 0}, {"dia": 4, "x": 20, "y": 10}]
    doc = _rebuild(sw, 60, 40, 5, holes)
    check("rebuild plate+2 holes", _body_count(doc) == 1, f"bodies={_body_count(doc)}")

    stl = os.path.join(out_dir(), "selftest_plate.STL")
    if os.path.exists(stl):
        os.remove(stl)
    doc.ClearSelection2(True)
    doc.SaveAs(stl)
    size = os.path.getsize(stl) if os.path.exists(stl) else 0
    stats = _stl_stats(stl) if size else {}
    check("STL exported", size > 1000, f"{size} bytes")
    check("STL has holes tessellated", stats.get("triangles", 0) > 20,
          f"triangles={stats.get('triangles')}")
    sz = stats.get("size_mm")
    if sz:
        check("STL size 60x40x5mm",
              all(abs(a - b) < 0.5 for a, b in zip(sz, [60, 40, 5])), f"{sz}")

    part = os.path.join(out_dir(), "selftest_plate.SLDPRT")
    doc.ClearSelection2(True)
    doc.SaveAs(part)
    check("SLDPRT saved", os.path.exists(part),
          f"{os.path.getsize(part) if os.path.exists(part) else 0} bytes")

    warns = print_warnings(thickness_mm=0.5, holes=[{"dia": 0.8}])
    check("guardrails warn on thin/holey", len(warns) == 2, f"{warns}")

    S = {"length": 60.0, "width": 40.0, "thickness": 5.0, "holes": []}
    _save_state(S)
    check("state file round-trip", _load_state() == S)

    print("== selftest", "PASSED" if not fails else f"FAILED ({fails})", "==", flush=True)
    return 0 if not fails else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    build_server().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
