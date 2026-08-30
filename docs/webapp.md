# S1-GRiTS v3 中文 Web 控制台

中文控制台是 `s1grits serve` 提供的零构建 FastAPI 页面，用于完成 MGRS/AOI
预检、受控下载任务、Catalog Schema v8 检索与任务日志查看。页面不实现第二套
遥感处理逻辑；确认后的任务只调用公开 CLI。

正式 CLI、Catalog、路径和状态契约见
[`phase0-v3-web-contract.md`](phase0-v3-web-contract.md)。

## 1. 安装与启动

```powershell
conda activate py312_s1grits_v3
cd D:\path\to\S1-GRiTS
pip install -e ".[web]"

# 工作区是任务唯一可写根；--catalog-root 是额外只读本地数据根，可重复指定
s1grits serve D:\S1-GRiTS-output `
  --catalog-root "湖北成果=E:\s1grits-data" `
  --catalog-root "F:\s1grits-archive" `
  --host 127.0.0.1 --port 5556
```

浏览器打开 `http://127.0.0.1:5556/`。OpenAPI 文档位于 `/docs`。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `workspace` / `--root` | 必填 | 数据立方体和 `.webapp/jobs` 的安全根 |
| `--catalog-root` | 无 | 额外只读本地数据根；可重复使用 `PATH` 或 `名称=PATH` |
| `--host` | `127.0.0.1` | 非本机地址必须配置 token，或显式使用 `--insecure` |
| `--port` | `8765` | 本机服务端口 |
| `--token` | `S1GRITS_WEB_TOKEN` | 为全部 `/api` 请求启用 Bearer Token |
| `--max-concurrent-jobs` | `1` | 同时运行的顶层任务数；每个任务内部仍可并行瓦片 |

## 2. 页面工作流

### 产品

- “逐景时序”调用 `process_scenes`；可同时生成逐轨月合成。
- “全瓦片月合成”仍调用 `process_scenes`，设置
  `processing.monthly.enabled=true` 和 `only=true`。
- “同时生成静态图层”配置集成 Static post-stage，固定生成六类原始层；
  它与影像使用相同动态格网，不能单独提交。
- “仅 Zarr”保留主数据立方体，关闭 COG 与 Preview。

### 空间

- 输入 MGRS 编号时使用随包 `s1grits/data/mgrs.parquet` 校验。
- AOI 支持 Polygon/MultiPolygon GeoJSON、经纬度矩形、Shapefile ZIP，或一次
  多选同名 `.shp/.shx/.dbf/.prj/.cpg` 组件。Shapefile 必须带 `.prj`，服务端
  读取源 CRS 后统一转换到 EPSG:4326。
- AOI 只定义候选瓦片，不是最终任务输入：候选默认全选，用户可以在地图上再次
  点击取消，也可以全选/取消候选。清除 AOI 不清除已选瓦片；“清空瓦片”才清空
  最终任务集合。瓦片选择在当前浏览器会话中保持，AOI 文件和几何不持久化。
- 地图使用本地 Leaflet；Google 道路/卫星为主底图，OpenStreetMap 和 Esri
  卫星分别作为自动备用源。部署环境可通过
  `S1GRITS_BASEMAP_ROAD_URL`、`S1GRITS_BASEMAP_ROAD_FALLBACK_URL`、
  `S1GRITS_BASEMAP_SATELLITE_URL` 和 `S1GRITS_BASEMAP_SATELLITE_FALLBACK_URL`
  覆盖地址。底图保留两个视窗的瓦片缓冲并沿用 Leaflet 默认的移动/缩放加载
  行为；初始瓦片连续加载失败时自动切换备用源，不影响 MGRS/AOI 操作。
- MGRS 由服务端 GeoParquet 内存空间索引按 bbox 返回，客户端使用 Leaflet
  Canvas 单层绘制；4 级以下只返回当前视窗真实格网数量而不序列化几何，超过
  5000 个候选时同样只提示数量并要求继续放大。视窗结果
  建立 `tile_id` 到图层的索引，单瓦片点击只更新该图层；AOI 批量选择才执行一次
  可视图层样式刷新。输入框与会话保存采用防抖，避免每次按键触发大集合重绘。
  道路与卫星底图分别使用高对比样式；卫星底图下参考格网为黄色、AOI候选为
  橙色、下载选择为青色，本地数据覆盖为紫色。
- 服务端返回 EPSG:4326 GeoJSON，Leaflet 负责 Web Mercator 显示；几何不参与
  生产重投影，生产 CRS 仍由每个 MGRS 瓦片的 UTM EPSG 决定。

### 预检与执行

“预检并创建下载任务”先校验空间、时间、容量、输出目录和产品组合，只生成
一小时有效的一次性令牌。用户确认后才写入任务队列。升降轨一起选择时，两个
方向在同一任务中串行执行，最后运行 Catalog resync、validate 和 strict doctor。

任务保存在 `{workspace}/.webapp/jobs/{id}/`：

- `config_01.yaml`、`config_02.yaml`：服务器生成并重新限定输出根的配置；
- `job.json`：可跨服务重启读取的任务台账；
- `log.txt`：CLI 合并输出，凭据类值会替换为 `[REDACTED]`。

任务中心显示排队、影像、静态层、Catalog 和终态；日志窗口按字节增量读取，
也可下载完整日志。服务重启时，尚未执行的 queued 任务自动重新排队；原 CLI
已经退出的 running 任务转为 `interrupted`，等待用户点击“恢复任务”。恢复前
服务端检查原 YAML、输出边界、resume/skip 策略、命令阶段、进程身份和同目录
并发；检查通过才从首个未完成命令重放。已有 Zarr/月成果保持不动，日志以新的
attempt 分隔后追加，最后总是重新执行 Catalog 三项门禁。如果检测到原 CLI PID
仍存活，任务显示为 `detached` 并禁止重复执行。

容量小于 10 GiB 时确认短语区域完全隐藏；达到阈值时页面明确显示“大任务二次
确认”、必须输入的准确短语及复制按钮，输入匹配前执行按钮保持禁用。

## 3. 本地数据

地图下方使用“任务中心 / 本地数据”双标签工作区，不再打开全屏 Catalog列表。
未选择数据时，页面只显示“选择数据立方体文件夹”主操作；点击后直接打开 Windows
原生目录选择器，不再先显示 workspace、服务器输出或数据根下拉框。也可通过次要入口
手动输入服务所在电脑上的绝对路径。目录根部存在 `catalog.parquet` 时，页面立即执行
只读 Schema v8 校验并自动上图；否则只提供“重新选择”和受限的“查找一级子目录”。
候选发现跳过隐藏目录及 `.zarr`，不递归，并限制为最多检查 1000 个入口、200 个目录和
返回 100 个候选，避免扫描整块磁盘。筛选条件只在校验通过后出现。

验证成功的数据立方体显示在“最近使用”中。网页授权记录保存在 workspace 的
`.webapp/catalog_roots.json`，最近选择保存在当前浏览器；服务重启后可恢复。忘记记录
只移除授权或当前子立方体历史，不删除原始目录或数据。任务输出根保持唯一可写；
workspace 和 `--catalog-root` 仍作为后端安全边界，但不暴露为普通用户的首要操作。
校验通过后才能按瓦片、产品、轨道、月份和状态查询或生成覆盖报告。检索结果由服务端
按 `tile_id` 聚合并连接随包 MGRS 字典，以紫色覆盖直接显示在主地图；点击格网
查看记录数、产品、轨道、时间、状态和格网版本摘要。原始记录只在用户点击
“查看详细记录”后按需读取，下载选择与本地数据覆盖保持独立。

生产任务的输出目录必须是 workspace 内的相对目录。外部本地数据根不能用于任务
写入；选择 Catalog 不会修改任务输出根，也不会自动执行 resync；需要修复目录时应
显式创建受控任务或使用 CLI。

## 4. 主要 API

| 路由 | 作用 |
|---|---|
| `GET /api/capabilities` | v3、Schema v8、工作区与 MGRS 参数 |
| `GET /api/map/mgrs?bbox=&zoom=` | bbox MGRS GeoJSON |
| `POST /api/spatial/aoi/resolve` | GeoJSON/矩形或 multipart Shapefile 转换与候选瓦片 |
| `POST /api/plan` | 只读预检并返回一次性令牌 |
| `GET/POST /api/tasks` | 任务列表与确认创建 |
| `GET/DELETE /api/tasks/{id}` | 状态与取消 |
| `GET /api/tasks/{id}/log` | JSON 增量日志或完整文本下载 |
| `GET /api/tasks/{id}/events` | 结构化生命周期事件 |
| `GET /api/tasks/{id}/recovery` | 中断任务只读恢复检查 |
| `POST /api/tasks/{id}/resume` | 检查通过后人工恢复 |
| `GET/POST /api/output-directories` | 安全浏览或新建工作区内目录 |
| `GET /api/catalog-roots` | 已授权只读本地数据根 |
| `POST /api/catalog-roots` | 以绝对路径持久化登记只读数据根 |
| `POST /api/catalog-roots/pick` | 打开 Windows 原生文件夹选择器并登记所选目录 |
| `DELETE /api/catalog-roots/{root_id}` | 移除网页登记（不删除数据） |
| `GET /api/catalog-directories` | 在指定只读数据根内安全浏览目录 |
| `GET /api/catalog-candidates` | 受限检查授权根的一级数据立方体候选 |
| `GET /api/catalog/inspect` | Schema v8 校验 |
| `GET /api/catalog` | 最多 500 条筛选记录 |
| `GET /api/catalog/map` | 不受记录列表上限影响的 MGRS 聚合覆盖 GeoJSON |
| `GET /api/catalog/report` | 产品、状态、方向和缺月摘要 |

原有 `/api/health`、`/api/workspace`、`/api/tiles`、`/api/items`、
`/api/timeseries`、`/api/asset`、`/api/coverage` 和 `/api/bursts` 继续保留，
可供自动化客户端使用。

## 5. 安全约束

- 默认只监听 localhost；远程绑定必须使用 token。
- 页面携带 `?token=` 时会将其作为 Bearer Token 用于后续 API 请求。
- 所有 CLI 都由服务器白名单构造，不接受用户命令字符串。
- 配置使用安全 YAML 解析；`output.base_dir` 在服务端重写到已批准目录。
- 目录和资产路径在解析后必须仍位于 workspace 内。
- 运行日志对 password、token、secret 和 API key 形式的值脱敏。
- AOI 上传限制为 50 MiB；ZIP 最多 20 项、解压后最多 200 MiB、最多 10000 个
  要素，并拒绝路径穿越和符号链接。Shapefile 临时文件在请求结束后立即清理。

## 6. 验证

```powershell
python -m pytest -q
node --check src/s1grits/webapp/static/app.js
python -m ruff check src/s1grits/webapp tests/test_webapp_api.py tests/test_webapp_cn_api.py
```

`tests/test_webapp_cn_api.py` 覆盖 v3 能力、随包静态资源、目录越权、Catalog v8、
MGRS bbox 和月合成/Static 的 CLI 映射。全量测试使用短 `--basetemp` 可避免
Windows 传统 260 字符路径限制影响 GLCM Zarr 临时块文件。
