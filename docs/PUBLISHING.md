# 发布 Runbook：solidworks-mcp 上架 DSH MCP 连接器市场

按顺序执行。前两步需要 GitHub / PyPI 凭据，第 3 步是向 registry 提交 PR。

## 第 1 步：把包推到 GitHub

```powershell
cd D:\03_Dev\Projects\devlep_practice\solidworks
git init
git add pyproject.toml README.md LICENSE .gitignore src tests connectors docs sw_mcp_server.py
git commit -m "solidworks-mcp: COM-driven SOLIDWORKS MCP server v0.1.0"
gh repo create wangruiyu070802/solidworks-mcp --public --source . --push
```

> 本机 gh 已登录 `wangruiyu070802`。若换账号，需同步修改 `pyproject.toml`
> 的 `[project.urls]` 与 `connectors/solidworks-cad.json` 的 `homepage`/`args`。

## 第 2 步：（可选）发布到 PyPI，简化启动命令

当前卡片命令为 `uvx --from https://github.com/wangruiyu070802/solidworks-mcp solidworks-mcp`，
GitHub 直装、可核验，无需 PyPI。发布 PyPI 后可把命令简化为 `uvx solidworks-mcp`：

```powershell
pip install build twine
python -m build
twine upload dist/*
```

> 需要 PyPI 账号（本机未配置凭据）。发布后提交一次 descriptor 更新 PR 即可。

## 第 3 步：向 registry 提交 Connector PR

1. Fork <https://github.com/duhu2000/dsh-mcp-connector-registry>
2. 复制卡片文件并开分支：

   ```powershell
   cd _registry   # 或重新 clone 你的 fork
   git checkout -b add-solidworks-cad
   Copy-Item ..\connectors\solidworks-cad.json connectors\solidworks-cad.json
   ```

3. 本地验证（需要 Node.js 24，本机已装 v24.20.0）：

   ```powershell
   npm ci --legacy-peer-deps
   npm test
   npm run validate
   npm run assets:check
   ```

   > 不要提交 `catalog.json`、`catalog-stats.json`、README 数量区块的差异，
   > 合并后 CI 会自动重建。

4. 提交 PR：

   ```powershell
   git add connectors/solidworks-cad.json
   git commit -m "Add solidworks-cad"
   git push origin add-solidworks-cad
   gh pr create --repo duhu2000/dsh-mcp-connector-registry --title "Add solidworks-cad" --body "见 PR 模板 checklist"
   ```

5. PR checklist 逐项确认（模板在 `.github/PULL_REQUEST_TEMPLATE.md`）：
   - 文件名 = `id` = `solidworks-cad`；`serverName` `solidworks` 全局唯一
   - `category` = 设计创意（9 标准分类之一）；`featured` = false
   - 无 Token/Key/密码/本机路径；stdio `command: uvx` 指向已发布 PyPI 包
   - Prompt 只做本地建模与状态检查，无付费/破坏性副作用
   - 图标用 emoji 📐，无远程图片跨域问题
6. 维护者复核后会补 `candidates/records/solidworks-cad.json`（人工批准 + 真实运行验收报告，我方不提交）。
7. 合并后：CI 重建目录 → jsDelivr 缓存清理 → 用户在 MCP连接器市场点「刷新」即可看到卡片。

## 上架后维护

- 改了 server：`pip install -e .` 验证 → bump 版本 → PyPI 发布 →（如影响用户侧配置）提交 descriptor 更新 PR。
- 改了工具签名或能力边界：更新 `connectors/solidworks-cad.json` 的 `description`，走同一更新 PR。
- 不要改 `id`/`serverName`（工具前缀 `mcp__solidworks__*` 依赖它稳定）。

## 本机已完成的验证证据（供维护者参考）

- 集成自测：`solidworks-mcp --selftest` 7/7 PASS（建模、双孔、STL 网格 324 三角面、60×40×5mm 包围盒、SLDPRT、护栏、状态持久化）
- 单测：`python -m unittest discover -s tests -v` 9/9 OK（纯函数，CI 可跑）
- DSH 端到端：`sw_plate → sw_hole → sw_check_printability → sw_export_stl → sw_open_in_bambu` 全部实测通过
- 环境：SOLIDWORKS 2024 SP0.1 (revision 32.0.1)、Bambu Studio 02.09.01.70、Python 3.13
