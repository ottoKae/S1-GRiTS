# 阶段 0：S1-GRiTS v3 Web/CLI/Catalog 契约

状态：通过（2026-08-22）  
适用版本：S1-GRiTS 3.0.0  
Catalog：Schema v8（46 个规范字段）

本文档替代旧 `S1-GRiTS-V100` 工作区中的 Schema v6 阶段 0 记录。旧记录仅作为迁移审计材料，不再是生产契约。

## 1. 唯一项目与运行边界

- 唯一代码库：`data_pipeline/S1-GRiTS`。
- Web 服务入口：`s1grits serve <workspace>`。
- `<workspace>` 同时是目录浏览的安全根和所有 Web 任务允许写入的根。
- 页面只能选择工作区内的相对目录；绝对路径、`..` 和隐藏目录不能越权。
- 中文页面与英文 API 共用一个 FastAPI 应用、一个 JobManager 和一套 CLI。

## 2. 三类生产请求的 CLI 映射

| 页面请求 | 唯一 CLI | 关键配置 |
|---|---|---|
| 逐景时序 | `s1grits process_scenes --config <yaml>` | `processing.monthly.enabled=false`，或勾选逐轨月合成后为 `true/only=false` |
| 全瓦片月合成 | `s1grits process_scenes --config <yaml>` | `processing.monthly.enabled=true` 且 `only=true` |
| 影像附带静态层 | 同一个 `process_scenes` | `static_layers.run_after_scenes=true`，使用动态产品的参考格网 |

明确禁止：

- 不暴露已经移除的 `s1grits process`。
- 不调用旧 `process_static`。
- 静态层不能脱离影像任务单独提交。
- 升降轨选择 `BOTH` 时生成两份受控配置，按同一任务串行运行。

每个生产任务结束后固定执行：

1. `s1grits catalog resync --output-dir <cube>`；
2. `s1grits catalog validate --output-dir <cube>`；
3. `s1grits catalog doctor --strict --output-dir <cube>`。

任一命令非零退出时任务失败，后续命令不再执行。

## 3. 输出、增量与静态层契约

- Zarr 始终生成；“仅 Zarr”只关闭 COG 和 Preview。
- `output.existing_store=resume`：采用已有兼容格网并增量追加。
- `output.existing_month=skip`：已有月份不重复计算。
- 全瓦片月合成仍是 `smonthly` 产品，不恢复旧 standalone monthly workflow。
- 集成静态 post-stage 固定生成六类 OPERA RTC-STATIC 原始层：
  `local_inc_angle`、`inc_angle`、`ls_map`、`number_of_looks`、
  `rtc_anf_beta0`、`rtc_anf_sigma0`。
- 静态层与其动态产品按 `tile_id + flight_direction + track + grid_id`
  对齐，并保留 `reference_grid_id`、`reference_zarr_path` 与产品标签。

## 4. Grid ID、路径与 Catalog v8

- `grid_id` 使用 `make_grid_id()` 对 tile、CRS、仿射变换、宽高进行规范化哈希；它是像元格网连接键，不是显示名称。
- `grid_name` 仅供人读，不能替代 `grid_id`。
- Catalog 路径相对于数据立方体根；Web API 返回目录的解析路径仅用于本机诊断。
- 根 `catalog.parquet` 与逐瓦片 Catalog 均遵守 `CANONICAL_CATALOG_COLUMNS`。
- 页面打开 Catalog 时要求全部 46 个字段存在，且非空记录的 `schema_version` 只能为 8。
- STAC 统一由 `catalog resync` 生成 GeoParquet，不在生产工作流中分散写入。

核心关联：

| 主对象 | 主键/稳定标识 | 主要外键 |
|---|---|---|
| Catalog Item | `item_id` | `collection_id`, `tile_id`, `grid_id`, `geometry_group_id` |
| 动态 Zarr | `zarr_path` + `grid_id` | `tile_id`, `track`, `processing_signature` |
| Static Zarr | `zarr_path` + `grid_id` | `reference_grid_id`, `reference_zarr_path`, `geometry_group_id` |
| STAC GeoParquet Item | `item_id` | `collection_id`，资产 href 指向 Catalog 资产 |

## 5. 中文 Web API 契约

- `GET /api/capabilities`：返回软件版本、Schema v8、工作区和 MGRS 显示参数。
- `GET /api/map/mgrs`：从随包 `mgrs.parquet` 的 EPSG:4326 几何构建 bbox GeoJSON；Leaflet 在 EPSG:3857 中显示。
- MGRS 使用内存 GeoDataFrame 空间索引、bbox 查询、最多 5000 要素和 128 项 LRU 缓存；低于 4 级不加载。
- `POST /api/plan`：校验瓦片/AOI、时间、输出目录、容量和产品组合，不启动任务。
- `POST /api/tasks`：消费一次性预检令牌，生成服务器受控配置并入队。
- `/api/tasks/{id}/log`：按字节增量读取或下载完整日志。
- `/api/tasks/{id}/events`：提供排队、执行、Catalog 门禁与终态的结构化事件。
- `/api/output-directories`：仅浏览工作区内目录；Catalog 模式标记根级 `catalog.parquet`。
- `/api/catalog/inspect|report` 与 `/api/catalog`：只读校验、报告和最多 500 条查询结果。

## 6. 已通过的自动门禁

- CLI 命令面：不存在 standalone `process`，存在 `process_scenes`、`static ensure`、Catalog 四个子命令。
- Schema：`SCHEMA_VERSION == 8`，46 个规范字段。
- v3 Web/API 基线测试。
- 中文控制台的能力、静态资源、目录越权、Catalog v8、MGRS bbox、月合成/Static CLI 映射测试。
- Python 编译、JavaScript 语法、FastAPI 影子服务和浏览器烟测。

真实 OPERA 下载不属于代码发布门禁；它需要 Earthdata 凭据、网络窗口和用户明确选择的瓦片/时间范围，单独执行。

