/* Shared Chinese/English display catalog. Keys never enter scientific requests. */
const S1_MESSAGES = {
  "关闭": ["关闭", "Close"],
  "分辨率（米）": ["分辨率（米）", "Resolution (metres)"],
  "邻格栅格镶嵌": ["邻格栅格镶嵌", "Inter-tile raster mosaic"],
  "同月或同次获取的 COG，按所列顺序逐波段取首个有效像元。目标范围采用目标 CRS 的米坐标；VRT 依赖原始 COG 和 .sources 目录。": ["同月或同次获取的 COG，按所列顺序逐波段取首个有效像元。目标范围采用目标 CRS 的米坐标；VRT 依赖原始 COG 和 .sources 目录。", "Select COGs from the same month or acquisition. Listed order sets per-band first-valid priority. Bounds use metres in the target CRS. VRT access requires original COGs and the .sources directory."],
  "目标范围（左 下 右 上）": ["目标范围（左 下 右 上）", "Target bounds (left bottom right top)"],
  "输出格式": ["输出格式", "Output format"],
  "预检工具任务": ["预检工具任务", "Preflight tool task"],
  "本地 LandTrendr（实验版）": ["本地 LandTrendr（实验版）", "Local LandTrendr (experimental)"],
  "使用已有实体产品，最多 64 个像元、128 个有效年份。少于 6 年或未拟合不表示零变化。CUSUM 分发审查未完成，CCDC 本地实现尚不可用；此页面不调用 GEE。": ["使用已有实体产品，最多 64 个像元、128 个有效年份。少于 6 年或未拟合不表示零变化。CUSUM 分发审查未完成，CCDC 本地实现尚不可用；此页面不调用 GEE。", "Reads existing physical products, up to 64 pixels and 128 valid years. Fewer than 6 years or no fit does not mean zero change. CUSUM distribution review is pending and local CCDC is unavailable. This page does not call GEE."],
  "选择方式": ["选择方式", "Selection mode"],
  "单点": ["单点", "Single pixel"],
  "小窗口": ["小窗口", "Small window"],
  "点集": ["点集", "Point set"],
  "点集 JSON（point_id、col、row）": ["点集 JSON（point_id、col、row）", "Points JSON (point_id, col, row)"],
  "年度合成": ["年度合成", "Annual aggregation"],
  "扰动方向": ["扰动方向", "Disturbance direction"],
  "增加": ["增加", "Increase"],
  "减少": ["减少", "Decrease"],
  "每年最少观测数": ["每年最少观测数", "Minimum observations per year"],
  "工具结果": ["工具结果", "Tool results"],
  "加入镶嵌": ["加入镶嵌", "Add to mosaic"],
  "需要 GDAL 的 VRT 和 COG 驱动。": ["需要 GDAL 的 VRT 和 COG 驱动。", "Requires GDAL with VRT and COG drivers."],
  "需要单独安装 landtrendr==0.4.0（本地实验后端）。": ["需要单独安装 landtrendr==0.4.0（本地实验后端）。", "Requires separately installed landtrendr==0.4.0 (experimental local backend)."],
  "确认工具任务": ["确认工具任务", "Confirm tool task"],
  "暂无结果回执": ["暂无结果回执", "No result receipt yet"],
  "缺测、未拟合和失败均不表示零变化；逐点状态及原始回执如下。": ["缺测、未拟合和失败均不表示零变化；逐点状态及原始回执如下。", "Missing data, no fit and failures do not mean zero change. See per-point status and the original receipt below."],
  "原始回执": ["原始回执", "Original receipt"],
  "空间去斑": ["空间去斑", "Spatial despeckling"],
  "生产可选策略": ["生产可选策略", "Optional production strategies"],
  "虚拟视图与显式物化": ["虚拟视图与显式物化", "Virtual views and explicit materialization"],
  "虚拟视图只保存定义，读取时依赖父数据；物化是独立操作，写入新的实体 Cube。": ["虚拟视图只保存定义，读取时依赖父数据；物化是独立操作，写入新的实体 Cube。", "Virtual views save definitions and read from their parents. Materialization is a separate action that writes a new physical Cube."],
  "操作": ["操作", "Operation"],
  "创建选择／特征视图": ["创建选择／特征视图", "Create a selection / feature view"],
  "物化所选虚拟视图": ["物化所选虚拟视图", "Materialize the selected virtual view"],
  "创建多轨月产品视图": ["创建多轨月产品视图", "Create a multi-track monthly view"],
  "仅支持同格网、同方向且统计范围一致的单轨派生月实体。逐行列出 item ID，顺序即优先级；贡献轨道数不是观测次数。": ["仅支持同格网、同方向且统计范围一致的单轨派生月实体。逐行列出 item ID，顺序即优先级；贡献轨道数不是观测次数。", "Requires single-track derived monthly products with matching grids, directions and statistical ranges. List one item ID per line, in priority order. Contributing-track counts are not observation counts."],
  "有序父实例（同一 Catalog，每行一个）": ["有序父实例（同一 Catalog，每行一个）", "Ordered parent items (same Catalog, one per line)"],
  "加入当前实例": ["加入当前实例", "Add selected item"],
  "开始月份 UTC": ["开始月份 UTC", "Start month UTC"],
  "结束月份 UTC": ["结束月份 UTC", "End month UTC"],
  "mean 在有效双极化配对的线性功率域等权计算，不做入射角归一化。": ["mean 在有效双极化配对的线性功率域等权计算，不做入射角归一化。", "Mean uses equal weights on valid dual-polarization pairs in linear power, without incidence-angle normalization."],
  "预检视图任务": ["预检视图任务", "Preflight view task"],
  "确认视图任务": ["确认视图任务", "Confirm view task"],
  "工具计算": ["工具计算", "Tool execution"],
  "缺少视图依赖，请安装 s1grits[views]。": ["缺少视图依赖，请安装 s1grits[views]。", "View dependencies are missing. Install s1grits[views]."],
  "虚拟视图只保存定义，不写完整数组。父数据必须保持可访问。": ["虚拟视图只保存定义，不写完整数组。父数据必须保持可访问。", "This virtual view saves a definition without writing full arrays. Parent data must remain accessible."],
  "本次将写入新的实体 Cube。": ["本次将写入新的实体 Cube。", "This task will write a new physical Cube."],
  "对应实体数据量：{p0} GiB": ["对应实体数据量：{p0} GiB", "Equivalent physical data size: {p0} GiB"],
  "同次获取内的 burst 拼接": ["同次获取内的 burst 拼接", "Intra-pass burst mosaicking"],
  "去斑算法（启用时）": ["去斑算法（启用时）", "Despeckling method (when enabled)"],
  "月合成方法（启用时）": ["月合成方法（启用时）", "Monthly reducer (when enabled)"],
  "Looks 目录（相对工作区）": ["Looks 目录（相对工作区）", "Looks directory (relative to workspace)"],
  "非默认拼接仅支持 30 米最近邻，不同时启用去斑、月合成或静态后处理。": ["非默认拼接仅支持 30 米最近邻，不同时启用去斑、月合成或静态后处理。", "Alternative mosaics require 30 m nearest resampling, without despeckling, monthly compositing or static post-processing."],
  "从此产品创建派生 Cube": ["从此产品创建派生 Cube", "Derive a Cube from this product"],
  "读取已有实体，写入新目录；不下载数据。时间为 UTC，结束时间包含该时刻。": ["读取已有实体，写入新目录；不下载数据。时间为 UTC，结束时间包含该时刻。", "Read an existing physical product into a new directory, without downloads. Timestamps are UTC and inclusive."],
  "开始时间 UTC": ["开始时间 UTC", "Start timestamp UTC"],
  "结束时间 UTC": ["结束时间 UTC", "End timestamp UTC"],
  "新输出目录（相对工作区）": ["新输出目录（相对工作区）", "New output directory (relative to workspace)"],
  "宽度（像元）": ["宽度（像元）", "Width (pixels)"],
  "高度（像元）": ["高度（像元）", "Height (pixels)"],
  "月合成": ["月合成", "Monthly composite"],
  "不启用": ["不启用", "Disabled"],
  "时间插值（单独执行）": ["时间插值（单独执行）", "Temporal interpolation (separate operation)"],
  "方法": ["方法", "Method"],
  "计算域": ["计算域", "Computation domain"],
  "请选择": ["请选择", "Select"],
  "最大间隔（天）": ["最大间隔（天）", "Maximum gap (days)"],
  "仅在现有时间轴上补内部缺测，不外推；不会把插值当作新观测。": ["仅在现有时间轴上补内部缺测，不外推；不会把插值当作新观测。", "Fill internal gaps on the existing time axis without extrapolation; interpolated values are not new observations."],
  "派生任务中断后不原地续写。保留已有输出，另选新目录重新提交。": ["派生任务中断后不原地续写。保留已有输出，另选新目录重新提交。", "Interrupted derivatives cannot resume in place. Retain existing output and submit again to a new directory."],
  "预检派生任务": ["预检派生任务", "Plan derivative"],
  "正在预检派生任务…": ["正在预检派生任务…", "Checking derivative request…"],
  "确认派生规划": ["确认派生规划", "Confirm derivative plan"],
  "已有产品：{p0}": ["已有产品：{p0}", "Existing product: {p0}"],
  "预计未压缩输出：{p0} GiB": ["预计未压缩输出：{p0} GiB", "Estimated uncompressed output: {p0} GiB"],
  "轨道编号": ["轨道编号", "Track"],
  "处理级别": ["处理级别", "Processing level"],
  "处理签名": ["处理签名", "Processing signature"],
  "产品变体": ["产品变体", "Product variant"],
  "完整元数据与来源": ["完整元数据与来源", "Full metadata and provenance"],
  "读取提示：{p0}": ["读取提示：{p0}", "Read warnings: {p0}"],
  "全部表示类型": ["全部表示类型", "All representations"],
  "实体产品": ["实体产品", "Physical product"],
  "虚拟视图": ["虚拟视图", "Virtual view"],
  "查看产品列表": ["查看产品列表", "Browse products"],
  "实例与位置": ["实例与位置", "Instance and location"],
  "读取": ["读取", "Read"],
  "上一页": ["上一页", "Previous"],
  "下一页": ["下一页", "Next"],
  "选中的产品实例": ["选中的产品实例", "Selected product instance"],
  "波段": ["波段", "Band"],
  "行（从 0 开始）": ["行（从 0 开始）", "Row (from 0)"],
  "列（从 0 开始）": ["列（从 0 开始）", "Column (from 0)"],
  "开始日期（含）": ["开始日期（含）", "Start date (inclusive)"],
  "结束日期（含）": ["结束日期（含）", "End date (inclusive)"],
  "读取像元": ["读取像元", "Read pixel"],
  "仅按需读取所选像元；null 表示缺测。": ["仅按需读取所选像元；null 表示缺测。", "Only the selected pixel is read on demand; null indicates missing data."],
  "数值": ["数值", "Value"],
  "静态": ["静态", "Static"],
  "正在读取产品元数据…": ["正在读取产品元数据…", "Reading product metadata…"],
  "已选择：{p0}": ["已选择：{p0}", "Selected: {p0}"],
  "正在读取所选像元…": ["正在读取所选像元…", "Reading the selected pixel…"],
  "{p0} · 行 {p1}，列 {p2} · {p3} 个值": ["{p0} · 行 {p1}，列 {p2} · {p3} 个值", "{p0} · row {p1}, column {p2} · {p3} values"],
  "serverOutput": ["服务器输出", "Server output"],
  "planEstimate": ["按未压缩 float32、109.8 km 名义瓦片估算；逐景按每月 4 景估计。", "Estimate uses uncompressed float32 and nominal 109.8 km tiles; scenes assume four acquisitions per month."],
  "largeConfirmation": ["规划估算达到 10 GiB，需要输入所示短语以防止误提交大任务。", "The estimate is at least 10 GiB. Enter the exact server-provided phrase below to confirm this large task. The phrase stays unchanged when switching languages."],
  "resumeReady": ["恢复检查通过。", "Resume checks passed."],
  "resumeBlocked": ["当前无法恢复，请查看下方原始诊断。", "Cannot resume. See the original diagnostics below."],
  "authenticationRequired": ["需要认证。服务端详情：{p1}", "Authentication required. Server details: {p1}"],
  "requestError": ["请求失败 ({p0})。服务端详情：{p1}", "Request failed ({p0}). Server details: {p1}"],
  "zoomIn": ["放大", "Zoom in"],
  "zoomOut": ["缩小", "Zoom out"],
  "S1-GRiTS 数据立方体控制台": [
    "S1-GRiTS 数据立方体控制台",
    "S1-GRiTS Data Cube Console"
  ],
  "Sentinel-1 时序数据准备与本地数据 · 中文控制台": [
    "Sentinel-1 时序数据准备与本地数据",
    "Sentinel-1 time-series preparation and local data"
  ],
  "正在连接…": [
    "正在连接…",
    "Connecting…"
  ],
  "选择产品": [
    "选择产品",
    "Choose products"
  ],
  "影像产品可附带静态几何层": [
    "影像产品可附带静态几何层",
    "Include static geometry layers with imagery"
  ],
  "逐景时序": [
    "逐景时序",
    "Scene time series"
  ],
  "Scenes + 可选逐轨月合成": [
    "Scenes + 可选逐轨月合成",
    "Scenes + optional monthly composites per track"
  ],
  "全瓦片月合成": [
    "全瓦片月合成",
    "Full-tile monthly composites"
  ],
  "空间与轨道": [
    "空间与轨道",
    "Area and orbit"
  ],
  "EPSG:4326 检索，MGRS 生产格网": [
    "EPSG:4326 检索，MGRS 生产格网",
    "Search in EPSG:4326; produce on MGRS grids"
  ],
  "瓦片选择": [
    "瓦片选择",
    "Select tiles"
  ],
  "AOI 候选筛选": [
    "AOI 候选筛选",
    "AOI candidates"
  ],
  "最终选择：": [
    "最终选择：",
    "Selected: "
  ],
  "个瓦片": [
    "个瓦片",
    "tiles"
  ],
  "清空瓦片": [
    "清空瓦片",
    "Clear tiles"
  ],
  "MGRS 瓦片（空格、逗号或换行分隔）": [
    "MGRS 瓦片（空格、逗号或换行分隔）",
    "MGRS tiles (spaces, commas or newlines)"
  ],
  "例如：49RGP 49RGQ": [
    "例如：49RGP 49RGQ",
    "Example: 49RGP 49RGQ"
  ],
  "文本输入与地图点击双向同步；再次点击已选瓦片即可取消。刷新本页仍保留本次会话的选择。": [
    "文本输入与地图点击双向同步；再次点击已选瓦片即可取消。刷新本页仍保留本次会话的选择。",
    "Text and map selections stay in sync. Click a selected tile to deselect it. Selections survive page refreshes in this session."
  ],
  "导入 AOI": [
    "导入 AOI",
    "Import AOI"
  ],
  "选择 GeoJSON、Shapefile ZIP，或同时选择 SHP 组件": [
    "选择 GeoJSON、Shapefile ZIP，或同时选择 SHP 组件",
    "Choose GeoJSON, Shapefile ZIP, or all SHP components"
  ],
  "Shapefile 至少需要 .shp、.shx、.dbf、.prj；服务端读取其 CRS 并统一转换到 EPSG:4326。": [
    "Shapefile 至少需要 .shp、.shx、.dbf、.prj；服务端读取其 CRS 并统一转换到 EPSG:4326。",
    "Shapefiles require .shp, .shx, .dbf and .prj. The server reads their CRS and converts them to EPSG:4326."
  ],
  "或输入经纬度矩形": [
    "或输入经纬度矩形",
    "Or enter longitude/latitude bounds"
  ],
  "西": [
    "西",
    "West"
  ],
  "东": [
    "东",
    "East"
  ],
  "南": [
    "南",
    "South"
  ],
  "北": [
    "北",
    "North"
  ],
  "应用矩形 AOI": [
    "应用矩形 AOI",
    "Apply rectangular AOI"
  ],
  "当前未设置 AOI。": [
    "当前未设置 AOI。",
    "No AOI selected."
  ],
  "全选候选": [
    "全选候选",
    "Select all candidates"
  ],
  "取消候选选择": [
    "取消候选选择",
    "Deselect candidates"
  ],
  "清除 AOI": [
    "清除 AOI",
    "Clear AOI"
  ],
  "AOI 只限定候选范围，不遮挡地图点击；清除 AOI 不会清除已选择的瓦片。": [
    "AOI 只限定候选范围，不遮挡地图点击；清除 AOI 不会清除已选择的瓦片。",
    "The AOI limits candidates without blocking map clicks. Clearing it preserves selected tiles."
  ],
  "轨道方向": [
    "轨道方向",
    "Orbit direction"
  ],
  "升降轨（串行处理）": [
    "升降轨（串行处理）",
    "Ascending + descending (sequential)"
  ],
  "仅升轨 ASCENDING": [
    "仅升轨 ASCENDING",
    "Ascending only"
  ],
  "仅降轨 DESCENDING": [
    "仅降轨 DESCENDING",
    "Descending only"
  ],
  "时间与输出": [
    "时间与输出",
    "Time and output"
  ],
  "先预检，再确认执行": [
    "先预检，再确认执行",
    "Review the plan before running"
  ],
  "年份": [
    "年份",
    "Years"
  ],
  "月份": [
    "月份",
    "Months"
  ],
  "数据立方体目录（相对输出根目录）": [
    "数据立方体目录（相对输出根目录）",
    "Cube directory (relative to output root)"
  ],
  "浏览": [
    "浏览",
    "Browse"
  ],
  "仅 Zarr（推荐）": [
    "仅 Zarr（推荐）",
    "Zarr only (recommended)"
  ],
  "同时生成静态图层": [
    "同时生成静态图层",
    "Include static layers"
  ],
  "同时生成逐轨月合成": [
    "同时生成逐轨月合成",
    "Include monthly composites per track"
  ],
  "高级处理选项": [
    "高级处理选项",
    "Advanced processing options"
  ],
  "目标分辨率": [
    "目标分辨率",
    "Target resolution"
  ],
  "30 米（原生兼容）": [
    "30 米（原生兼容）",
    "30 m (native-compatible)"
  ],
  "10 米（优化双线性）": [
    "10 米（优化双线性）",
    "10 m (bilinear)"
  ],
  "30 米采用最近邻重投影，保持现有产品兼容。": [
    "30 米采用最近邻重投影，保持现有产品兼容。",
    "30 m uses nearest-neighbor reprojection for compatibility with existing products."
  ],
  "并行瓦片数": [
    "并行瓦片数",
    "Parallel tiles"
  ],
  "TV 去斑": [
    "TV 去斑",
    "TV despeckling"
  ],
  "Ratio 波段": [
    "Ratio 波段",
    "Ratio band"
  ],
  "RVI 波段": [
    "RVI 波段",
    "RVI band"
  ],
  "v3 集成静态层": [
    "v3 集成静态层",
    "Integrated static layers"
  ],
  "与影像共用同一动态参考格网，固定生成六类原始静态层；不能脱离影像单独提交。": [
    "与影像共用同一动态参考格网，固定生成六类原始静态层；不能脱离影像单独提交。",
    "Six raw static layers share the imagery reference grid. They cannot be submitted without imagery."
  ],
  "局地入射角": [
    "局地入射角",
    "Local incidence angle"
  ],
  "入射角": [
    "入射角",
    "Incidence angle"
  ],
  "叠掩/阴影": [
    "叠掩/阴影",
    "Layover / shadow"
  ],
  "等效视数": [
    "等效视数",
    "Number of looks"
  ],
  "预检并创建下载任务": [
    "预检并创建下载任务",
    "Plan a download"
  ],
  "中国区域空间预检地图": [
    "中国区域空间预检地图",
    "Spatial planning map for China"
  ],
  "道路": [
    "道路",
    "Road"
  ],
  "卫星": [
    "卫星",
    "Satellite"
  ],
  "缩放至4级后显示MGRS格网": [
    "缩放至4级后显示MGRS格网",
    "Zoom to level 4 to show MGRS grids"
  ],
  "MGRS格网": [
    "MGRS格网",
    "MGRS grids"
  ],
  "本地数据": [
    "本地数据",
    "Local data"
  ],
  "地图图层图例": [
    "地图图层图例",
    "Map legend"
  ],
  "AOI候选": [
    "AOI候选",
    "AOI candidates"
  ],
  "下载选择": [
    "下载选择",
    "Download selection"
  ],
  "工作区": [
    "工作区",
    "Workspace"
  ],
  "任务中心": [
    "任务中心",
    "Tasks"
  ],
  "刷新任务": [
    "刷新任务",
    "Refresh tasks"
  ],
  "选择本地数据立方体": [
    "选择本地数据立方体",
    "Open a local Cube"
  ],
  "请选择根目录直接包含": [
    "请选择根目录直接包含",
    "Choose a folder containing "
  ],
  "的文件夹，选择后将自动校验并显示在地图上。": [
    "的文件夹，选择后将自动校验并显示在地图上。",
    " at its root. It will be validated and displayed on the map."
  ],
  "选择数据立方体文件夹": [
    "选择数据立方体文件夹",
    "Choose Cube folder"
  ],
  "手动输入路径": [
    "手动输入路径",
    "Enter path"
  ],
  "最近使用的数据": [
    "最近使用的数据",
    "Recent data"
  ],
  "本地数据立方体": [
    "本地数据立方体",
    "Local Data Cube"
  ],
  "更换文件夹": [
    "更换文件夹",
    "Change folder"
  ],
  "忘记记录": [
    "忘记记录",
    "Forget entry"
  ],
  "正在检查本地数据立方体…": [
    "正在检查本地数据立方体…",
    "Checking local Cube…"
  ],
  "当前文件夹没有 Catalog": [
    "当前文件夹没有 Catalog",
    "No Catalog in this folder"
  ],
  "可以重新选择，或只检查它的一级子目录。": [
    "可以重新选择，或只检查它的一级子目录。",
    "Choose another folder or inspect its immediate subfolders."
  ],
  "查找一级子目录": [
    "查找一级子目录",
    "Find in subfolders"
  ],
  "重新选择文件夹": [
    "重新选择文件夹",
    "Choose another folder"
  ],
  "瓦片，如 49RGP": [
    "瓦片，如 49RGP",
    "Tile, e.g. 49RGP"
  ],
  "全部产品": [
    "全部产品",
    "All products"
  ],
  "全部轨道": [
    "全部轨道",
    "All directions"
  ],
  "月份 YYYY-MM": [
    "月份 YYYY-MM",
    "Month YYYY-MM"
  ],
  "全部状态": [
    "全部状态",
    "All statuses"
  ],
  "检索并上图": [
    "检索并上图",
    "Search and map"
  ],
  "报告": [
    "报告",
    "Report"
  ],
  "清除图层": [
    "清除图层",
    "Clear layer"
  ],
  "产品与状态": [
    "产品与状态",
    "Products and status"
  ],
  "时间完整性": [
    "时间完整性",
    "Temporal completeness"
  ],
  "确认下载规划": [
    "确认下载规划",
    "Review download plan"
  ],
  "大任务二次确认": [
    "大任务二次确认",
    "Confirm large task"
  ],
  "复制短语": [
    "复制短语",
    "Copy phrase"
  ],
  "请准确输入上方短语": [
    "请准确输入上方短语",
    "Enter the exact phrase above"
  ],
  "返回修改": [
    "返回修改",
    "Back to edit"
  ],
  "确认并执行": [
    "确认并执行",
    "Confirm and run"
  ],
  "恢复中断任务": [
    "恢复中断任务",
    "Resume interrupted task"
  ],
  "取消": [
    "取消",
    "Cancel"
  ],
  "确认恢复": [
    "确认恢复",
    "Confirm resume"
  ],
  "选择输出目录": [
    "选择输出目录",
    "Choose output directory"
  ],
  "服务器输出根：": [
    "服务器输出根：",
    "Server output root: "
  ],
  "上一级": [
    "上一级",
    "Parent folder"
  ],
  "新建子目录名": [
    "新建子目录名",
    "New subfolder name"
  ],
  "新建": [
    "新建",
    "Create"
  ],
  "选择当前目录": [
    "选择当前目录",
    "Select this directory"
  ],
  "本地数据详情": [
    "本地数据详情",
    "Local data details"
  ],
  "关闭本地数据详情": [
    "关闭本地数据详情",
    "Close local data details"
  ],
  "产品": [
    "产品",
    "Product"
  ],
  "轨道": [
    "轨道",
    "Orbit"
  ],
  "时间": [
    "时间",
    "Time"
  ],
  "状态": [
    "状态",
    "Status"
  ],
  "逐级打开本机文件夹，选择后将检查根目录中的": [
    "逐级打开本机文件夹，选择后将检查根目录中的",
    "Browse local folders. On selection, the server checks "
  ],
  "关闭文件夹选择器": [
    "关闭文件夹选择器",
    "Close folder chooser"
  ],
  "当前位置": [
    "当前位置",
    "Current location"
  ],
  "此电脑": [
    "此电脑",
    "This computer"
  ],
  "正在读取可用位置…": [
    "正在读取可用位置…",
    "Loading available locations…"
  ],
  "重新加载": [
    "重新加载",
    "Reload"
  ],
  "目录较深时，也可以直接输入路径": [
    "目录较深时，也可以直接输入路径",
    "You can also enter a folder path directly"
  ],
  "手动输入绝对路径": [
    "手动输入绝对路径",
    "Enter absolute path"
  ],
  "选择此文件夹": [
    "选择此文件夹",
    "Select this folder"
  ],
  "手动输入数据文件夹": [
    "手动输入数据文件夹",
    "Enter data folder path"
  ],
  "请输入服务所在电脑上的绝对路径。该目录只用于本地检索，不会成为任务输出位置。": [
    "请输入服务所在电脑上的绝对路径。该目录只用于本地检索，不会成为任务输出位置。",
    "Enter an absolute path on the server computer. This folder is for read-only data access, not task output."
  ],
  "文件夹绝对路径": [
    "文件夹绝对路径",
    "Absolute folder path"
  ],
  "例如 D:\\data\\s1_cube": [
    "例如 D:\\data\\s1_cube",
    "Example: D:\\data\\s1_cube"
  ],
  "显示名称（可选）": [
    "显示名称（可选）",
    "Display name (optional)"
  ],
  "例如 湖北历史数据": [
    "例如 湖北历史数据",
    "Example: Historical data"
  ],
  "打开文件夹": [
    "打开文件夹",
    "Open folder"
  ],
  "任务日志": [
    "任务日志",
    "Task log"
  ],
  "正在读取任务状态…": [
    "正在读取任务状态…",
    "Reading task status…"
  ],
  "关闭任务日志": [
    "关闭任务日志",
    "Close task log"
  ],
  "自动跟随": [
    "自动跟随",
    "Follow output"
  ],
  "刷新": [
    "刷新",
    "Refresh"
  ],
  "下载完整日志": [
    "下载完整日志",
    "Download full log"
  ],
  "阶段事件": [
    "阶段事件",
    "Stage events"
  ],
  "实时输出": [
    "实时输出",
    "Live output"
  ],
  "请求失败 ({p0})": [
    "请求失败 ({p0})",
    "Request failed ({p0})"
  ],
  "{p0} 不在当前 AOI 候选范围；先清除 AOI 才能选择": [
    "{p0} 不在当前 AOI 候选范围；先清除 AOI 才能选择",
    "{p0} is outside the AOI candidates; clear the AOI to select it"
  ],
  "已选择": [
    "已选择",
    "Selected"
  ],
  "已取消": [
    "已取消",
    "Cancelled"
  ],
  "已选择 {p0}": [
    "已选择 {p0}",
    "Selected {p0}"
  ],
  "未选择": [
    "未选择",
    "Not selected"
  ],
  "MGRS参考格网已隐藏": [
    "MGRS参考格网已隐藏",
    "MGRS reference grids hidden"
  ],
  "正在加载MGRS格网…": [
    "正在加载MGRS格网…",
    "Loading MGRS grids…"
  ],
  "当前视窗包含 {p0} 个 MGRS 格网；缩放至 {p1} 级后显示边界": [
    "当前视窗包含 {p0} 个 MGRS 格网；缩放至 {p1} 级后显示边界",
    "{p0} MGRS grids in view; zoom to level {p1} to show boundaries"
  ],
  "当前视窗包含 {p0} 个 MGRS 格网，超过单次绘制上限 {p1}，请继续放大": [
    "当前视窗包含 {p0} 个 MGRS 格网，超过单次绘制上限 {p1}，请继续放大",
    "{p0} MGRS grids exceed the display limit of {p1}; zoom in"
  ],
  "已显示 {p0} 个 MGRS 格网 · z{p1}": [
    "已显示 {p0} 个 MGRS 格网 · z{p1}",
    "Showing {p0} MGRS grids · z{p1}"
  ],
  "MGRS参考格网加载失败：{p0}": [
    "MGRS参考格网加载失败：{p0}",
    "MGRS grid loading failed: {p0}"
  ],
  "Google 道路": [
    "Google 道路",
    "Google Roads"
  ],
  "OpenStreetMap（备用）": [
    "OpenStreetMap（备用）",
    "OpenStreetMap (fallback)"
  ],
  "Google 卫星": [
    "Google 卫星",
    "Google Satellite"
  ],
  "Esri 卫星（备用）": [
    "Esri 卫星（备用）",
    "Esri Satellite (fallback)"
  ],
  "{p0} 无法访问，正在切换到 {p1}…": [
    "{p0} 无法访问，正在切换到 {p1}…",
    "{p0} unavailable; switching to {p1}…"
  ],
  "{p0} 暂时无法访问；MGRS 与 AOI 仍可正常使用。": [
    "{p0} 暂时无法访问；MGRS 与 AOI 仍可正常使用。",
    "{p0} is unavailable. MGRS and AOI controls still work."
  ],
  "当前：{p0}": [
    "当前：{p0}",
    "Current: {p0}"
  ],
  "<div class=\"catalog-popup\"><b>{p0}</b><dl><dt>记录</dt><dd>{p1} 条</dd><dt>产品</dt><dd>{p2}</dd><dt>轨道</dt><dd>{p3}</dd><dt>时间</dt><dd>{p4} 至 {p5}</dd><dt>状态</dt><dd>{p6}</dd><dt>格网</dt><dd>{p7} 个版本</dd></dl><div class=\"popup-actions\"><button type=\"button\" onclick=\"addCatalogTile('{p8}')\">加入下载选择</button><button type=\"button\" onclick=\"openCatalogTileDetails('{p9}')\">查看详细记录</button></div></div>": [
    "<div class=\"catalog-popup\"><b>{p0}</b><dl><dt>记录</dt><dd>{p1} 条</dd><dt>产品</dt><dd>{p2}</dd><dt>轨道</dt><dd>{p3}</dd><dt>时间</dt><dd>{p4} 至 {p5}</dd><dt>状态</dt><dd>{p6}</dd><dt>格网</dt><dd>{p7} 个版本</dd></dl><div class=\"popup-actions\"><button type=\"button\" onclick=\"addCatalogTile('{p8}')\">加入下载选择</button><button type=\"button\" onclick=\"openCatalogTileDetails('{p9}')\">查看详细记录</button></div></div>",
    "<div class=\"catalog-popup\"><b>{p0}</b><dl><dt>Records</dt><dd>{p1}</dd><dt>Products</dt><dd>{p2}</dd><dt>Orbit</dt><dd>{p3}</dd><dt>Time</dt><dd>{p4} to {p5}</dd><dt>Status</dt><dd>{p6}</dd><dt>Grid</dt><dd>{p7} versions</dd></dl><div class=\"popup-actions\"><button type=\"button\" onclick=\"addCatalogTile('{p8}')\">Select for download</button><button type=\"button\" onclick=\"openCatalogTileDetails('{p9}')\">View records</button></div></div>"
  ],
  "本地数据 {p0} · {p1} 条记录": [
    "本地数据 {p0} · {p1} 条记录",
    "Local data {p0} · {p1} records"
  ],
  "已将 {p0} 加入下载选择": [
    "已将 {p0} 加入下载选择",
    "Added {p0} to download selection"
  ],
  "已显示 {p0} 个本地数据格网": [
    "已显示 {p0} 个本地数据格网",
    "Showing {p0} local data grids"
  ],
  "本地数据覆盖已隐藏": [
    "本地数据覆盖已隐藏",
    "Local data coverage hidden"
  ],
  "项目内置 Leaflet 资源未加载，请重新安装 S1-GRiTS Web 组件并重启服务。": [
    "项目内置 Leaflet 资源未加载，请重新安装 S1-GRiTS Web 组件并重启服务。",
    "Bundled Leaflet could not load. Reinstall S1-GRiTS Web components and restart the service."
  ],
  "{p0} · {p1} → EPSG:4326 · {p2} 个候选格网{p3}": [
    "{p0} · {p1} → EPSG:4326 · {p2} 个候选格网{p3}",
    "{p0} · {p1} → EPSG:4326 · {p2} candidate grids{p3}"
  ],
  "（最终最多选择 {p0} 个）": [
    "（最终最多选择 {p0} 个）",
    " (select up to {p0})"
  ],
  "请输入中国区域内有效的西、东、南、北边界。": [
    "请输入中国区域内有效的西、东、南、北边界。",
    "Enter valid west, east, south and north bounds within China."
  ],
  "正在计算 AOI 与 MGRS 候选关系…": [
    "正在计算 AOI 与 MGRS 候选关系…",
    "Finding MGRS candidates for the AOI…"
  ],
  "正在上传、转换投影并计算候选格网…": [
    "正在上传、转换投影并计算候选格网…",
    "Uploading, reprojecting and finding candidate grids…"
  ],
  "正在执行空间、容量与配置预检…": [
    "正在执行空间、容量与配置预检…",
    "Checking area, storage estimate and configuration…"
  ],
  "{p0} 年 · {p1} 个月份": [
    "{p0} 年 · {p1} 个月份",
    "Years {p0} · {p1} months"
  ],
  "<dl><dt>产品</dt><dd>{p0}</dd><dt>目标格网</dt><dd>{p1} 米 · {p2}</dd><dt>瓦片</dt><dd>{p3} 个：{p4}{p5}</dd><dt>轨道</dt><dd>{p6}</dd><dt>时间</dt><dd>{p7}</dd><dt>输出</dt><dd><code>{p8}</code></dd><dt>规划估算</dt><dd>{p9} GiB</dd></dl><p class=\"hint\">{p10}</p>": [
    "<dl><dt>产品</dt><dd>{p0}</dd><dt>目标格网</dt><dd>{p1} 米 · {p2}</dd><dt>瓦片</dt><dd>{p3} 个：{p4}{p5}</dd><dt>轨道</dt><dd>{p6}</dd><dt>时间</dt><dd>{p7}</dd><dt>输出</dt><dd><code>{p8}</code></dd><dt>规划估算</dt><dd>{p9} GiB</dd></dl><p class=\"hint\">{p10}</p>",
    "<dl><dt>Product</dt><dd>{p0}</dd><dt>Target grid</dt><dd>{p1} m · {p2}</dd><dt>Tiles</dt><dd>{p3}: {p4}{p5}</dd><dt>Orbit</dt><dd>{p6}</dd><dt>Time</dt><dd>{p7}</dd><dt>Output</dt><dd><code>{p8}</code></dd><dt>Estimated size</dt><dd>{p9} GiB</dd></dl><p class=\"hint\">{p10}</p>"
  ],
  "双线性（线性功率域）": [
    "双线性（线性功率域）",
    "Bilinear (linear power)"
  ],
  "最近邻": [
    "最近邻",
    "Nearest neighbor"
  ],
  "预检通过，请核对规划。": [
    "预检通过，请核对规划。",
    "Checks passed. Review the plan."
  ],
  "任务 {p0} 已进入队列。": [
    "任务 {p0} 已进入队列。",
    "Task {p0} queued."
  ],
  "排队中": [
    "排队中",
    "Queued"
  ],
  "处理中": [
    "处理中",
    "Running"
  ],
  "外部处理中": [
    "外部处理中",
    "Running externally"
  ],
  "影像处理": [
    "影像处理",
    "Processing imagery"
  ],
  "静态图层": [
    "静态图层",
    "Static layers"
  ],
  "目录复验": [
    "目录复验",
    "Validating Catalog"
  ],
  "关联复验": [
    "关联复验",
    "Validating associations"
  ],
  "已完成": [
    "已完成",
    "Completed"
  ],
  "失败": [
    "失败",
    "Failed"
  ],
  "取消中": [
    "取消中",
    "Cancelling"
  ],
  "已中断": [
    "已中断",
    "Interrupted"
  ],
  "{p0} · {p1}月": [
    "{p0} · {p1}月",
    "{p0} · {p1} months"
  ],
  "<button class=\"secondary\" onclick=\"cancelTask('{p0}')\">取消</button>": [
    "<button class=\"secondary\" onclick=\"cancelTask('{p0}')\">取消</button>",
    "<button class=\"secondary\" onclick=\"cancelTask('{p0}')\">Cancel</button>"
  ],
  "<button class=\"primary\" onclick=\"openRecovery('{p0}')\">恢复任务</button>": [
    "<button class=\"primary\" onclick=\"openRecovery('{p0}')\">恢复任务</button>",
    "<button class=\"primary\" onclick=\"openRecovery('{p0}')\">Resume task</button>"
  ],
  " · Static关联 {p0}": [
    " · Static关联 {p0}",
    " · Static associations {p0}"
  ],
  "<div class=\"task-meta ok\">Catalog {p0} 条 · {p1} 瓦片{p2}</div>": [
    "<div class=\"task-meta ok\">Catalog {p0} 条 · {p1} 瓦片{p2}</div>",
    "<div class=\"task-meta ok\">Catalog {p0} records · {p1} tiles{p2}</div>"
  ],
  "<article class=\"task-card\"><div class=\"task-title\"><b>{p0} · {p1} 瓦片</b><span class=\"badge {p2}\">{p3}</span></div><div class=\"task-meta\">{p4} · {p5}</div><div class=\"task-meta\" title=\"{p6}\">{p7}</div><div class=\"progress\"><i style=\"width:{p8}%\"></i></div>{p9}{p10}<div class=\"task-actions\"><button class=\"secondary\" onclick=\"openLog('{p11}')\">日志</button>{p12}{p13}<button class=\"secondary\" onclick=\"catalogFor({p14})\">检索结果</button></div></article>": [
    "<article class=\"task-card\"><div class=\"task-title\"><b>{p0} · {p1} 瓦片</b><span class=\"badge {p2}\">{p3}</span></div><div class=\"task-meta\">{p4} · {p5}</div><div class=\"task-meta\" title=\"{p6}\">{p7}</div><div class=\"progress\"><i style=\"width:{p8}%\"></i></div>{p9}{p10}<div class=\"task-actions\"><button class=\"secondary\" onclick=\"openLog('{p11}')\">日志</button>{p12}{p13}<button class=\"secondary\" onclick=\"catalogFor({p14})\">检索结果</button></div></article>",
    "<article class=\"task-card\"><div class=\"task-title\"><b>{p0} · {p1} tiles</b><span class=\"badge {p2}\">{p3}</span></div><div class=\"task-meta\">{p4} · {p5}</div><div class=\"task-meta\" title=\"{p6}\">{p7}</div><div class=\"progress\"><i style=\"width:{p8}%\"></i></div>{p9}{p10}<div class=\"task-actions\"><button class=\"secondary\" onclick=\"openLog('{p11}')\">Log</button>{p12}{p13}<button class=\"secondary\" onclick=\"catalogFor({p14})\">Browse results</button></div></article>"
  ],
  "确认取消该任务及其子进程？": [
    "确认取消该任务及其子进程？",
    "Cancel this task and its child processes?"
  ],
  "<p>正在检查原配置、输出目录和并发状态…</p>": [
    "<p>正在检查原配置、输出目录和并发状态…</p>",
    "<p>Checking saved configuration, output directory and running processes…</p>"
  ],
  "<p class=\"{p0}\">{p1}</p><dl><dt>输出目录</dt><dd><code>{p2}</code></dd><dt>恢复位置</dt><dd>第 {p3}/{p4} 条受控命令</dd><dt>执行次数</dt><dd>第 {p5} 次</dd><dt>现有成果</dt><dd>{p6} 个 Zarr · Catalog {p7}</dd></dl>{p8}{p9}<p class=\"hint\">恢复不会删除已有输出；完整月份将跳过，中断附近内容可能重新下载或处理。完成后会重新执行 Catalog 门禁。</p>": [
    "<p class=\"{p0}\">{p1}</p><dl><dt>输出目录</dt><dd><code>{p2}</code></dd><dt>恢复位置</dt><dd>第 {p3}/{p4} 条受控命令</dd><dt>执行次数</dt><dd>第 {p5} 次</dd><dt>现有成果</dt><dd>{p6} 个 Zarr · Catalog {p7}</dd></dl>{p8}{p9}<p class=\"hint\">恢复不会删除已有输出；完整月份将跳过，中断附近内容可能重新下载或处理。完成后会重新执行 Catalog 门禁。</p>",
    "<p class=\"{p0}\">{p1}</p><dl><dt>Output directory</dt><dd><code>{p2}</code></dd><dt>Resume from</dt><dd>Command {p3}/{p4}</dd><dt>Attempt</dt><dd>{p5}</dd><dt>Existing results</dt><dd>{p6} Zarr stores · Catalog {p7}</dd></dl>{p8}{p9}<p class=\"hint\">Resuming preserves existing outputs and skips complete months. Data near the interruption may be downloaded or processed again. Catalog checks run again on completion.</p>"
  ],
  "已存在": [
    "已存在",
    "exists"
  ],
  "尚未生成": [
    "尚未生成",
    "not yet created"
  ],
  "<h3>阻止恢复</h3><ul class=\"error\">{p0}</ul>": [
    "<h3>阻止恢复</h3><ul class=\"error\">{p0}</ul>",
    "<h3>Resume blocked</h3><ul class=\"error\">{p0}</ul>"
  ],
  "<h3>恢复说明</h3><ul>{p0}</ul>": [
    "<h3>恢复说明</h3><ul>{p0}</ul>",
    "<h3>Resume notes</h3><ul>{p0}</ul>"
  ],
  "排队": [
    "排队",
    "Queue"
  ],
  "影像": [
    "影像",
    "Imagery"
  ],
  "静态层": [
    "静态层",
    "Static"
  ],
  "关联": [
    "关联",
    "Associations"
  ],
  "完成": [
    "完成",
    "Done"
  ],
  "事件": [
    "事件",
    "Event"
  ],
  "任务日志 · {p0}": [
    "任务日志 · {p0}",
    "Task log · {p0}"
  ],
  "{p0} · {p1}/{p2} 条命令 · {p3}": [
    "{p0} · {p1}/{p2} 条命令 · {p3}",
    "{p0} · {p1}/{p2} commands · {p3}"
  ],
  "\\n[日志读取失败] {p0}\\n": [
    "\n[日志读取失败] {p0}\n",
    "\n[Log read failed] {p0}\n"
  ],
  "所选文件夹记录已经失效，请重新选择。": [
    "所选文件夹记录已经失效，请重新选择。",
    "The folder entry is no longer available. Select it again."
  ],
  "正在检查 catalog.parquet 与数据结构…": [
    "正在检查 catalog.parquet 与数据结构…",
    "Checking catalog.parquet and data structure…"
  ],
  "所选目录根部不存在 catalog.parquet。可以重新选择，或仅检查当前目录的一级子目录。": [
    "所选目录根部不存在 catalog.parquet。可以重新选择，或仅检查当前目录的一级子目录。",
    "No catalog.parquet at the selected root. Choose another folder or inspect its immediate subfolders."
  ],
  "当前文件夹不是可直接打开的数据立方体": [
    "当前文件夹不是可直接打开的数据立方体",
    "This folder is not a directly accessible Cube"
  ],
  "本地数据目录加载失败：{p0}": [
    "本地数据目录加载失败：{p0}",
    "Local data roots could not load: {p0}"
  ],
  "上次使用的数据已恢复；进入“本地数据”时将重新校验。": [
    "上次使用的数据已恢复；进入“本地数据”时将重新校验。",
    "Previous selection restored; it will be checked when you open Local data."
  ],
  "正在打开 {p0}…": [
    "正在打开 {p0}…",
    "Opening {p0}…"
  ],
  "正在读取本机盘符…": [
    "正在读取本机盘符…",
    "Loading local drives…"
  ],
  "正在加载文件夹…": [
    "正在加载文件夹…",
    "Loading folders…"
  ],
  "<div class=\"catalog-folder-row\" role=\"listitem\"><button class=\"catalog-folder-entry\" type=\"button\" data-path=\"{p0}\" aria-label=\"进入文件夹 {p1}\"><span class=\"folder-icon\" aria-hidden=\"true\">📁</span><span class=\"folder-name\">{p2}</span>{p3}<span class=\"folder-enter\" aria-hidden=\"true\">进入 ›</span></button></div>": [
    "<div class=\"catalog-folder-row\" role=\"listitem\"><button class=\"catalog-folder-entry\" type=\"button\" data-path=\"{p0}\" aria-label=\"进入文件夹 {p1}\"><span class=\"folder-icon\" aria-hidden=\"true\">📁</span><span class=\"folder-name\">{p2}</span>{p3}<span class=\"folder-enter\" aria-hidden=\"true\">进入 ›</span></button></div>",
    "<div class=\"catalog-folder-row\" role=\"listitem\"><button class=\"catalog-folder-entry\" type=\"button\" data-path=\"{p0}\" aria-label=\"Open folder {p1}\"><span class=\"folder-icon\" aria-hidden=\"true\">📁</span><span class=\"folder-name\">{p2}</span>{p3}<span class=\"folder-enter\" aria-hidden=\"true\">Open ›</span></button></div>"
  ],
  "<span class=\"catalog-ready\">含 Catalog</span>": [
    "<span class=\"catalog-ready\">含 Catalog</span>",
    "<span class=\"catalog-ready\">Has Catalog</span>"
  ],
  "<div class=\"folder-browser-message\">当前目录没有可进入的子文件夹。</div>": [
    "<div class=\"folder-browser-message\">当前目录没有可进入的子文件夹。</div>",
    "<div class=\"folder-browser-message\">No accessible subfolders here.</div>"
  ],
  "发现 {p0} 个子文件夹{p1}。": [
    "发现 {p0} 个子文件夹{p1}。",
    "Found {p0} subfolders{p1}."
  ],
  "，其中 {p0} 个包含 Catalog": [
    "，其中 {p0} 个包含 Catalog",
    ", {p0} with a Catalog"
  ],
  "请选择一个盘符或可用位置。": [
    "请选择一个盘符或可用位置。",
    "Choose a drive or available location."
  ],
  "文件夹加载失败：{p0}": [
    "文件夹加载失败：{p0}",
    "Folder loading failed: {p0}"
  ],
  "无法读取该位置。请重试，或使用下方其他方式。": [
    "无法读取该位置。请重试，或使用下方其他方式。",
    "Cannot read this location. Retry or use another method below."
  ],
  "已取消选择，当前本地数据未改变。": [
    "已取消选择，当前本地数据未改变。",
    "Selection cancelled; local data selection unchanged."
  ],
  "正在登记并检查所选文件夹…": [
    "正在登记并检查所选文件夹…",
    "Registering and checking the selected folder…"
  ],
  "请输入服务所在电脑上的绝对文件夹路径。": [
    "请输入服务所在电脑上的绝对文件夹路径。",
    "Enter an absolute folder path on the server computer."
  ],
  "正在登记并检查只读数据文件夹…": [
    "正在登记并检查只读数据文件夹…",
    "Registering and checking the read-only data folder…"
  ],
  "忘记“{p0}”的本地数据记录？\\n不会删除文件夹或其中的数据。": [
    "忘记“{p0}”的本地数据记录？\n不会删除文件夹或其中的数据。",
    "Forget the local data entry “{p0}”?\nThe folder and its data will not be deleted."
  ],
  "已忘记该数据记录，原始文件没有被删除。": [
    "已忘记该数据记录，原始文件没有被删除。",
    "Entry forgotten. Original files were not deleted."
  ],
  "正在受限扫描一级子目录…": [
    "正在受限扫描一级子目录…",
    "Inspecting immediate subfolders within the scan limit…"
  ],
  "已检查 {p0} 个一级子目录，没有发现 catalog.parquet。{p1}": [
    "已检查 {p0} 个一级子目录，没有发现 catalog.parquet。{p1}",
    "Checked {p0} immediate subfolders; no catalog.parquet found. {p1}"
  ],
  "扫描已达到安全上限。": [
    "扫描已达到安全上限。",
    "Scan limit reached."
  ],
  "发现 {p0} 个数据立方体{p1}，请选择一个打开。": [
    "发现 {p0} 个数据立方体{p1}，请选择一个打开。",
    "Found {p0} Cubes{p1}. Select one to open."
  ],
  "（结果已达到安全上限）": [
    "（结果已达到安全上限）",
    " (result limit reached)"
  ],
  "<div class=\"empty\">这里还没有子目录</div>": [
    "<div class=\"empty\">这里还没有子目录</div>",
    "<div class=\"empty\">No subfolders yet</div>"
  ],
  "正在检查 catalog.parquet 与 Schema v{p0} 契约…": [
    "正在检查 catalog.parquet 与 Schema v{p0} 契约…",
    "Checking catalog.parquet against Schema v{p0}…"
  ],
  "未知": [
    "未知",
    "Unknown"
  ],
  "已打开 · Schema v{p0} · {p1} 条记录 · {p2} 个瓦片": [
    "已打开 · Schema v{p0} · {p1} 条记录 · {p2} 个瓦片",
    "Opened · Schema v{p0} · {p1} records · {p2} tiles"
  ],
  "无法打开：{p0}": [
    "无法打开：{p0}",
    "Cannot open: {p0}"
  ],
  "Catalog 校验失败": [
    "Catalog 校验失败",
    "Catalog validation failed"
  ],
  "命中 {p0} 个瓦片，超过地图单次显示上限 {p1}；请增加瓦片、产品、轨道或月份条件。": [
    "命中 {p0} 个瓦片，超过地图单次显示上限 {p1}；请增加瓦片、产品、轨道或月份条件。",
    "{p0} tiles exceed the map limit of {p1}. Narrow the tile, product, orbit or month filters."
  ],
  "没有符合条件的本地数据记录{p0}": [
    "没有符合条件的本地数据记录{p0}",
    "No matching local records{p0}"
  ],
  "；{p0} 个编号未在 MGRS 字典中找到": [
    "；{p0} 个编号未在 MGRS 字典中找到",
    "; {p0} IDs not found in the MGRS lookup"
  ],
  "命中 {p0} 个瓦片 · {p1} 条记录 · {p2} 至 {p3} · 已显示在地图上": [
    "命中 {p0} 个瓦片 · {p1} 条记录 · {p2} 至 {p3} · 已显示在地图上",
    "{p0} tiles · {p1} records · {p2} to {p3} · shown on map"
  ],
  "无时间": [
    "无时间",
    "No time"
  ],
  "请先选择并打开通过校验的数据立方体目录。": [
    "请先选择并打开通过校验的数据立方体目录。",
    "Select and open a validated Cube directory first."
  ],
  "正在聚合本地记录并连接 MGRS 空间字典…": [
    "正在聚合本地记录并连接 MGRS 空间字典…",
    "Aggregating local records and matching MGRS grids…"
  ],
  "本地数据详情 · {p0}": [
    "本地数据详情 · {p0}",
    "Local data details · {p0}"
  ],
  "正在按需读取该瓦片记录…": [
    "正在按需读取该瓦片记录…",
    "Reading records for this tile…"
  ],
  "共 {p0} 条，当前显示 {p1} 条 · {p2}": [
    "共 {p0} 条，当前显示 {p1} 条 · {p2}",
    "{p0} records; showing {p1} · {p2}"
  ],
  "<div>无</div>": [
    "<div>无</div>",
    "<div>None</div>"
  ],
  "正在生成覆盖与完整性报告…": [
    "正在生成覆盖与完整性报告…",
    "Generating coverage and completeness report…"
  ],
  "记录": [
    "记录",
    "Records"
  ],
  "瓦片": [
    "瓦片",
    "Tiles"
  ],
  "有效月份": [
    "有效月份",
    "Available months"
  ],
  "存在缺月": [
    "存在缺月",
    "With missing months"
  ],
  "<div class=\"report-list\"><div>时间范围：{p0} 至 {p1}</div><div>完整组合：{p2}</div><div>缺月组合：{p3}</div><div>报告瓦片行：{p4}{p5}</div></div>": [
    "<div class=\"report-list\"><div>时间范围：{p0} 至 {p1}</div><div>完整组合：{p2}</div><div>缺月组合：{p3}</div><div>报告瓦片行：{p4}{p5}</div></div>",
    "<div class=\"report-list\"><div>Time range: {p0} to {p1}</div><div>Complete combinations: {p2}</div><div>Combinations with gaps: {p3}</div><div>Tile rows: {p4}{p5}</div></div>"
  ],
  "（页面摘要已截断）": [
    "（页面摘要已截断）",
    " (summary truncated)"
  ],
  "报告已生成 · {p0}": [
    "报告已生成 · {p0}",
    "Report ready · {p0}"
  ],
  "已连接 · S1-GRiTS {p0} · Schema v{p1} · {p2}": [
    "已连接 · S1-GRiTS {p0} · Schema v{p1} · {p2}",
    "Connected · S1-GRiTS {p0} · Schema v{p1} · {p2}"
  ],
  "服务连接失败": [
    "服务连接失败",
    "Service connection failed"
  ],
  "10 米采用 NoData 感知双线性插值；后向散射先在功率域插值，再转换为 dB。分类静态层仍采用最近邻。": [
    "10 米采用 NoData 感知双线性插值；后向散射先在功率域插值，再转换为 dB。分类静态层仍采用最近邻。",
    "10 m uses NoData-aware bilinear interpolation in linear power before conversion to dB. Categorical static layers use nearest neighbor."
  ],
  "本地数据地图图层已清除。": [
    "本地数据地图图层已清除。",
    "Local data map layer cleared."
  ]
};

const S1I18n = (() => {
  const storageKey = 's1grits_language', views = new Map();
  let saved;
  try { saved = localStorage.getItem(storageKey); } catch { /* Browser storage may be disabled. */ }
  let language = ['zh-CN', 'en'].includes(saved) ? saved
    : /^zh\b/i.test(navigator.languages?.[0] || navigator.language || '') ? 'zh-CN' : 'en';

  function t(key, params = {}) {
    const pair = S1_MESSAGES[key];
    const template = pair ? pair[language === 'en' ? 1 : 0] : (key ?? '');
    return template.replace(/\{(p\d+)\}/g, (match, name) => String(params[name] ?? match));
  }

  function setView(node, property, render) {
    if (!views.has(node)) views.set(node, new Map());
    views.get(node).set(property, render);
    node[property] = render();
  }

  // Only display nodes/attributes change; existing buttons keep their handlers and focus.
  function updateMarkup(target, source) {
    if (target.childNodes.length !== source.childNodes.length) {
      target.replaceChildren(...source.childNodes);
      return;
    }
    [...target.childNodes].forEach((node, i) => {
      const next = source.childNodes[i];
      if (node.nodeType === Node.TEXT_NODE && next.nodeType === Node.TEXT_NODE) node.nodeValue = next.nodeValue;
      else if (node.nodeType === Node.ELEMENT_NODE && node.nodeName === next.nodeName) {
        for (const attribute of ['title', 'aria-label', 'alt']) {
          if (next.hasAttribute(attribute)) node.setAttribute(attribute, next.getAttribute(attribute));
        }
        updateMarkup(node, next);
      } else node.replaceWith(next.cloneNode(true));
    });
  }

  function apply() {
    document.documentElement.lang = language;
    document.querySelectorAll('[data-i18n]').forEach(node => { node.textContent = t(node.dataset.i18n); });
    for (const attribute of ['title', 'placeholder', 'aria-label', 'alt']) {
      document.querySelectorAll(`[data-i18n-${attribute}]`).forEach(node => {
        node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`)));
      });
    }
    for (const [node, properties] of views) {
      if (!node.isConnected) { views.delete(node); continue; }
      for (const [property, render] of properties) {
        if (property === 'innerHTML') {
          const template = document.createElement('template');
          template.innerHTML = render();
          updateMarkup(node, template.content);
        } else node[property] = render();
      }
    }
    document.querySelectorAll('[data-language]').forEach(select => { select.value = language; });
    document.dispatchEvent(new Event('s1-language-change'));
  }

  function setLanguage(value) {
    if (!['zh-CN', 'en'].includes(value)) throw new Error('Unsupported language');
    language = value;
    try { localStorage.setItem(storageKey, value); } catch { /* Keep the session selection. */ }
    apply();
  }

  document.querySelectorAll('[data-language]').forEach(select => select.addEventListener('change', event => setLanguage(event.target.value)));
  apply();
  return {t, setView, setLanguage, get language() { return language; }};
})();
