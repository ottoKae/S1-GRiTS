# S1-GRiTS 数据立方体控制台 / S1-GRiTS Data Cube Console

这是唯一公开维护的 Web 入口，统一通过 `s1grits serve` 启动。支持同页中文／English
切换，不需要另装旧英文版。三代界面的固定提交、
功能去向及历史查看方法见 [Web 历史索引](web-history.md)。

中文控制台是 `s1grits serve` 提供的零构建 FastAPI 页面，用于完成 MGRS/AOI
预检、受控下载任务、Catalog Schema v8 检索与任务日志查看。页面不实现第二套
遥感处理逻辑；确认后的任务只调用公开 CLI。

CLI 映射、Catalog 与路径规则在本文统一说明；生产参数见[配置参考](configuration.md)，
动态与静态格网配对见[工作流](workflows.md#static-alignment)。CLI／Agent／Web 的完整
支持边界、旧目录登记和任务重放文件位置见[入口支持矩阵](entry_points.md)。

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

### 中英文切换 / Language

顶部和弹窗内的“语言 / Language”选择器同步切换中文和 English。首次使用跟随
浏览器的首选语言：中文使用 zh-CN，其余使用 English；手动选择保存在本浏览器，
后续访问优先使用该选择。不启用浏览器存储时，当前页面仍可切换。

切换不刷新页面，不重置输入、AOI／瓦片选择、地图、规划令牌或运行任务；不会重新
提交预检或下载。状态、目录按钮、地图提示和规划说明同步更新。原始日志、路径、
产品／方法标识和科学参数保持原样，服务端错误用当前语言说明并附原始详情。
大任务确认短语以服务端原文为准，切换语言后仍须准确输入同一短语。

Use **Language** in the header or an open dialog to switch without restarting a
task or clearing your selections. Logs, paths and scientific identifiers remain
unchanged. Enter the exact server-provided confirmation phrase for a large task,
regardless of the current display language.

### 产品

- “逐景时序”调用 `process_scenes`；可同时生成逐轨月合成。
- “全瓦片月合成”仍调用 `process_scenes`，设置
  `processing.monthly.enabled=true` 和 `only=true`。
- “同时生成静态图层”配置集成 Static post-stage，固定生成六类原始层；
  它与影像使用相同动态格网，不能单独提交。
- “仅 Zarr”保留主数据立方体，关闭 COG 与 Preview。

<a id="contract"></a>

页面和 CLI 共用一条生产路径：不调用已移除的 `process` 或 `process_static`。
默认增量策略为 `output.existing_store=resume`、`output.existing_month=skip`。
逐景与月度产品的区别由 `processing.monthly.enabled/only` 决定。
Static 按瓦片、方向、轨道和 `grid_id` 与动态数据配对；网格身份由 CRS、
仿射变换和尺寸计算，显示名称不能替代该标识。

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

### 选择并读取已有产品 / Read an existing product

打开数据目录后，可以直接点击“查看产品列表 / Browse products”，或从地图格网的
“查看详细记录 / View records”进入。支持方向、track、月份、状态、处理级别、
处理签名、产品变体及实体／虚拟类型筛选；每页 50 条，地图统计使用完整筛选结果。
不在 MGRS 字典中的记录也可从列表访问。

每行展示 item ID、时间范围、grid ID、签名、变体和实际位置。点击该行“读取 / Read”
明确选择一个实例，再选波段、从 0 开始的行列及可选 UTC 日期范围，点击“读取像元 /
Read pixel”。日期包含起止当天；静态波段不使用时间筛选。输出使用原波段单位，
有限 NoData、NaN 和无穷值显示为 `null`，有效 0 保留；掩膜和贡献计数也可逐波段读取。
切换中文／English 保留所选实例、参数和结果。完整元数据、来源和读取警告可见。

Catalog 使用 Schema v8 的规范字段；不为旧数据补写缺失签名。
实体定位使用 `zarr_path + zarr_group`，虚拟视图使用 `view_path`，
科学身份保留 `processing_signature`、`product_variant` 和 `grid_id`；
成像组以 `geometry_group_id` 区分。同一资产的多条时间记录可以指向同一实例，读取详情
显示该资产实际时间范围；不会把某条 Catalog 记录误当作只能读取的单一时刻。
缺失或重复的 item ID 会明确报错，不自动选择第一条。

读取复用 `CubeResolver.open(record)`，先选择像元再计算数据；存储读取粒度仍受原始
Zarr chunk 大小约束。支持现有单父视图和多轨月视图，无需 COG／PNG，不创建实体副本。
视图父代发生变化会报错；旧父数据缺少 generation ID 时显示现有 Resolver 的检测限制。
实体、group 和视图父资产必须位于 workspace 或已登记只读根中；跨根父资产须另行登记。
浏览与读取不会写入外部源目录，也不会自动下载、生产或物化。

Static 另保留 `reference_grid_id`、`reference_zarr_path` 和产品标签。
Catalog/STAC 同步由 `catalog resync` 统一负责。每次生产任务按顺序执行
`resync`、`validate` 和 `doctor --strict`，任何一步失败都不能标记为成功。

## 4. 主要 API

`GET /api/catalog` 支持 `offset`（默认 0）和 `limit`（默认 500，最大 2000），
返回筛选后 `total`、当前 `returned`、分页参数和记录。它与 `/api/catalog/map`
共享实例筛选；兼容的 `/api/items` 也增加身份字段及筛选。新增筛选名为
`item_id`、`grid_id`、`processing_level`、`processing_signature`、`product_variant`、
`representation`（`physical`／`virtual`）；`track` 按数字匹配。

`GET /api/catalog/product?root_id=workspace&output=cube&item_id=...` 返回选中记录、
实际维度、波段／单位、时间范围、属性和警告。相同选择加 `band`、`row`、`col`、
可选 `start`／`end` 调用 `/api/catalog/sample`，返回该点的时间和值。
旧 `/api/timeseries` 根 Zarr 接口保留兼容；group／view 使用新的实例接口。

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

### 日志游标

`GET /api/jobs/{id}/log` 的 `after`／`next` 以行为单位，`next` 只推进到
本次实际返回的行末。按响应中的 `next` 继续读取，不会跳过分页剩余行。

`GET /api/tasks/{id}/log?format=json` 的 `offset`／`next_offset` 以原日志的
字节为单位。客户端使用上次返回的 `next_offset`；不自行按字符数累加。
UTF-8 块末尚未完整的字符留到下次读取，正在追加的半个字符不会提前变成乱码。
`tail=true` 或指定 offset 落在字符内部时，起点向后对齐到下一个字符。
`limit` 为软字节上限：小于单个字符长度时最多多读取三字节，以保证读取可推进。
`size` 表示本次读取前观察到的文件长度；这些接口只读，不改写日志。
不带 `format=json` 的下载接口保留完整原始日志字节。

## 5. 生产可选策略与单父派生（R6e）

基础生产的“生产可选策略”折叠区复用现有 YAML／`process_scenes`：空间滤波可选
none、TV Bregman、NLM；可生成 Ratio、RVI、GLCM；月度合成可选 median／mean。
默认 first_valid_pixel 和既有重采样规则不变。其他 intra-pass 方法为 average、
bursts_center、nlooks_weighted；它们沿用 CLI 的限制：30 m 最近邻、关闭空间滤波、
月度合成和 static。nlooks_weighted 需要工作区内真实 number-of-looks TIFF 目录，
预检检查资产，执行时再由现有 CLI 绑定 burst。非默认拼接结果位于
`<output>/derived/intra_pass_<method>`，任务的查看结果与目录检查指向此目录。
不同 pass 的融合不属于这里的 burst 拼接。

已有 Cube：目录查询 → 选择明确实例 → “从已有 Cube 派生”。首批支持实体 scenes／
smonthly／monthly 的 VV_dB、VH_dB；可指定 UTC 起止时间、小窗口、新输出目录，
以及 Ratio／RVI／GLCM、none／TV／NLM、月度 mean／median。月度派生要求 scenes
父实例有明确 track。插值单独执行，可选 none／linear，必须指定 db 或 linear_power
计算域，linear 还须指定最大间隔天数；不外推。插值掩膜已有的父产品不能继续用于
滤波、GLCM、月度合成或再次插值，界面预检沿用 CLI 约束。

`POST /api/tools/plan` 返回规范化参数、父身份、尺寸估算及白名单 CLI；预检不写数据。
`POST /api/tools/tasks` 消费一次性 plan_id，复用任务队列执行 `derive_cube` 和
`catalog validate`。父数据可以来自只读注册目录；输出只能在工作区的新目录，不能
位于注册只读根或父 Zarr 内。每个成功结果包含新产品身份、Catalog 和 `derivation.json`，
可直接回到目录浏览并按实例读取。取消／失败保留已产生的输出；服务重启不会自动重放
派生任务，也不提供原地续写。重新运行须选择新目录并重新预检。

The advanced production panel uses the existing YAML and CLI strategies. The existing-cube
derivation form runs the same `derive_cube` command in the managed queue, followed by catalog
validation. Both panels support English and Chinese without losing the current plan.
Parents remain unchanged; derived products use a new workspace directory and retain provenance.
Interrupted derivatives require a new destination and a new preflight, rather than in-place resume.
Local analysis and neighboring-grid mosaics are scheduled for R6g.

### 虚拟视图、显式物化与多轨月产品（R6f）

安装 `s1grits[web,views]` 后，选择已有产品，在“虚拟视图与显式物化”中选择操作。
未安装视图依赖时，能力接口返回 `available=false`，页面禁用预检并说明所需 extra。

1. **创建选择／特征视图**：选择实体父实例，设置 UTC 起止时间、像元窗口，勾选
   VV_dB／VH_dB／Ratio／RVI。时间戳包含起止时刻。保存的是定义和 Catalog 记录，
   不复制完整数组；读取才按需计算，原有观测／插值掩膜保留。
2. **物化所选虚拟视图**：明确选择虚拟实例，另选新的输出目录并预检。执行
   `view materialize` 写成实体 Zarr，随后检查 Catalog。它是独立动作，查看视图不会
   自动物化；结果有独立 item ID，保留视图定义和父身份。
3. **创建多轨月产品视图**：同一 Catalog 中选择单轨派生月实体，点击“加入当前实例”，
   或在有序父实例框逐行填入完整 item ID。列表从上到下为优先级，可直接编辑顺序。
   切换同一 Catalog 内的所选实例会保留列表；切换 Catalog 后清空列表。设置 UTC 月份
   范围、窗口及 first_valid_pixel／mean。预检沿用 R4c 的同格网、同方向、可比统计范围、
   明确不同轨道和实体父代约束，不对跨格网数据隐式重投影；旧生产月标签本身不足以证明
   统计范围。mean 使用有效双极化配对的线性功率等权均值，不做入射角归一化。

任务确认页区分虚拟／实体，显示规范化请求、预期 item ID、对应实体尺寸、警告及 CLI。
成功后的“检索结果”按明确 item ID 打开新结果。多轨结果保留 `n_contributing_tracks`
和 `contributes_TK...`，不把贡献轨道数写成 n_obs，也不虚构唯一 track／geometry group。
虚拟结果继续依赖可访问的父数据；父代没有 generation_id 时，元数据检查不能发现原地
改写的像元，这个已有 Resolver 限制会显示在警告中。

三种操作共用 `/api/tools/plan` 与 `/api/tools/tasks`，operation 分别为
`view_create`、`view_materialize`、`view_multitrack`；服务端构造现有 `view` CLI 的
白名单参数，依次执行工具和 `catalog validate`。只允许工作区新目录，外部注册根只读。
不接受任意 Python／shell、嵌套虚拟父代或自动依赖执行。取消／失败／重启沿用 R6e 的
保留文件、重新预检和新目录规则。

**English quick start:** Install `s1grits[web,views]`, select an existing product, and expand
“Virtual views and explicit materialization.” Create a selection/feature view from a physical
parent, or explicitly materialize a selected virtual view into a new directory. For a multi-track
monthly view, list exact parent item IDs from the same Catalog in priority order, choose the UTC
month range and first_valid_pixel or mean, then review the plan before running. The parent
monthly products must satisfy the existing CLI compatibility checks. Virtual outputs retain
references to their parents; materialization writes independent arrays. The result button opens
the exact new item. Interrupted tasks require a new destination and preflight.

### 本地分析与邻格镶嵌

本地分析读取选定的已有实体产品；先打开实例详情，再展开“本地 LandTrendr（实验版）”。
选择单点、小窗口或带 `point_id/col/row` 的 JSON 点集，明确 UTC 时间、VV/VH、年度
mean/median、db/linear_power 及扰动方向，指定工作区新目录后预检。Web 每次最多
64 个像元，每点最多 128 个有效年份；预检只读取所选序列并统计支持数，不运行拟合。
实际执行复用 `analyze_segments`／`analyze_segments_batch` CLI，写入分析表及来源回执。
这些文件没有被注册为新的 Cube 产品。

后端需按 [segmentation.md](segmentation.md) 单独安装已验收的 `landtrendr==0.4.0`
（nthh/LT-rs，本地实验版）；不作为基础包依赖自动安装。缺少后端时能力检测、页面和
API 都标明不可用。少于六个有效年份为 `insufficient_observations`，未拟合为 `no_fit`；
二者都不能解释为零变化。批次保留逐点状态，失败点的分段数为 null；部分失败的 CLI
退出码非零，任务显示失败，但“检索结果”仍能查看已产生的逐点汇总和 partial 回执。
CUSUM 的分发事项尚待完成，本地 CCDC 未实现，此页面不提供 GEE 或自动回退。

邻格镶嵌位于 Catalog 详情中的独立面板，COG-only 记录也可使用“加入镶嵌”。从同一
Catalog 按优先级列出精确 item ID，填写投影 CRS、米制分辨率和左／下／右／上边界，
选择 VRT 或 COG。边界必须构成整数个目标像元。输入须满足已有 `mosaic_grid` 约束：
同方向／处理签名／波段，同月月产品，或同次获取且同轨的逐景产品。重叠区域按列表
顺序逐波段取首个有效像元，使用精确坐标变换和最近邻，不修改生产拼接策略。

预检核对 Catalog 和 GeoTIFF 头信息，提交前复查来源文件大小／修改时间，复用 GDAL
VRT／COG CLI；不预扫描整个栅格。COG 的未压缩预估沿用服务 500 GiB 限制和 10 GiB
确认规则；VRT 显示等效实体大小，不能据此承诺物化后的磁盘用量。VRT 必须保留原始
COG 及生成的 `.sources` 依赖目录。结果按钮显示实际栅格路径、尺寸及来源回执，
不对栅格文件执行 Catalog 校验或隐式注册。

两类工具都要求输出位于工作区新目录、源 Cube 根之外；注册数据根保持只读。共用
预检、确认、日志、取消和失败保留规则；服务重启不自动重放。API 使用现有
`/api/tools/plan`、`/api/tools/tasks`，结果来自 `/api/tools/results/{job_id}` 的固定
回执／表格文件，不接受任意输出文件路径。

**English quick start:** Open a physical product and expand “Local LandTrendr (experimental).”
Choose a pixel, a small window or JSON point set; explicitly select annual aggregation, domain
and disturbance direction. Preflight shows support counts, then the existing local CLI runs.
Install the separate pinned backend as documented in [segmentation.md](segmentation.md).
Missing observations, no fit and failures are distinct states, never zero-change claims.
For inter-tile mosaics, use “Add to mosaic” on COG records, order the exact IDs, specify a
projected metre grid and choose VRT or COG. Results show tables or the raster path and receipt.
Retain original COGs and `.sources` for VRT access. Outputs require a new workspace directory
outside the source Cube; interrupted jobs require new preflight and a new destination.

## 6. 安全约束

- 默认只监听 localhost；远程绑定必须使用 token。
- 页面携带 `?token=` 时会将其作为 Bearer Token 用于后续 API 请求。
- 所有 CLI 都由服务器白名单构造，不接受用户命令字符串。
- 配置使用安全 YAML 解析；`output.base_dir` 在服务端重写到已批准目录。
- 目录和资产路径在解析后必须仍位于 workspace 内。
- 运行日志对 password、token、secret 和 API key 形式的值脱敏。
- AOI 上传限制为 50 MiB；ZIP 最多 20 项、解压后最多 200 MiB、最多 10000 个
  要素，并拒绝路径穿越和符号链接。Shapefile 临时文件在请求结束后立即清理。

## 7. 验证

```powershell
python -c "from pathlib import Path; Path('.test-tmp').mkdir(exist_ok=True)"
python -m pytest tests/test_webapp_api.py tests/test_webapp_cn_api.py -q -p no:cacheprovider --basetemp .test-tmp/webapp
node --check src/s1grits/webapp/static/app.js
python -m ruff check src/s1grits/webapp tests/test_webapp_api.py tests/test_webapp_cn_api.py
```

`tests/test_webapp_cn_api.py` 覆盖 v3 能力、随包静态资源、目录越权、Catalog v8、
MGRS bbox 和月合成/Static 的 CLI 映射。全量测试使用短 `--basetemp` 可避免
Windows 传统 260 字符路径限制影响 GLCM Zarr 临时块文件。

### 小范围真实下载检查

在使用者明确选择瓦片、月份与下载范围后，按以下步骤验收；自动测试不会替你发起下载。

1. 使用独立输出目录，选择一个瓦片、一个方向、一个月和 Zarr-only，先关闭
   static、月度合成和派生波段。预检只返回摘要，不创建任务或下载影像。
2. 核对摘要后确认一次，记录 `run_id`；任务应依次进入排队、处理、验证和完成状态。
   刷新页面不应产生重复任务，一次性预检令牌不能重复消费。
3. 核对任务 YAML、`job.json` 和脱敏日志；确认写入范围符合预检，Catalog 三项检查通过。
   Zarr 的 VV/VH、时间坐标、CRS、格网与 Catalog 一致，且没有生成未选择的产品。
4. 用网页查询同一批输出，核对数量和覆盖；任务结束后重启服务，确认台账可读且不重跑。
5. 检查无效 MGRS、缺少时间、越界输出路径会被拒绝，且不会启动额外下载。

若凭据、持续限流、磁盘余量或 Catalog 检查不满足要求，或发现目录已有待保留数据、
越界写入、凭据泄露，立即停止。保留任务、日志与输出排查原因，不自动删除或覆盖重跑。
验收记录至少包括预检摘要、任务身份、起止时间、输出结构和 Catalog 检查结果；
这些是每次执行的过程材料，不随源码发行。
