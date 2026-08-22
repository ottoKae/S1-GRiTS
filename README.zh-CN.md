<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/logo-dark.png">
    <img src="assets/logo/logo.png" width="200" alt="S1-GRiTS 标志">
  </picture>
</p>

<p align="center">
  <em>面向智能体直接调用的 Sentinel-1 遥感时空数据立方体。</em>
  <br>
  <em>每个像素来源可追溯，成像几何不丢失。</em>
</p>
<p align="center"><a href="README.md">English</a> | <strong>简体中文</strong></p>
<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12+-blue.svg" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20NC-green.svg" /></a>
  <a href="https://github.com/ottoKae/S1-GRiTS"><img src="https://img.shields.io/badge/version-3.0.0-orange.svg" /></a>
</p>

---

<p align="center">
  S1-GRiTS（Sentinel-1 Gridded RTC Time Series）是一个 Python 软件包，用于从 ASF OPERA RTC-S1 产品构建可直接分析的 Sentinel-1 SAR 时序数据立方体。它将 burst 级观测转换为按 MGRS 对齐、时间一致的 Zarr 数据立方体，并可按需导出 COG。
</p>

## 供论文审稿人查阅：论文与代码对应关系

> 本仓库是下述论文的开源实现。审稿人可据此确认论文关键方法的代码实现，并复现论文中的图表。

**论文：** *Sentinel-1 Gridded Time Series (S1-GRiTS): Geometry-traceable SAR Data Cubes for decadal vegetation monitoring in cloud-prone regions* — Rao 等，2026（审稿中）。

**软件：** `s1grits` — [GitHub](https://github.com/ottoKae/S1-GRiTS) · [PyPI](https://pypi.org/project/s1grits/)（`pip install s1grits`）。

**验证区域：** 厄瓜多尔大陆，MGRS 瓦片 **17MPU**，2017—2025 年。

### 关键技术与实现位置

| 论文技术 | 代码实现 | 执行入口 |
|---|---|---|
| 按 `(mgrs_tile_id, acq_group_id_within_mgrs_tile, pass_id)`、`track_token` 进行 burst 优先的确定性成像分组 | `asf_tiles.py`、`dist_enum.py` | `s1grits process_scenes` |
| 首个有效像素镶嵌（来源控制，而非辐射融合） | `asf_output_writing.py` 的 `_mosaic_align()` | scenes 工作流及其 static 后处理阶段 |
| 升降轨分离，每个成像组对应一个 Zarr | `workflow_scenes.py`、`merge_acq_group_zarrs()` | `s1grits process_scenes` |
| 云原生 S3 流式读取、零落盘、内存虚拟文件转 float32 | `asf_io.py`、`rtc_s1_io.py` | `s1grits process_scenes` 及其 static 后处理阶段 |
| 自适应时间分批与内存受控并行 | `memory_manager.py` | YAML 的 `parallel`、`memory` 配置 |
| 月度中值合成及瓦片裁剪前 TV-Bregman 去斑 | `asf_io.py`、`asf_array_processing.py` | `process_scenes` 的 `processing.monthly` |
| 可增量追加的 Zarr、STAC 1.1.0 与 Parquet 目录 | `stac_builder.py`、`catalog_sync.py` | `s1grits catalog inspect` |
| 静态成像几何图层：LIA、入射角、叠掩/阴影、视数、ANF β0/σ0 | `workflow_static.py` | scenes YAML 或 `s1grits static ensure` |
| 升降轨后向散射差异及其与 LIA/ANF 的关系 | `manuscript_analysis_scripts/c05_*` | 对应论文脚本 |

### 动态数据与 static 的同目录存储及配对规则

动态后向散射立方体与静态成像几何图层属于同一逻辑数据立方体，共用一个 `output.base_dir` 和根目录下的 `catalog.parquet`。二者在每个 MGRS 瓦片目录中平级存储，既不分成两个独立数据档案，也不相互嵌套：

```text
{base_dir}/
  catalog.parquet
  17MNV/
    smonthly_ASCENDING/
      zarr/s1grits_smonthly_17MNV_ASCENDING_TK18.zarr
    static_ASCENDING/
      zarr/s1grits_static_17MNV_ASCENDING_TK18_Nxx.zarr
```

static 只有两个生产入口：在 `workflow_scenes` YAML 中启用后处理阶段，或对已有动态产品运行 `s1grits static ensure`。系统不支持独立 static YAML，以防静态数据选择错误的网格或成像几何。

RTC-STATIC 永远采用**原始值对齐**策略。六个源变量不进行去斑、空间滤波、归一化、时间合成，也不生成派生特征；只进行落到权威动态像素网格所必需的几何镶嵌与重投影。无论动态 scenes 是否使用空间滤波，此原则都不改变。

动态 `smonthly` 与 `static` 仅在共享轨道级键 `geometry_group_id = tile_id + flight_direction + track` 时配对。burst 数量只作为溯源信息，不属于连接键；多轨瓦片的每种成像几何分别对应一个 static 产品，禁止跨轨道混用。

生产时，static 必须以配对的 `scenes` 或 `smonthly` Zarr 为网格权威，继承其 CRS、仿射变换、形状、`x`/`y` 坐标和规范 `grid_id`。六个 static 变量只保存一次，为二维 `(y, x)`；动态后向散射为三维 `(time, y, x)`。`CubeResolver` 从同一个目录按瓦片、方向和轨道选择产品，并保持 static 为二维数组；只有具体空间窗口或模型批次提出请求时才进行惰性时间广播，不在数据档案中沿完整时间轴重复保存 static。

### 复现论文图表

`manuscript_analysis_scripts/` 保存论文使用的脚本：

| 脚本 | 对应结果 |
|---|---|
| `c01_f5a_gridded_composites_mosaics_ECU.py`、`c01_f5a_gridded_composites_17MPU.py` | 图 5a：厄瓜多尔镶嵌与 17MPU 合成影像 |
| `c04_f08_gridded_composites_mosaics_DEU.py` | 图 8：多区域扩展性 |
| `c05_f07_evaluation_cross_orbit_offsets_LIA_ANF.py` | 图 7：跨轨偏差空间分布与 LIA/ANF 热力图 |
| `c05_t03_evaluation_orbit_paris_offsets.py` | 表 3：升降轨与同轨偏差统计 |

**已发布数据（Zenodo，无需登录）：**

- 厄瓜多尔 2026 年 1 月降轨月度合成与镶嵌：[10.5281/zenodo.20607389](https://doi.org/10.5281/zenodo.20607389)
- 17MPU 升轨 2017—2025 年格网时序：[10.5281/zenodo.20589543](https://doi.org/10.5281/zenodo.20589543)
- 17MPU 降轨 2017—2025 年格网时序：[10.5281/zenodo.20607919](https://doi.org/10.5281/zenodo.20607919)
- 多区域扩展性合成产品：[10.5281/zenodo.20604391](https://doi.org/10.5281/zenodo.20604391)
- 像素级轨道配对统计与静态几何数据：[10.5281/zenodo.20607604](https://doi.org/10.5281/zenodo.20607604)

### 用三条命令复现主要结果

```bash
# 1. 生成论文研究瓦片的成像几何一致格网时序
s1grits process_scenes --config config/s1grits_scenes.yaml

# 2. 生成用于偏差分析的 static 成像几何图层
s1grits static ensure --output-dir ./output --product-label smonthly_ASCENDING

# 3. 计算跨轨偏差
python manuscript_analysis_scripts/c01_f5a_gridded_composites_17MPU.py
```

---

## 功能特点

S1-GRiTS 面向需要开展大范围、长时序 SAR 分析，同时希望避免重复处理原始数据的科研和业务用户。

**三类数据产品：**

- **月度合成数据**：多年、月尺度时序数据。
- **逐景数据**：适用于事件检测的高时间分辨率产品。
- **静态图层**：与动态数据逐像素对齐、时间不变的成像几何参考变量。

**核心能力：**

1. **Zarr 优先的数据立方体架构**：Zarr 是主要时序产品，新建 store 统一采用 Zstandard level 7 无损压缩；COG 和预览图为可选衍生输出。
2. **云原生 S3 流式处理**：从 ASF S3 直接读取，无需将源数据落盘。
3. **MGRS 格网对齐**：产品按原生 UTM 投影对齐至 100 km MGRS 瓦片。
4. **升降轨分离**：保证成像几何一致性。
5. **成像组策略**：按轨道、轨迹和帧组织 burst，保持时间一致性。
6. **标准化 Gamma0 辐射量**：基于经过辐射地形校正的 OPERA RTC-S1。
7. **双重斑点抑制**：时间中值合成与可选 TV-Bregman 空间滤波。
8. **时序增量更新**：新数据追加到 Zarr，无需重建历史数据。
9. **STAC 1.1.0 元数据**：配合 Parquet 目录实现快速检索。
10. **分析 API**：提供数据读取、时间序列提取、可视化和验证等模块。

**典型应用：** 长期形变监测、农作物分类、森林变化检测、洪涝灾害评估、土地利用与覆盖制图。

![MGRS 镶嵌示例](notebooks/S1-GRiTS-f1-exmaple.png)
**图 1：** 中国武汉地区的 burst 优先 MGRS 格网镶嵌。

![瓦片合成示例](notebooks/S1-GRiTS-f12-tile.jpg)
**图 2：** 厄瓜多尔大陆及其 17MPU 瓦片，展示去斑后的空间一致性和无接缝拼接效果。

![时间序列示例](notebooks/S1-GRiTS-f2-TS-exmaple.jpg)
**图 3：** 代表性地物在 2017—2025 年的 Sentinel-1 后向散射时间序列。

---

## 快速开始

```bash
# 1. 安装（Python 3.12，Linux/macOS/Windows 均有地理空间依赖包）
pip install s1grits

# 2. 可选：为 ASF 下载配置 Earthdata 认证
#    在 ~/.netrc 中填写 urs.earthdata.nasa.gov 凭据，详见 docs/installation.md

# 3. 复制配置模板并设置研究区和时间范围
cp config/s1grits_scenes.yaml my_run.yaml

# 4. 检查环境和配置，然后运行
s1grits doctor --config my_run.yaml
s1grits process_scenes --config my_run.yaml

# 5. 在网页界面浏览结果
s1grits serve --root /path/to/output
```

conda、源码安装、可选依赖、认证和首次运行说明见 **[安装文档](docs/installation.md)**。

## static 静态图层

static 是 scenes 或月度立方体中不随时间变化的配套数据。S1-GRiTS 按 `瓦片 + 轨道方向 + 轨道` 保存一次六个 OPERA RTC-STATIC 变量：局部入射角、入射角、叠掩/阴影掩膜、视数以及 β0/σ0 面积归一化因子。static 与动态产品共用完全一致的像素网格、瓦片目录、根 catalog 和 `geometry_group_id`。static 数值不进行去斑、空间滤波、归一化或时间合成，也不会沿时间轴重复存储。

新建任务时，在同一个 scenes YAML 中启用后处理：

```yaml
static_layers:
  run_after_scenes: true
  grid_reference: required
  reference_product_type: smonthly   # 逐景立方体改为 scenes
  on_failure: fail
```

随后照常运行：

```bash
s1grits process_scenes --config my_run.yaml
```

如果动态立方体已经存在，可在同一个根目录中补齐缺失的 static。`--product-label` 必须填写 `catalog.parquet` 中准确的动态产品标签；只有 catalog 缺失或过期时，才需要先执行 `catalog resync`。`static ensure` 支持幂等重跑：完整的配对产品会被跳过，只创建缺失的成像几何组。

```bash
# 仅当 catalog.parquet 缺失或过期时执行：
s1grits catalog resync --output-dir /path/to/output
s1grits static ensure --output-dir /path/to/output --product-label smonthly_ASCENDING
# 可选：使用 --tile 17MPU 限定瓦片；多个瓦片可重复传入 --tile
```

系统有意不支持独立 static YAML，因为 static 必须从权威动态产品取得成像几何和像素网格。详细规则见 **[static/scenes 对齐说明](docs/static_scenes_alignment.md)**。

## 文档

| 页面 | 内容 |
|---|---|
| [安装](docs/installation.md) | 环境要求、pip/conda/源码安装、Earthdata 认证与环境检查 |
| [架构](docs/architecture.md) | Zarr 优先架构、成像组与工作流比较 |
| [工作流](docs/workflows.md) | 月度合成、逐景处理和 static 图层 |
| [输出结构](docs/outputs.md) | Zarr/COG/预览图、波段、STAC 和 Parquet 目录 |
| [配置参考](docs/configuration.md) | YAML 配置项及默认值 |
| [目标格网与插值 v1](docs/target-grid-resampling-v1.md) | 固定 30/10 米格网与 10 米优化插值规范 |
| [命令行参考](docs/cli.md) | 处理、目录、瓦片、镶嵌、检查、缓存与服务命令 |
| [Python API](docs/python_api.md) | 数据读取、时间序列、绘图和验证 |
| [示例](docs/examples.md) | 端到端示例和教程 Notebook |
| [网页界面](docs/webapp.md) | `s1grits serve` 网页界面 |
| [内存受控架构](docs/scenes_blockwise_architecture.md) | 分块 scenes 流水线设计 |
| [常见问题](docs/faq.md) | 常见问题与故障排查 |
| [更新日志](CHANGELOG.md) | 版本变更记录 |

## 许可证与引用

### 许可证

版权所有 2026 KaeRao。

S1-GRiTS 采用双重许可：

- **非商业用途**（学术研究、教育、个人项目、政府或非营利研究、评估）：遵循 **[PolyForm Noncommercial License 1.0.0](LICENSE)**，保留署名后可用于非商业目的的使用、修改和再分发。
- **商业用途**（用于营利性产品、服务或运营）：需要单独取得商业许可，请阅读 [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md) 并联系作者。

许可证只规定分发和使用条件，不限制本地技术执行。v2.3.0 之前发布的版本仍按其原 Apache-2.0 条款提供给当时的接收者。

### 引用

在研究中使用 S1-GRiTS 时，请同时引用论文和软件。

**论文（审稿中）：**

```text
Rao, K., Lei, L., Dong, S., Alvarez, C. I., Zou, L., Hu, Z., & Wu, Z. (2026).
Sentinel-1 Gridded Time Series (S1-GRiTS): Geometry-traceable SAR Data Cubes
for decadal vegetation monitoring in cloud-prone regions. (under review).
```

**软件：**

```text
KaeRao. (2026). S1-GRiTS: Sentinel-1 Gridded RTC Time Series Data Cube (Version 3.0.0).
GitHub: https://github.com/ottoKae/S1-GRiTS
```

**BibTeX：**

```bibtex
@article{rao2026s1grits,
  author  = {Rao, Keyi and Lei, Lei and Dong, Shixin and Alvarez, Cesar Ivan
             and Zou, Linxin and Hu, Zhongwen and Wu, Zhaocong},
  title   = {Sentinel-1 Gridded Time Series (S1-GRiTS): Geometry-traceable SAR
             Data Cubes for decadal vegetation monitoring in cloud-prone regions},
  year    = {2026},
  note    = {under review}
}

@software{s1grits2026,
  author       = {KaeRao},
  title        = {S1-GRiTS: Sentinel-1 Gridded RTC Time Series Data Cube},
  year         = {2026},
  version      = {3.0.0},
  url          = {https://github.com/ottoKae/S1-GRiTS},
  note         = {A companion paper is under review}
}
```

## 致谢

**[@ottoKae](https://github.com/ottoKae)** 负责 S1-GRiTS 的整体设计与规划、真实数据测试和验证、最终用户可用性以及全部交付物的质量保证。

burst 到 MGRS 瓦片的枚举和空间滤波方法大量参考了 **OPERA/JPL** 的 [dist-s1-enumerator](https://github.com/opera-adt/dist-s1-enumerator) 项目，感谢其基础工作。

S1-GRiTS 建立在 NASA OPERA（Observational Products for End-Users from Remote Sensing Analysis）RTC-S1 产品之上，感谢 JPL OPERA 团队提供可直接分析的 SAR 数据。

代码优化和生产化实现得到 **[@claude](https://claude.ai)**（Anthropic）的协助。

## 参与贡献

S1-GRiTS 正在持续开发，欢迎通过 GitHub Issues 提交代码、错误报告和功能建议。

```bash
# 克隆仓库
git clone https://github.com/ottoKae/S1-GRiTS.git
cd S1-GRiTS

# 创建开发环境
conda env create -f environment.yml --solver=libmamba
conda activate py312_s1grits

# 以可编辑模式安装
pip install -e .
```

问题或功能建议请提交至：https://github.com/ottoKae/S1-GRiTS/issues

---

*README 最后更新：2026-08-11 | 版本 3.0.0*
