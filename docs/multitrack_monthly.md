# 多轨月产品组合

这是读取已有单轨月产品的可选工具，不进入默认生产链，也不改变 burst 单景镶嵌。
结果是一个具有独立处理身份的多轨月产品，可先登记为虚拟视图，再显式保存为实体 Zarr。
使用候选代码安装已有的 `views` extra：`pip install -e ".[views]"`。

## 输入

首版接收 `derive_cube(..., monthly="mean"/"median")` 生成的实体月产品。
每个轨道只选一个实例的一个 item ID；该 item 引用整个 store/group，不是只选该目录行的月份。
至少两轨，父产品必须同 tile、方向、格网和可核对的处理阶段。显式列出的顺序保留在身份中。
不同路径或不同 catalog 行引用同一 store/group，不能作为两份独立输入。

程序核对月度处理合同、来源处理签名、单轨身份和实际格网；不同轨道的来源处理签名也必须一致。
不自动混合 mean/median、插值前后或不同滤波配置的月值，不重投影、不进行入射角归一化。
月值可以带其他特征，但本入口只组合 VV/VH。

已有 scenes 可以先使用现有 `derive_cube` 做月合成。两个父产品应指定可比的 UTC 统计区间。
`start`/`end` 是包含端点的时间戳；仅写日期的 `end` 表示当天午夜。
请求整月时可明确写 `2026-03-31T23:59:59.999999999Z`。如果父产品统计区间是部分月份，
只有各父产品在该月的实际统计区间完全一致才可组合；该区间会保存在合同中。

历史生产 smonthly 只有月份标签或 reducer 配置时，不能证明其输入统计区间，本入口会拒绝。
不要为通过校验而手改旧数据元信息。首版没有自动转换这些旧月产品，也不会重新下载或重建数据。
带插值掩膜、已经混轨、同轨多个变体、每月多个时间层均不接受。

## 方法与来源

`method` 必须显式选择：

| 方法 | 数值 | 每像元贡献 |
| --- | --- | --- |
| `first_valid_pixel` | 按输入轨道顺序，选择第一个 VV/VH 同时有效的月值，保留其 dB 值 | 一轨或零轨 |
| `mean` | 将有效配对 VV/VH 转为线性功率，各轨月值等权平均，再转回 dB | 所有实际参与的轨道 |

例如两轨月功率分别为 1、9，原始观测数分别为 2、6：`mean` 为 5，绝不是按观测数加权的 7。
对两个单轨月 median 再取跨轨 mean，也不能解释为原始观测的 mean。
有限 NoData、NaN 和无效功率不参与；有效 0 dB 保留。两极化使用同一贡献集合。

输出包含 `VV_dB`、`VH_dB`、`n_contributing_tracks`（uint32）及每个输入轨道的
`contributes_TK<轨道号>`（uint8，0/1）。这些标记表示实际贡献，不是候选覆盖。
对 first-valid 而言，覆盖但未选中的轨道标记为 0。
输出不伪造 `n_obs`；合同记录每个父 item、轨道、版本证据、每月对应的父时间标签及父 `n_obs` 变量引用。
父 `n_obs` 的值和定义留在父产品中，不能把贡献轨道数当成独立观测数。

按 UTC 日历月份配对，缺失月份和全缺测月份保留；没有来源时 VV/VH 为 NaN、贡献标记为 0。
输出 time 是月初标签，不是实际获取日期。合同保存日历周期 `[起始, 下月起始)` 和实际统计区间。
结果 `track`、`geometry_group_id` 为空；完整轨道序列位于处理合同中。

## Python

假定两个月产品已通过 `register_product` 登记在 `/data/collection`：

```python
from s1grits.analysis import define_multitrack_view, register_view, materialize_view
from s1grits.resolver import CubeResolver

definition = define_multitrack_view(
    "/data/collection", ["TRACK18_MONTHLY_ITEM", "TRACK120_MONTHLY_ITEM"],
    start_month="2026-01", end_month="2026-03", method="mean",
    window=[100, 200, 16, 16],
)
record = register_view("/data/collection", definition)
resolver = CubeResolver("/data/collection")
with resolver.open(record) as cube:
    small = cube.isel(time=0, y=slice(0, 2), x=slice(0, 2)).compute()

saved = materialize_view(
    "/data/collection", record["item_id"], "/data/collection/products/multitrack",
    chunk_size=128,
)
```

定义、登记和打开读取坐标及元数据，VV/VH 按需计算。物化复用相同归约函数和已有分块 writer。
写到集合内的新子目录时自动登记；独立输出根目录也可直接用 Resolver 读取。
同一集合内每个 item ID 对应一个资产位置；对同一配方做不同 chunk 的副本测试时使用独立输出集合。

## CLI

```bash
s1grits view multitrack --catalog-root /data/collection \
  --item-ids TRACK18_MONTHLY_ITEM TRACK120_MONTHLY_ITEM \
  --start-month 2026-01 --end-month 2026-03 --method mean \
  --window 100 200 16 16 --output /data/collection

s1grits view materialize --catalog-root /data/collection \
  --item-id PRINTED_MULTITRACK_ITEM_ID \
  --output /data/collection/products/multitrack --chunk-size 128
```

## 验证与边界

小文件测试包含两轨、三个月、2×2 像元，两种原始轴顺序、配对缺测、空月、输入次序、
不同 n_obs、逐轨贡献、两种 chunk、CLI、目录重建、STAC、迁移与父版本变更。
虚拟和实体结果逐数组比较，父文件保持不变。

父数据必须在打开到计算期间保持不变。缺少 generation_id 的历史父数据明确标记
`metadata_only` 并发出警告；元数据检查不能发现像元原位修改。参见[视图生命周期](cube_views.md)。
本入口不提供嵌套视图、在线 GEE、多轨入射角校正、自动刷新或大陆规模验收。
