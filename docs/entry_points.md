# Entry points and support / 使用入口与支持范围

This page describes the development candidate, not an already published V4.0.0.
Check `python -m s1grits --version`, `s1grits.__file__` and the relevant command's
`--help` in your environment. All entries use the same computational implementation.

本文对应开发候选，不能据此认定 PyPI 已发布 V4.0.0。先核对版本、实际导入位置和命令
帮助。CLI／Python 执行计算；Agent 根据通用 skill 选择输入、调用已有命令并检查结果。
本次保留既有中文 Web；双语控制台、新工具接入和 Web 修复保存在 `feature/web-console-v4`，
后续审阅后再合并发布。**YAML 保存生产配置，skill 是操作说明，两者不是替代关系。**

Upgrading an existing installation? Start with [migration and compatibility](migration_v4.md).
已有 V3 环境或历史 Cube，请先阅读迁移说明；安装新软件不会自动改写历史产品。

## Choose a starting route / 选择起点

| Your situation / 当前情况 | Start here / 推荐入口 | Keep / 需要保留 |
| --- | --- | --- |
| New Cube / 没有 Cube | `init` → 修改 YAML → `doctor` → `process_scenes`；Agent 用 `s1grits-cube`；原有图形流程见[Web指南](webapp.md) | 最终 YAML、环境、输出根及日志 |
| Existing Cube / 已有 Cube | [Small local tutorial](getting_started_existing_cube_ZH.md)；CLI/Resolver选择明确实例；Agent 用 `s1grits-analysis` | item ID、参数、窗口／时段、命令及原生来源回执 |
| Repeat a task / 需要重放或自动化 | 使用同一解释器运行已保存的 CLI／Python；生产读取 YAML | 输入及依赖位置、科学参数、执行记录；新派生使用新输出目录 |

No Agent is required. The [Agent workflow guide](agent_workflows_ZH.md) explains the
two generic skills. Generated [skill exports](../agents/skills/README.md) are included
in this checkout and the source distribution, separately from the Python wheel.
Point your Agent to the matching skill file, or to that guide and command help;
named skills are optional. The retained Chinese Web is launched with the Web extra
and `python -m s1grits serve <workspace>`; it does not expose the new V4 tools.
CLI/Python views additionally need `s1grits[views]`. Optional temporal backends and
GDAL are described in their own guides.

没有 Agent 也能使用全部 CLI／Python 功能。通过 Resolver 可以直接读取虚拟产品；
实体专用分析需要先明确物化，再启动分析。普通读取不会自动重建或下载 Cube。
旧 Web 不承诺支持新实例、group 或虚拟产品表示。

## Quick invocation and explicit contracts / 快速调用与明确接口

For an existing Cube, select its item ID and start with a small window. Replace
`cube`, `ITEM_ID` and `derived-small` with your input, selected instance and new output:

```sh
python -m s1grits catalog inspect --output-dir cube
python -m s1grits derive_cube --catalog-root cube --item-id ITEM_ID --output derived-small --start 2026-01-01 --end 2026-03-31T23:59:59Z --window 0 0 4 4 --features ratio rvi --filter none
```

The example reads a 4×4 spatial window for January–March 2026. Both `--start` and
`--end` are required; replace the dates with your selected UTC interval. It writes an independent Cube and
`derivation.json`. It does not download or rebuild the source. The
[runnable tutorial](getting_started_existing_cube_ZH.md) supplies a tiny generated
input, item ID and expected numerical results for a first run.

三个使用约定适用于所有入口：

- **快速调用**：已有数据直接选择实例并执行工具；生产使用 YAML，分析使用对应 CLI 参数。
  用 `python -m s1grits <command> --help` 查看当前安装版本的接口；保存的 CLI／Agent 命令可以重放。
- **接口明确**：给出 Catalog 根、item ID、窗口／时段和独立输出位置。窗口顺序为
  `x_offset y_offset width height`；输入单位、计算域和可选组合以对应命令指南为准。
  派生 Cube、虚拟定义和分析结果文件是不同输出，不能互相冒充。
- **功能明确**：先看下表的支持范围。实验后端保留名称和版本，本地未实现的功能明确不可用；
  不因缺依赖隐式换算法、重建数据或调用云端。完成后检查状态、来源和数值，失败或样本不足不代表零变化。

## Support matrix / 支持矩阵

“Agent” means invoking the listed CLI/API using matching documentation, not a separate
backend. “Web” below means the retained Chinese interface. New tool forms and
bilingual switching are deferred to `feature/web-console-v4`; CLI availability
does not imply a matching form control.

| Capability / 能力 | CLI or Python entry | Agent skill | Retained Web / 既有页面 | Boundary / 主要边界 |
| --- | --- | --- | --- | --- |
| Scenes / 逐景生产 | `process_scenes --config` | `s1grits-cube` | Yes / 支持 | 生产 YAML；保留当前默认策略 |
| Monthly / 月合成 | 同一生产入口；`processing.monthly` | `s1grits-cube` | 既有生产表单 | 逐轨合成；不是跨轨融合；新增策略使用 YAML/CLI |
| Static / 静态图层 | 生产 Static post-stage | `s1grits-cube` | 随动态产品启用 | Web 不单独启动 Static |
| Intra-pass / 同次获取 burst 拼接 | [first_valid_pixel / average](intra_pass_average.md)、[bursts_center](intra_pass_center.md)、[nlooks_weighted](intra_pass_looks.md) | `s1grits-cube` | 既有默认策略；新增选择暂缓 | 可选方法沿用已有 30 m、无滤波／月合成／Static 等组合限制；nlooks 需匹配权重资产 |
| Inspect / 精确实例读取 | Catalog + `CubeResolver` | 两种 skill | 既有目录浏览；新实例/group/view接入暂缓 | CLI/Resolver不默认选第一条；原生 group／view locator |
| Register / 显式登记已有资产 | `catalog register` | `s1grits-cube` | 无登记表单 | CLI 写集合元数据；不复制影像 |
| Features / 特征与滤波 | [`derive_cube`](cube_derivatives.md) | `s1grits-analysis` | 暂缓接入 | 已有实体、新输出；操作组合须满足 CLI 约束 |
| Monthly derivative / 派生月产品 | `derive_cube --monthly` | `s1grits-analysis` | 暂缓接入 | 明确单轨 scenes 父代；已有月产品不能当 scenes 重合成 |
| Interpolation / 时间插值 | [`derive_cube --interpolation`](temporal_interpolation.md) | `s1grits-analysis` | 暂缓接入 | 单独操作；显式计算域、最大间隔；不外推，不把插值当新观测 |
| Virtual selection/features / 虚拟选择与特征 | [`view create`](cube_views.md) | `s1grits-analysis` | 暂缓接入 | 时间／窗口及 VV/VH/Ratio/RVI；仅保存定义，依赖父数据 |
| Materialize / 显式物化 | `view materialize` | `s1grits-analysis` | 暂缓接入 | 单独操作，写新实体；无隐式物化 |
| Multi-track monthly / 多轨月组合 | [`view multitrack`](multitrack_monthly.md) | `s1grits-analysis` | 暂缓接入 | 同格网／方向及统计范围兼容的单轨派生月实体；贡献轨道数不同于 n_obs |
| Local LandTrendr | [`analyze_segments`, `analyze_segments_batch`](segmentation.md) | `s1grits-analysis` | 暂缓接入 | 实验 LT-rs 0.4.0；明确年度方法／域／方向 |
| Public CUSUM | [`analyze_cusum`](cusum.md) | `s1grits-analysis` | 不开放 | 分发适配器与说明；用户另装 nrt，保持实验状态；不捆绑后端 |
| Local CCDC | 尚无本地实现 | 明确不可用 | 不开放 | 不将其他算法改名为 CCDC |
| GEE segmentation | 显式 `--backend earthengine` 的单点入口 | 需要明确远程执行授权 | 不开放 | 不因本地缺依赖自动发送数据至 GEE；批次入口仅本地 |
| Inter-tile grid mosaic / 邻格镶嵌 | [`mosaic_grid`](grid_mosaic.md) | 按同一 CLI／指南执行 | 暂缓接入 | 明确有序 COG、目标网格；同月或同次获取；精确最近邻，逐波段首有效 |
| Legacy mosaic / 旧镶嵌入口 | `mosaic`, `mosaic_scenes` | 按匹配版本帮助执行 | 无独立表单 | 不能将旧入口的选择规则当成 `mosaic_grid` 的规则 |

Intra-pass mosaicking, multi-track monthly combination and inter-tile grid mosaicking
are different operations. Their valid inputs, missing-data rules and provenance are
not interchangeable. The [Web guide](webapp.md) describes form-level limits; CLI-only
capabilities remain CLI-only. The [backend assessment](local_backends.md) records
experimental and unavailable implementations without implying scientific equivalence.

## Older catalogs / 旧 Catalog 的接入

A version label alone does not establish the fields or product identity present
in an old catalog. Inspect it through Resolver before analysis. Explicit registration
can connect verified existing assets to a new collection without rewriting imagery.

保留源不变，将**已核验的一个完整产品记录**导出为 JSON，使用已有登记入口建立
一个新的集合。`--source-root` 保持源记录中相对资产路径的解释位置：

```sh
python -m s1grits catalog register --output-dir new-collection --record selected-record.json --source-root original-cube
python -m s1grits catalog inspect --output-dir new-collection
```

`selected-record.json` 应来自明确选定的源 Catalog 记录，包含 item ID、处理签名、格网及
资产定位；不能凭空补造身份。登记会检查资产／元数据一致性；不一致时应核对真实来源，
而不是修改影像以消除错误。通过 Resolver 读取新集合；不要据此假定旧 Web 支持
新的实例/group/view。详见[集合登记](workflows.md)。

## Reproduce a CLI or Agent task / 保存和重放

Keep the production YAML or the exact CLI/API arguments, selected item IDs,
any point/input-list files, and execution logs. Preserve native `derivation.json`,
registered view definitions, analysis/batch receipts or mosaic `.vrt.json` / `.tif.json`,
including their source references and VRT dependencies. Existing Web task records
follow the retained [Web guide](webapp.md); new tool-job forms are deferred.

生产可在核对兼容性后使用同一 YAML 续跑。派生／视图／分析／镶嵌不支持在已存在的
输出上自动续写；重放时修改为新的独立目标路径，保留同一输入和科学参数。按参数列表
调用 CLI，不把 JSON 数组直接当作 shell 文本。Agent 同样应交付这些文件和确切命令。

Check values, masks, coordinates, parent identity and scientific signature. Independent
outputs may have different generation IDs, paths and timestamps. Partial or failed batches,
insufficient observations and no fit are distinct from zero change. A Catalog row count
does not establish per-pixel temporal coverage.

Only local small-sample and offline integration checks are claimed here. The public Web
does not add continental scheduling or prove temporal detection accuracy. Remote CI,
online download and GEE execution require separate evidence; they are not implied by a
successful local page or command.

<a id="repository-layout"></a>

## Repository layout / 仓库目录职责

Source layout and output layout serve different purposes. See [output structure](outputs.md)
for a delivered Cube and [runtime call paths](call_flow.md) for how the interfaces share code.

| Directory / 目录 | Responsibility / 职责 |
| --- | --- |
| `src/s1grits/` | Installed runtime code, CLI and packaged Web assets / 正式运行代码与打包资源 |
| `src/s1grits/data/` | Bundled lookup tables required by the package / 程序必需查找表，不是研究数据目录 |
| `tests/`, `benchmarks/` | Tracked regression tests and benchmark helpers, including helpers used by tests / 可复用验证代码，不能整体忽略 |
| `config/` | Public example configurations / 可公开模板；实际运行配置与输出、日志共同保留 |
| `docs/` | Public usage, API and maintenance guides / 使用、接口和维护说明 |
| `scripts/`, `tools/` | Standalone audit/maintenance utilities and development/release helpers / 辅助入口，不由运行包导入 |
| `agents/skills/` | Generated Cube/analysis skill exports and the public handoff reference / 通用 skills 的生成发布副本与交接说明；维护规范源保持唯一 |
| `notebooks/`, `assets/` | Tutorials and referenced illustrations / 教程与文档引用素材 |
| `.github/` | CI and release workflows / 持续集成及发布流程 |

`docs/_work/`, `tooling/diagnostics/`, `.test-tmp/`, local environments and Web job
ledgers stay outside Git. Keep disposable data in a dedicated test run directory;
retain acceptance evidence separately. Manuscripts and research datasets do not belong
in Python distributions. An ignored path is not permission to delete its contents.

本地忽略目录不代表可随意删除：真实验收证据、运行配置与论文数据需要独立保留。
公开文档不依赖 `docs/_work/`，测试源码不随临时测试数据一起清理。
