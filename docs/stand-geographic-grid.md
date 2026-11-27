# 小班取数 GeoParquet 格网

S1-GRiTS 新增 `grid build` / `grid query`，在既有 MGRS 生产格网之外建立地理坐标取数索引。
默认 0.01°，边界与 Tessera 原生 0.1° 格网对齐，每个父格细分 10×10。
这不是把栅格像元分辨率改成 0.01°；现有 10/30 m 像元、轨道分组及 Zarr 生产保持原有契约。

Tessera 原生瓦片尺寸及中心坐标规则见
[GeoTessera 官方文档](https://geotessera.readthedocs.io/en/latest/geotessera.html)，
S1-GRiTS 的索引实现位于 `src/s1grits/stand_grid.py`。
Parquet 保存索引，Tessera 的数值数据仍在单独的数组文件中。子格本身不会改变上游下载单元。

## 使用

```powershell
s1grits grid build --stands "samples/stands.gpkg" --layer stands --step 0.01 --output-dir "output/stand_grid"
s1grits grid query --index-dir "output/stand_grid" --stand-id 1
```

输出目录必须为空。默认 ID 为源文件 FID 的字符串表示；`--id-column` 仅接受无空值、无重复的字段。
GPKG 重建可能改变 FID，应把源 SHA256 和索引版本一起保存。

| 文件 | 内容 |
|---|---|
| `cells.parquet` | EPSG:4326 多边形、稳定整数行列 ID、经纬度范围、Tessera 父格、面积及小班数量 |
| `stand_cells.parquet` | 全部正面积相交的小班—格子关系、交集面积及小班面积比例 |
| `stands.parquet` | 源属性、修复后几何、源 FID、几何修复标记 |
| `cell_mgrs.parquet` | 格子—MGRS 候选来源关系 |
| `cell_bursts.parquet` | 格子—burst 候选来源关系 |
| `manifest.json` | 源路径与 SHA256、面积 CRS、格网定义 |

`query` 使用 Parquet 列过滤返回全部关联格子、父格、MGRS 和 burst。
候选 footprint 不代表所需日期存在影像，也不替代具体产品/轨道筛选。
同一小班涉及多个 MGRS 时，分别查询相关产品；不要混合升降轨或不同轨道的观测。

对已选择的单个产品，可先按时间和波段选择，再裁剪小班：

```python
from s1grits.resolver import CubeResolver
from s1grits.stand_grid import query_stand, clip_stand_window

index = "output/stand_grid"
plan = query_stand(index, "1")
# 由 plan['mgrs_tile_id'] 查询并明确选择一个产品、方向及轨道的 catalog record。
# resolver 为既有 CubeResolver；record 为所选记录。
ds = resolver.open(record, chunks={})
patch = clip_stand_window(ds, index, "1", crs=record["crs"])
# patch 保持源像元；需要时再 load()/统计。完成后关闭 ds。
```

`clip_stand_window` 先裁剪包围盒再做多边形掩膜，避免为整个 MGRS 构造小班掩膜。
默认使用像元中心归属规则；极小小班可能无中心像元。可显式选择 `all_touched=True`，
但这会纳入边界相交像元，并不等同于面积加权统计。函数只返回当前 cube 的覆盖部分。

## 定义与限制

- 格子坐标系为 EPSG:4326；面积使用区域中心的 LAEA 等面积投影，避免把平方度当面积。
- 索引以 `deg100_x11112_y3125` 为例：西南角为 (111.12, 31.25)，尺寸 0.01°。
- 正面积相交才建关系，边线接触不算；不能按小班中心点只选一个格子。
- 允许 0.1° 的整数等分，按研究区生成稀疏索引。超大包围盒受数量限制，跨日期变更线需拆分。
- 面积为各小班面积之和，并非重叠消除后的林地净面积；地类代码应依据数据字典筛选。
- 当前没有新增按子格网生产/下载的执行器。`roi.wkt` 仍是现有流程的区域选择条件，不能据此宣称只生产 0.01° 小块。
- 查询与裁剪读取量仍受原 Zarr chunk 大小限制。512×512 的 30 m chunk 约 15.36 km 宽，
  一个约 1 km 的窗口仍可能读取完整 chunk。Parquet 本身不会自动减少上游网络字节数。
- 尚无真实 ASF 下载提速测试。建议先用真实产品验证窗口读耗时、解压字节数及热点复用率，
  再决定是否创建小块缓存或新增生产后端。

完整区域分析入口为 `scripts/analyze_stand_grid.py`，输出四种格网比较、地类统计、图表及索引。
