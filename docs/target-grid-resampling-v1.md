# S1-GRiTS 目标格网与插值规范 v1

## 1. 决策摘要

S1-GRiTS 生产格网只允许 `30 m` 和 `10 m` 两种目标分辨率：

| 目标分辨率 | `auto` 实际算法 | 用途 |
|---|---|---|
| 30 m | `nearest` | 默认生产规格，保持历史产品兼容 |
| 10 m | `bilinear` | 面向 10 米分析栅格的连续场优化插值 |

10 米产品是空间采样和格网对齐结果，并不提高 Sentinel-1 原始观测的
物理分辨能力。相同区域的 10 米格网像元数约为 30 米格网的 9 倍，存储、
网络和计算预算也应据此规划。

## 2. GeoTessera 参考与本项目实现

参考实现位于 `third_party/geotessera`。GeoTessera 对连续特征通道使用
Rasterio/GDAL `reproject(..., Resampling.bilinear)`，其金字塔降采样另用
`average`。S1-GRiTS 采用相同的连续场重投影原则，但没有复制或依赖
GeoTessera 代码；实现集中在 `src/s1grits/resampling.py` 和现有镶嵌器中。

S1-GRiTS 的雷达约束更严格：

1. VV/VH RTC 数组先在**线性功率域**执行插值，再转换为 dB；不得直接对
   dB 值做双线性插值。
2. 采用 GDAL 的 `src_nodata`、`dst_nodata` 和初始化掩膜语义，避免无效值
   参与权重计算或污染有效边界。
3. 多 Burst 重叠区域保持既有的“首个有效像元优先”策略；双线性只改变
   单个源到目标格网的重投影，不混合不同 Burst 的辐射值。
4. 跨 CRS、跨 UTM 带或非整数像元偏移的源先一次性预对齐，再按 Zarr
   block 做切片读取，避免每个 block 重复调用 GDAL。
5. 分类静态层 `ls_map` 无条件使用最近邻。其余连续静态层在 10 米规格下
   使用双线性。

## 3. YAML/CLI 正式契约

```yaml
processing:
  target_resolution: 10.0       # 仅允许 30 或 10
  resampling_method: auto       # auto | nearest | bilinear
```

推荐始终使用 `auto`。CLI 读取 YAML 后会在下载或创建目录之前完成校验，
并将其解析为确定值：10 米为 `bilinear`，30 米为 `nearest`。显式设置
`nearest` 或 `bilinear` 仅用于对照实验；其他名称和其他分辨率均失败退出。

示例：

```powershell
conda run -n py312_s1grits_v3 s1grits process_scenes --config config/my_10m.yaml
```

30 米与 10 米产品不能追加到同一个既有 Zarr store，因为 CRS/仿射变换、
shape 和 `grid_id` 不同。需要同时保留两种产品时，应使用不同的
`output.base_dir`（例如 `cube_30m`、`cube_10m`）。

## 4. Web 中文前端契约

高级处理选项只显示两个目标格网：

- `30 米（原生兼容）`
- `10 米（优化双线性）`

浏览器向 `/api/plan` 提交 `target_resolution` 和 `resampling_method: auto`。
服务端不信任浏览器值，会再次执行同一套白名单校验。预检结果明确显示
最终解析后的算法，生成的任务 YAML 写入确定值 `nearest` 或 `bilinear`。

## 5. 产品与 Catalog 可追踪性

Scenes、smonthly 和 static Zarr 根属性写入：

- `target_resolution_m`
- `resampling_method`
- `processing_variant_json`
- `processing_signature`

`processing.target_resolution` 与 `processing.resampling_method` 参与产品签名，
因此不同分辨率或插值语义不会被 Catalog/Resolver 当成同一处理变体。
格网本身仍由 CRS、shape、仿射变换和坐标计算 `grid_id`；Catalog 的
`resolution_x`、`resolution_y` 可用于快速筛选 10/30 米产品。

## 6. 数值验收规范

发布门禁至少包括：

1. `target_resolution` 为 20 等非白名单值时，YAML CLI 与 Web API 都拒绝。
2. 30 米 `auto` 解析为最近邻，并保持现有回归测试结果。
3. 10 米 `auto` 解析为双线性，输出存在源像元之间的连续中间值。
4. NoData 哨兵不出现在有效输出中，也不参与有效像元插值。
5. 输出 transform 的像元尺寸严格为 10 或 30 米，x/y 坐标与 Zarr shape
   一致，Catalog `grid_id` 可复算。
6. `ls_map` 输出只包含合法类别/NoData，不出现双线性产生的小数类别。
7. 10 米小样与 30 米基线做统计对照：均值、分位数和有效覆盖率差异需被
   记录；不得将视觉更平滑解释为新增空间信息。

## 7. 建议首轮真实数据实验

选择一个包含 Burst 接缝、NoData 边界和跨 UTM/MGRS 边缘的小时间窗，分别
生产 `30m-auto`、`10m-auto` 和 `10m-nearest` 三组独立输出。比较运行时间、
峰值内存、Zarr 大小、有效像元比例、VV/VH 线性功率统计、dB 分位数、接缝
剖面和 `ls_map` 类别集合。通过数值门禁后，再扩大到一个完整 MGRS 瓦片和
连续 12 个月；全国批处理前必须据 9 倍像元量重新核定容量和并发数。
