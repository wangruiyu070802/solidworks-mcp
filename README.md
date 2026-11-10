# solidworks-mcp

通过 COM 驱动本机 **SOLIDWORKS** 做参数化建模，衔接 **Bambu Studio** 做 FDM 3D 打印的 MCP Server。

面向场景：在 DSH / Claude / Cursor 等 MCP 客户端里，用自然语言说「建一块 60×40×5 的板，中心开 8mm 孔，检查可打印性，导出 STL 并打开 Bambu Studio」，agent 调用本服务的工具完成建模、护栏检查、导出与交付。

## 环境要求

- Windows 10/11 x64
- 本机安装 SOLIDWORKS（2024 已验证，COM 自动激活 `SldWorks.Application`）
- Python ≥ 3.10
- （可选）Bambu Studio：仅用于最后一步打开模型，人工切片打印

## 安装

```powershell
# 方式一：uv（推荐，免手动装依赖）
uv tool install solidworks-mcp

# 方式二：pipx / pip
pip install solidworks-mcp
```

启动（MCP stdio 模式）：

```powershell
solidworks-mcp
```

自测（会真实拉起 SOLIDWORKS 建模，非破坏性）：

```powershell
solidworks-mcp --selftest
```

## 接入 MCP 客户端

DSH「MCP连接器」市场中搜 `solidworks` 一键连接（上架后），或手动配置 stdio：

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "uvx",
      "args": ["solidworks-mcp"],
      "transport": "stdio"
    }
  }
}
```

Claude Desktop / Cursor 等价配置：`command: solidworks-mcp`（已 `pip install` 时）或 `uvx solidworks-mcp`。

## 工具一览

| 工具 | 作用 |
|------|------|
| `sw_status` | 连接体检：SW 版本、活动文档、实体数、模型参数 |
| `sw_plate(length, width, thickness)` | 建平板（原点居中，+Z 拉伸），清空已有孔 |
| `sw_hole(diameter, x, y)` | 加通孔（相对板中心偏移），模型整体重建 |
| `sw_holes_clear` | 清空所有孔 |
| `sw_check_printability` | 0.4 喷嘴硬护栏检查（壁厚/孔径/孔边距） |
| `sw_save_part` | 保存 .SLDPRT |
| `sw_export_stl` | 导出 STL（自动清空选择，附网格统计与包围盒） |
| `sw_open_in_bambu` | 启动 Bambu Studio 加载 STL，人工切片/打印 |

所有长度单位均为毫米（mm）。

## 打印可行性护栏（0.4 喷嘴）

- 最小壁厚 **0.8mm**（2 道外墙）
- 最小孔径 **1.2mm**（更小会缩孔，建议打印后钻孔）
- 孔距板边最小 **0.8mm**（否则破边）
- 配合间隙建议 **0.15mm/侧**

`sw_check_printability` 在建模阶段就返回 `WARN`，而不是等切片失败。

## 环境变量（可选）

| 变量 | 作用 |
|------|------|
| `SOLIDWORKS_TEMPLATE` | .prtdot 零件模板路径（默认自动探测，含中文版 `gb_part.prtdot`） |
| `BAMBU_EXE` | Bambu Studio 可执行文件路径（默认探测常见安装位置） |
| `SOLIDWORKS_OUT_DIR` | 产物目录（默认 `<cwd>/out`） |

## 已知限制

1. **Bambu Studio 命令行切片**：部分版本（实测 02.09.01.70）二进制无 CLI 参数字符串，调用即静默退出。已降级为「自动打开 GUI + 人工点切片/打印」。
2. **工具调用需串行**：并行调用会争用 COM 会话导致失败。
3. **仅平板+孔特征**：当前参数空间为「矩形板 + 任意多个圆孔」；更复杂的特征（槽、倒角、异形轮廓）按需扩展。
4. **仅 Windows**：依赖 SOLIDWORKS COM。

## 本机 COM 关键经验（开发踩坑记录）

1. 该构建下 `GetFeatureCount`/`FirstFeature`/`GetTypeName2` 等成员全部以**属性**暴露，不能加括号调用
2. `SelectByID2` 晚绑定必报类型不匹配 → 一律特征树遍历 + `Feature.Select2`
3. 拉伸前**必须先选中草图**，否则 `FeatureExtrusion2` 返回 None
4. `FeatureCut4` 任何参数个数都报 `DISP_E_PARAMNOTOPTIONAL` → 方案改为「矩形+圆同一草图一次拉伸」，绕开切除
5. 保存/导出只能用 `ModelDoc2.SaveAs(path)`
6. 导出 STL 前必须 `ClearSelection2(True)`，否则只导出选中面

## 开发

```powershell
pip install -e .[dev]
python -m unittest discover -s tests -v   # 纯函数单测，不依赖 SOLIDWORKS
solidworks-mcp --selftest                 # 集成自测，真实操作 SOLIDWORKS
```

## 发布

上架 DSH MCP 连接器市场：见 `docs/PUBLISHING.md`，卡片 Descriptor 见 `connectors/solidworks-cad.json`。

## License

MIT
