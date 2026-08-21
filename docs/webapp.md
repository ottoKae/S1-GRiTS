# S1-GRiTS v3 中文 Web 控制台

中文控制台是 `s1grits serve` 提供的零构建 FastAPI 页面，用于完成 MGRS/AOI
预检、受控下载任务、Catalog Schema v8 检索与任务日志查看。页面不实现第二套
遥感处理逻辑；确认后的任务只调用公开 CLI。

正式 CLI、Catalog、路径和状态契约见
[`phase0-v3-web-contract.md`](phase0-v3-web-contract.md)。

## 1. 安装与启动

```powershell
conda activate py312_s1grits_v3
cd D:\Project\claude-demo\data_pipeline\S1-GRiTS
pip install -e ".[web]"

# 工作区是页面可浏览和任务可写入的唯一根目录
s1grits serve D:\Project\claude-demo\data_pipeline\S1-GRiTS\webapp_output `
  --host 127.0.0.1 --port 5556
```

浏览器打开 `http://127.0.0.1:5556/`。OpenAPI 文档位于 `/docs`。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `workspace` / `--root` | 必填 | 数据立方体和 `.webapp/jobs` 的安全根 |
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
- AOI 支持 EPSG:4326 的 Polygon/MultiPolygon GeoJSON 或经纬度矩形。
- 地图使用本地 Leaflet；道路/影像底图为在线 Google 图层。
- MGRS 由服务端 GeoParquet 内存空间索引按 bbox 返回，客户端使用 Leaflet
  Canvas 绘制；4 级以下不加载，超过 5000 个候选时提示继续放大。
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
也可下载完整日志。服务重启时仍处于 queued/running 的任务会标记为失败，避免
无依据地声称后台工作仍在继续。

## 3. Catalog 文件夹检索

“目录检索”不是本机任意文件选择器。用户只能浏览启动服务时指定的 workspace，
绿色标记表示该目录根部存在 `catalog.parquet`。打开后执行只读 Schema v8 校验；
通过后才能按瓦片、产品、轨道、月份查询或生成覆盖报告。

生产任务的输出目录也必须是 workspace 内的相对目录。选择 Catalog 不会修改
任务输出根，也不会自动执行 resync；需要修复目录时应显式创建受控任务或使用 CLI。

## 4. 主要 API

| 路由 | 作用 |
|---|---|
| `GET /api/capabilities` | v3、Schema v8、工作区与 MGRS 参数 |
| `GET /api/map/mgrs?bbox=&zoom=` | bbox MGRS GeoJSON |
| `POST /api/plan` | 只读预检并返回一次性令牌 |
| `GET/POST /api/tasks` | 任务列表与确认创建 |
| `GET/DELETE /api/tasks/{id}` | 状态与取消 |
| `GET /api/tasks/{id}/log` | JSON 增量日志或完整文本下载 |
| `GET /api/tasks/{id}/events` | 结构化生命周期事件 |
| `GET/POST /api/output-directories` | 安全浏览或新建工作区内目录 |
| `GET /api/catalog/inspect` | Schema v8 校验 |
| `GET /api/catalog` | 最多 500 条筛选记录 |
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

## 6. 验证

```powershell
python -m pytest -q
node --check src/s1grits/webapp/static/app.js
python -m ruff check src/s1grits/webapp tests/test_webapp_api.py tests/test_webapp_cn_api.py
```

`tests/test_webapp_cn_api.py` 覆盖 v3 能力、随包静态资源、目录越权、Catalog v8、
MGRS bbox 和月合成/Static 的 CLI 映射。全量测试使用短 `--basetemp` 可避免
Windows 传统 260 字符路径限制影响 GLCM Zarr 临时块文件。
