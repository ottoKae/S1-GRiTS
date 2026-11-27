# V3 to V4 migration / V3 至 V4 使用迁移

This guide describes the development candidate targeting V4.0.0. It does not
announce a published V4 package. Check the interpreter, imported package and
command help before using candidate-only commands. Installing a new version
does not rewrite, relocate or regenerate an existing Cube.

本文对应目标为 V4.0.0 的开发候选。升级软件不会自动修改历史 Cube，也不会把旧数据
标记成新算法的结果。本地待发布包版本为 4.0.0；命令可用性以实际安装内容为准。

## 1. Keep the old environment and verify the new one / 保留环境，核对入口

Keep the working V3 environment, YAML and dataset provenance. Prepare a separate
Python 3.12 environment using the [installation guide](installation.md); install
the reviewed candidate checkout or wheel there. Do not install an assumed
`s1grits==4.0.0` before publication. Optional backends have separate dependencies.

保留可工作的 V3 环境、原 YAML 和来源记录，按安装指南在独立 Python 3.12 环境安装
候选源码或 wheel。需要 Web／视图时选择相应的 web／views 可选依赖；
LandTrendr、CUSUM 和 GDAL 的安装条件见各自指南。既有中文 Web 保留，双语切换和
新增工具接入在 `feature/web-console-v4` 后续交付；本次新增能力使用 CLI／Python。

```sh
python -c "import s1grits; print(s1grits.__version__); print(s1grits.__file__)"
python -m s1grits --help
python -m s1grits derive_cube --help
```

For a new production job, use `init` → edit YAML → `doctor` → `process_scenes`.
For an existing Cube, start with `catalog inspect` and an explicit item ID.
See [entry points](entry_points.md) for exact commands and supported interfaces.

新建 Cube 使用生产 YAML；已有 Cube 可以直接读取和派生。Agent skill 负责组织调用，
不替代 YAML、不包含另一套计算算法；无需 Agent 也可以执行同一 CLI／Python 流程。

## 2. Inspect before continuing production / 续跑前核对

The production framework, default first-valid-pixel rule and date/orbit grouping
remain. A 30 m auto warp uses exact coordinate transformation plus nearest
neighbour; aligned grids retain direct-copy handling. The 10 m auto continuous
route remains bilinear. This is not a guarantee of bitwise equality with every
historical V3 output.

默认生产流程保持，但精确坐标变换属于已明确的正确性更新。过去使用近似变换的跨格网
结果不会因安装而改变。需要采用新映射时，将选定范围重新生成到独立目标，保留旧产品
及其处理证据；不要把新旧映射结果无说明地混成同一历史序列。

Before resuming, compare the original YAML and recorded grid, method and product
identity with the requested run. Same array dimensions alone are insufficient:
CRS, transform and coordinates must also agree. Monthly mean and median are
different processing definitions. A rejected resume requires inspecting the
actual mismatch, not editing metadata to bypass it.

续跑必须沿用相容的网格与处理约定。改变分辨率、镶嵌／滤波／月合成方法时，应选择新的
产品输出并保留父来源。无法从历史记录确认参数时，明确保留未知项，不凭当前默认值补造。
只有需要重新计算的产品才重建；普通读取和已有 Cube 分析不需要重新下载 burst。

## 3. Connect existing products / 接入已有产品

Select the exact product instance and, where present, Zarr group. One catalog row
can reference a whole store containing multiple timestamps; catalog row counts
do not establish per-pixel coverage. Check values, masks and the internal time
axis before analysis.

需要将已有资产接入新集合时，按[旧 Catalog 接入说明](entry_points.md#older-catalogs--旧-catalog-的接入)
使用 CLI 将经核验的完整产品记录登记到新集合，保留资产原位及正确的 source root。
不要手工虚构签名或改影像来满足字段要求。新实例、group 和虚拟视图通过 Resolver
读取；本次保留的旧 Web 不承诺支持这些新增表示。

Registration and a virtual view do not copy imagery. A view retains its source
dependencies; materialization explicitly writes a new product. Preserve parent
paths and provenance when moving or sharing outputs. The catalog supports
hierarchical metadata and provenance; this release does not claim every output
is strictly compliant with all STAC schemas and extensions.

## 4. Verify a small independent output / 先验证小输出

Use the [existing-Cube tutorial](getting_started_existing_cube_ZH.md) for a
deterministic 4-date, 4×5 sample with expected mean/median, Ratio/RVI and missing
month results. For real data, begin with a small window and an explicit time
range. Save the command, selected item, parameters and native receipt.

派生／视图物化／分析／格网镶嵌重放时使用新的独立输出路径；它们不是生产续跑接口。
检查数值、缺测、坐标、时间、观测数量和父身份。输出 generation、路径和时间戳可以不同，
不应据此认定科学结果不同。失败、样本不足和无拟合不能解释成零变化。

## 5. Know the analysis boundaries / 明确分析边界

The [support matrix](entry_points.md) is the canonical interface list. Local
LandTrendr remains experimental and is not numerically equivalent to GEE by
assumption. CUSUM requires its separately installed backend and its distribution
scope is limited to the adapter and instructions, without redistributing nrt.
Local CCDC is unavailable; GEE must be explicitly selected
and currently supports single-point segmentation only.

保留分析后端、版本、单位与参数。软件适配测试不代表 SAR 检测精度，也不代表跨平台、
在线 GEE 或大洲规模验收。若候选不满足当前任务，继续使用保留的 V3 环境及原产品；
不要让旧软件覆盖候选生成的新产品或元数据。
