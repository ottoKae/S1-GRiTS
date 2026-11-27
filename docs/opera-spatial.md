# 独立 OPERA 点／区域增量工具

入口为 `python -m s1grits.opera_spatial`。它直接读取 OPERA RTC-S1 资产，输出独立点表或区域 Zarr，不运行 MGRS pipeline，不修改已有 MGRS Catalog。

## 环境和配置

使用 Python 3.12，在本分支仓库根目录执行 `pip install -e ".[opera]"`，安装包含 `tifffile` 和 `imagecodecs` 的 Range TIFF 解码依赖。Earthdata 认证沿用 requests 使用的本机凭据，不在配置文件中填写密码或令牌。

最小点配置：

```yaml
spatial:
  mode: points
input:
  path: points.csv
  id_column: id
source_strategy: S0
read_mode: range
workers: 4
output:
  base_dir: output/points
protected_roots:
  - D:/existing_mgrs_archive
```

CSV 列为 `id,lon,lat`。路径相对于 YAML 文件所在目录解析。`protected_roots` 填实际需要保护的生产目录；已有 Catalog／Zarr 下的输出也会被拒绝。首次创建使用空目录，后续必须匹配其 manifest，不能把旧 MGRS 根目录传给新工具。

区域沿用相同 input 定义，并添加：

```yaml
spatial:
  mode: geographic_0p1
grid:
  mode: target_crs
  target_crs: EPSG:32649
  target_resolution: 30
  origin: [0, 0]
```

`geographic_0p1` 输入 AOI 面，选择与之相交的完整 0.1° 单元；`vector_grid` 输入 SHP/GeoJSON 的独立面要素，保留各 ID、孔洞和 MultiPolygon；`point_buffer` 输入点并设置 `spatial.radius_m: 1000`，其网格可使用 `grid.mode: aeqd`。圆跨区域逐像元比较时使用明确的共同 target_crs。

可用 `source.burst_ids` 或 `source.relative_orbits` 明确限制试点来源。限制来源后，输出只代表这些候选的覆盖；未覆盖区域留空，不会自动补入其他 burst。

## 规划、追加与修订

```powershell
python -m s1grits.opera_spatial plan --config points.yaml --start 2026-10-01 --end 2026-11-10
python -m s1grits.opera_spatial update --config points.yaml --start 2026-10-01 --end 2026-11-10
python -m s1grits.opera_spatial repair --config points.yaml --start 2026-10-01 --end 2026-11-01
```

时间范围是 UTC 的 `[start,end)`。`plan` 查询清单并显示来源与全时段预算，不读取影像；超过 50 GB 的执行需检查计划后添加 `--confirm-large-job`，磁盘空间独立检查。

`--inventory complete_snapshot.json` 和 `--static-inventory static_snapshot.json` 可重放完整 ASF 查询快照。省略动态快照时直接查询 OPERA；首次初始化可查询静态来源，之后沿用已冻结引用。更新可能有清单查询流量，“零影像请求”不等于在线发现阶段完全零网络。

`update` 追加新获取。同一已完成更新重跑不会增加观测行。会改变既有时间片的候选标为 `repair_required`；`repair` 才补齐失败、插入历史获取或采用兼容新版本。不能用 repair 改变同一产品 ID 的获取身份。处理版本不兼容时形成独立几何组；新增 burst 改变已冻结候选池时要求新组装版本。

`read_mode` 可选 `range`、`burst_fetch`、`local_cache`，不自动将 Range 失败隐藏为成功。后两者共享任务缓存；默认 Range 下载目标 TIFF 块，不产生整 MGRS 中间产品。区域窗口为最近邻、线性功率域，点为原生包含像元采样。

## 正确读取分析数据

- 点原始表位于 `points/<group>/observations/year=YYYY.parquet`，可含不同 burst／产品版本；分析序列使用同组 `selected/year=YYYY.parquet`。
- S0 为严格固定 owner：owner 无有效 VV/VH＋mask 时留空。S1 显式允许回退，记录 source_index、fallback、实际 product_id 和名义 LIA 差；S1 不承诺严格同源。
- 不同方向、相对轨道、平台及处理版本分组；不要直接拼成一条混合曲线。产品 ID 随获取变化，稳定的 burst 来源索引不变。
- 功率字段是 gamma0 linear power。点的 `vv_db/vh_db` 使用 `10*log10`；区域 VV/VH 保留功率。mask 码保持原义并要求单位 scale/offset；连续层按来源元数据应用 scale/offset 一次。

区域文件位置以 `manifest.json` 的 `region_stores` 为准，年度 repair 后可能指向新的年度文件；不要通过目录 glob 把旧修订版本也拼进去。`source_map_path` 指向对应版本的来源表；`region_geometry` 指向固定 owner／ROI／名义 LIA。

```python
import json
from pathlib import Path
import xarray as xr
from s1grits.provenance import _io_path

root = Path("output/regions")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
entry = next(iter(manifest["region_stores"].values()))
ds = xr.open_zarr(str(_io_path(root / entry["path"])),
                  consolidated=False, mask_and_scale=False)
ds = ds.isel(time=slice(0, entry["committed_length"]))
```

来源编号 0 表示无有效来源；`quality` 区分无源、无效、读取失败、ROI 外和身份未知。LIA 是静态名义角，不是逐获取实测角；Catalog 和运行结果单列 LIA 失败／缺失，未知值保留为空。

首版单写者。写入中的 Parquet/Zarr 不提供并发读取保证；点 manifest 有 `pending_points` 时该更新尚未发布，应重放原任务恢复；区域只读取 manifest 已提交分区及长度。历史 repair 保留原年度分区，不自动清理。

## 点模式自动接入 ANF 和 number of looks

原配置仍只使用LIA。需要辅助层时，在点配置加入：

```yaml
static:
  layers: [lia, number_of_looks, rtc_anf_beta0, rtc_anf_sigma0]
```

新点任务在提交动态记录后自动采集静态层。已有LIA档案首次补层使用下面的显式命令；没有新的动态日期也可以执行，且不会重新读取VV/VH/mask：

```powershell
python -m s1grits.opera_spatial plan --static-only --config points.yaml --start 2017-01-01 --end 2026-01-01 --static-inventory static_snapshot.json
python -m s1grits.opera_spatial repair --static-only --config points.yaml --start 2017-01-01 --end 2026-01-01 --static-inventory static_snapshot.json
```

`--static-only`仅用于已有点档案的plan/repair。日期限制需要补齐的动态来源范围，不过滤STATIC目录的参考日期。可省略`--static-inventory`：已有扩展的冻结产品信息优先复用，否则查询完整STATIC目录。失败项通过repair重试；同一成功任务重放复用缓存。新版STATIC不会自动替换已冻结产品；首次同burst有多个候选时，用`static.reference_products`的`burst_id: 完整产品ID`映射明确选择。

`rtc_anf_beta0`对应gamma0→beta0乘法因子，`rtc_anf_sigma0`对应gamma0→sigma0乘法因子；工具只保存因子，不再次校正回波。`number_of_looks`保留无量纲小数和合法零值，表示面积投影多视的雷达样本贡献，不等于时序景数、独立ENL或热噪声大小。三者是名义静态信息，不是逐景实测值。

`static/manifest.json`保存冻结来源和当前`samples_path`；对应Parquet保留数值、状态、源像元、缩放与版本证据。原动态表和owner保持不变。通过以下入口关联到**当前**selected记录，动态repair之后也不会继续使用旧来源的副表：

```python
from s1grits.opera_spatial.point_static import read_point_static_context

df = read_point_static_context("output/points")
columns = ["point_id", "acquisition_utc", "owner_number_of_looks",
           "selected_number_of_looks", "selected_rtc_anf_beta0",
           "selected_rtc_anf_beta0_status", "selected_rtc_anf_beta0_grid_status"]
print(df[columns])
```

S1回退时，selected辅助值来自实际供值burst。未知或格网不匹配的便捷值为空，状态保留，原始静态值仍可在样本表查到。静态层失败不改变动态有效性，也不触发burst回退；`complete_with_gaps`表示已结束但有明确来源缺口，`partial`表示仍有读取失败。

本轮只增加点模式辅助层；区域Zarr暂不支持这些新层。资产总字节是完整文件体积参考，不能当作重复Range读取的流量上限。

## 验证范围

已做真实 OPERA 三时次试点、重叠 burst 的 S1 回退／恢复，以及本地真实 HTTP TIFF／Zarr 故障测试。尚未做十年大区域容量与性能运行，也未做全轨道、全候选 burst 的区域覆盖验收。临近区域的 Range 请求仍可能重复读取相同 TIFF 块；本轮没有新增全局共享缓存或后台服务。
