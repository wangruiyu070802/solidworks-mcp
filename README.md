# solidworks-mcp

通过 COM 驱动本机 **SOLIDWORKS** 做参数化建模，衔接 **Bambu Studio** 做 FDM 3D 打印的 MCP Server。

面向场景：在 DSH / Claude / Cursor 等 MCP 客户端里，用自然语言说「建一块 60×40×5 的板，中心开 8mm 孔，检查可打印性，导出 STL 并打开 Bambu Studio」，agent 调用本服务的工具完成建模、检查、导出与交付。

## 环境要求

- Windows 10/11 x64
- 本机安装并已授权 **SOLIDWORKS**（2024 已验证，COM 自动激活 `SldWorks.Application`）。SOLIDWORKS 是 Dassault Systèmes 的商业软件，需要你自己的有效许可——本插件不提供、也不绕过它。
- Python ≥ 3.10（由 `uvx` 自动提供即可）
- （可选）Bambu Studio：仅用于最后一步打开模型，人工切片打印。Bambu Studio 是独立第三方应用，遵循其自身发布许可。

## 安装（固定版本）

本包目前通过 git 源分发（未发布到 PyPI）。请固定到版本标签，避免引用移动分支：

```powershell
# uvx 直接运行（推荐；从固定 tag 安装，依赖自动解析）
uvx --from "https://github.com/wangruiyu070802/solidworks-mcp@v0.2.0" solidworks-mcp

# 或先安装成命令再启动
uv tool install "https://github.com/wangruiyu070802/solidworks-mcp@v0.2.0"
solidworks-mcp
```

> 安装过程需要联网下载依赖与源码；**运行阶段只在本机操作，不上传任何模型**。

启动（MCP stdio 模式）：

```powershell
solidworks-mcp
```

自测（会真实拉起 SOLIDWORKS 建模；**只新建/关闭本服务自己创建的临时文档，从不关闭你的其他文档**。请在无未保存工作的 SOLIDWORKS 会话里跑）：

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
      "args": ["--from", "https://github.com/wangruiyu070802/solidworks-mcp@v0.2.0", "solidworks-mcp"],
      "transport": "stdio"
    }
  }
}
```

Claude Desktop / Cursor 等价配置：`command: solidworks-mcp`（已 `uv tool install` 时）或 `uvx --from ...@v0.2.0 solidworks-mcp`。

## 工具一览

| 工具 | 作用 |
|------|------|
| `sw_status` | 连接体检：SW 版本、活动文档、实体数、模型参数（可能通过 COM 启动 SOLIDWORKS） |
| `sw_plate(length, width, thickness)` | 建平板（原点居中，+Z 拉伸），清空已有孔，重建并替换本服务自己的模型 |
| `sw_hole(diameter, x, y)` | 加通孔（相对板中心偏移），模型整体重建 |
| `sw_holes_clear` | 清空所有孔 |
| `sw_phone_stand(angle_deg, width_mm, depth_mm, channel_mm, support_z_mm)` | 一体打印手机支架（真实零件，见下） |
| `sw_part(name)` | 建 4 件套「贴地 V 叉 + 单脊柱」手机支架的任意一件 |
| `sw_check_printability` | 0.4 喷嘴打印可行性**建议**（壁厚/孔径/孔边距，返回 WARN 而非强制拦截） |
| `sw_save_part(path, overwrite)` | 保存 .SLDPRT（默认不覆盖，先写临时文件再原子替换） |
| `sw_export_stl(path, overwrite)` | 导出 STL（默认不覆盖，附网格统计与包围盒） |
| `sw_export_3mf(path, overwrite)` | 从 STL 重打包成规范 3MF（单位 mm、顶点去重），Bambu 可直接打开 |
| `sw_open_in_bambu(stl_path)` | 启动 Bambu Studio GUI 加载模型，人工切片/打印 |

所有长度单位均为毫米（mm）。

## 副作用与安全边界

本服务会真实操作本机 SolidWorks，调用前请了解这些副作用：

- **文档**：建模/加孔/换零件会重建并替换**本服务自己创建的模型文档**。服务通过标题跟踪自己创建的文档，`_close_owned` 只关闭这些文档——**从不枚举关闭你的其他文档**，因此不会丢失你未保存的工作。
- **状态与日志**：写 `model_state.json` 与 `server_debug.log` 到输出目录（默认 `<cwd>/out`）。
- **产物文件**：导出 STL/SLDPRT/3MF **默认不覆盖已存在文件**（需显式 `overwrite=true`），且先写临时文件再原子替换——导出失败不会破坏原文件。输出路径默认限定在输出目录内（设 `SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT=1` 才允许任意路径）。
- **GUI**：`sw_open_in_bambu` 会拉起 Bambu Studio 窗口。
- **SOLIDWORKS 启动**：`sw_status`、以及任何需要连接 COM 的工具，在 SolidWorks 未运行时可能通过 COM 激活启动它。
- **串行**：COM 会话不是线程安全的，工具调用需串行执行。

## 手机支架（`sw_phone_stand`）

第一个真实零件，也是「多块矩形拉伸」原语的样板：模型由一串
**立在底板上的竖直棱柱**拼成，重叠部分自动并成一个实体，
因此**零悬垂、零支撑、零装配**。

结构：

- **底板** 120×70×3，深度按倾角自动算（`depth_mm=0`）
- **前挡筋** 分左右两段，中间留 **22mm 充电通道** —— 线插着也能直接放进去，
  插头从通道落到板面、线往前出走
- **靠背棱**（默认 z=28、z=80 两片）：顶角**共线**于同一倾角，
  手机背面同时压在两条棱上（两条线定一个面，不是刀尖平衡）

关键结论（推过几何才确认，别绕回去）：**刚性一体件只能有一个倾角**。
手机背面那条线之后不能有任何材料，否则挡住 —— 支撑必须是那个方向上最外侧
的表面，角度就被钉死了。要多角度只能换参数重打一个，或者上铰链/卡齿结构
（那就不是「零装配」了）。

产物示例：`out/phone_stand_30deg.{3mf,STL,SLDPRT}`、
`out/phone_stand_45deg.{3mf,STL,SLDPRT}`（30° 看视频 / 45° 躺床上仰看），
`out/phone_stand_designs.html`（8 种支架设计的可视化对比与取舍分析）。

## 打印可行性建议（0.4 喷嘴）

- 最小壁厚 **0.8mm**（2 道外墙）
- 最小孔径 **1.2mm**（更小会缩孔，建议打印后钻孔）
- 孔距板边最小 **0.8mm**（否则破边）
- 配合间隙建议 **0.15mm/侧**

`sw_check_printability` 在建模阶段就返回 `WARN`（建议性告警），而不是等切片失败。
**这些是启发式建议，不是强制执行，PASS 也不构成制造安全保证**——最终切片、
材料与打印参数请以 Bambu Studio 与你的打印机实测为准。

## 环境变量（可选）

| 变量 | 作用 |
|------|------|
| `SOLIDWORKS_TEMPLATE` | .prtdot 零件模板路径（默认自动探测，含中文版 `gb_part.prtdot`） |
| `BAMBU_EXE` | Bambu Studio 可执行文件路径（默认探测常见安装位置） |
| `SOLIDWORKS_OUT_DIR` | 产物目录（默认 `<cwd>/out`） |
| `SOLIDWORKS_ALLOW_EXTERNAL_OUTPUT` | 设为 `1` 才允许把 STL/SLDPRT/3MF 写到输出目录之外 |

## 许可与第三方依赖

- 本插件：**MIT**（见 `LICENSE`）。
- **SOLIDWORKS**：Dassault Systèmes 的商业软件，运行它需要你自己的有效许可。
- **Bambu Studio**：独立第三方应用，遵循其自身发布许可；仅由 `sw_open_in_bambu` 拉起。
- 「免费」仅指本插件的 MIT 授权，**不含** SOLIDWORKS 授权与 Bambu Studio。

## 已知限制

1. **Bambu Studio 命令行切片**：部分版本（实测 02.09.01.70）二进制无 CLI 参数字符串，调用即静默退出。已降级为「自动打开 GUI + 人工点切片/打印」。
2. **工具调用需串行**：并行调用会争用 COM 会话导致失败。
3. **仅 Windows**：依赖 SOLIDWORKS COM。
4. **文档所有权按标题跟踪**：服务通过文档标题（如 `Part1`）识别自己创建的文档；若跨进程重启后 SOLIDWORKS 复用了相同标题给其他文档，存在极小概率的误判，请勿在关键未保存工作旁运行。

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
