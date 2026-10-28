# S1 月度工作流：可选生产记录与增量恢复

更新：2026-10-28，3.0.1rc1 候选版。此功能接在现有 `process_scenes` 流程上。默认关闭，启用后把来源、实际使用情况、写入代次和 Catalog 发布记录保存在影像旁边。合成算法及数值转换沿用原 writer；本候选同时包含有限 nodata 和分极化有效次数修复，升级不会重写历史影像。

## 1. 开关和最小启动方法

复制 [完整配置示例](../config/provenance_monthly.example.yaml)，修改输出目录、瓦片和年月。最关键的新增配置只有：

```yaml
metadata:
  provenance:
    enabled: true
    dataset_id: ecuador-s1-monthly
    run_id: append-202501-v1
```

- `enabled: false` 或不提供该项：使用原工作流。仍会拒绝进入尚有未完成生产记录的根目录。
- `dataset_id`：这套数据的稳定身份；后续月份继续使用同一个值。
- `run_id`：本批任务身份；同一任务中断重跑保持不变。新月份、替换任务、参数或候选来源改变时使用新值。
- 两个 ID 只能使用字母、数字、点、下划线和连字符，最长 128 字符；首字符为字母或数字。

从仓库安装的环境运行：

```powershell
s1grits process_scenes --config config/provenance_monthly.example.yaml
```

该命令会进行真实查询和下载，需自行配置现有 ASF 访问环境。不要把认证信息写进交付记录。代码支持不等于已经对生产大库实施：本轮验收使用小型本地 COG，没有发起真实 ASF 下载。

首轮范围固定为：显式年月或日期区间、单个升降轨方向、VV/VH、ARDC、仅月合成、按月分批、瓦片串行、blockwise 单线程。支持多月、多轨道组及多个依次处理的瓦片；不支持 `time.full` 动态探测起点。关闭空间去斑、额外特征、COG/预览、static 后处理、预取、lazy/windowed 读取和 spill；不允许重建不兼容 store。示例已落实这些开关。不符合范围会在查询、下载前拒绝。

现有 Catalog 必须通过规范 v8 校验。工具不会为了继续任务而自动重建旧 Catalog。已有网格、身份、变量布局不兼容时也会停止。

## 2. 追加、替换与断点

| 需求 | 配置与行为 |
| --- | --- |
| 新增月份 | 新 `run_id`；`existing_store: resume`、`existing_month: skip`；新层产生新 generation |
| 已有月份保持原样 | `existing_month: skip`；记录跳过，不把旧影像伪称为本批新生产 |
| 替换指定月份 | 缩小时间范围、使用新 `run_id`，设 `existing_month: overwrite`；output_id 保持、generation 改变 |
| 同一任务断点继续 | 用原配置、原 `run_id` 重跑；先恢复已提交的记录，再处理未完成组 |
| 整批已完成后重跑 | 验证当前记录，直接返回；不重新查询 ASF、下载或写入数组 |

替换会实际改变目标月影像；生产记录不备份旧像元。旧 release 留作历史证据，不能用旧指针恢复已被覆盖的数据。其他月若因原 writer 的替换操作移动索引，记录同步更新索引，保留其身份和 generation。

批次清单冻结每个瓦片的候选身份与分组；任务参数包括查询筛选和内存/失败容忍配置的摘要。具体写入参数、输入数组摘要及执行代码摘要进入逐组 intent。此月度路线不消费 product registry 的自定义产品覆盖来修改像元处理。

每个“store × 月份”独立完成发布。整批不是一次全有或全无的事务；中途失败时可能已有若干组成功。批次清单明确保存完成与跳过状态；没有可用输入的组不会伪造输出。

## 3. 随影像保存哪些记录

```text
dataset_root/
  catalog.parquet
  <tile>/catalog.parquet
  <tile>/smonthly_<direction>/zarr/<store>.zarr/
  DATASET_README.md
  provenance/
    batches/<run_id>.json
    pending/<operation_id>.json
    runs/<operation_id>/
      intent.json
      receipt.json
      catalog_plan.json
      catalog_receipt.json
      catalogs/0.parquet
      catalogs/1.parquet
      completed.json
    releases/<metadata_release_id>/
      dataset.json
      outputs.parquet
      sources.parquet
      source_links.parquet
      variables.json
      limitations.json
      evidence/...
    current.json
```

`pending` 只在未完成时保留；`runs`、`releases` 及批次清单保留历史。`catalogs` 是该操作恢复用的已冻结小型目录表，不是影像副本。

- 来源：资产身份和脱敏 URL，精确对应 loader 成功输入的位置。多个 burst 在同一时间采集，也按独立源行配对，不通过时间猜测。
- 使用情况：区分候选/未进入计算、选中及实际进入月合成计算的 `used`；下载失败、profile 不合格或 QC 排除的资产不会冒充 used。
- 数值处理：逐月 receipt 的 `numeric_processing` 保存实际对齐、nodata 处理、合成、对数转换、裁剪和写入；`input.sources` 保存能取得的源单位、scale/offset、产品版本及真实读取步骤。读到声明不等于应用了校正，未知的上游物理量仍保留 unknown。
- 写入证据：输入数组摘要、参数、相关源码和依赖版本、真实时间坐标、输出身份及 generation。receipt 检查写入后结构，不是生产全像元校验和。
- Catalog：分别记录 tile/global 的基准和目标摘要、冻结恢复副本；最后的 metadata release 绑定本次证据。仅恢复 Catalog 不凭空生成新像元代次。

原 writer 对输入数值作线性域合成，再执行 `10*log10` 写入 VV_dB/VH_dB；非正值按原实现处理。这里说明本程序的运算路径，不把历史数据或上游物理量推断成已实证。未启用新记录的旧月份不会自动获得历史生产证据。

历史补录随增量发布按 `output_id + generation_id` 继承：未变化的月层保留单位及处理依据，新增／替换层使用自己的新 receipt。`variables.json` 和 `limitations.json` 用 `historical_output_generations` 标明说明适用的输出及代次；store 的 `historical_context.output_generations` 使用相同口径。重新盘点取得的 shape、月份顺序、网格及原始属性仍以当前结构为准。旧补录首次衔接时绑定前一版中仍存续的输出，此后范围只随替换缩小，不扩展到新数据。混合历史与新生产月份时，应配套使用支持这些字段的更新版 Resolver。

3.0.1rc1 对带必需范围的新包自动使用发布语义 v2，并支持继续读取旧 v1。升级后的数据根在发布和 attach 时均不自动降级；Parquet 表及 data_revision 算法仍为 v1，纯元数据改版不产生新的影像版本。先安装并验证支持 v2 的 GRiTS-Resolver 0.3.1rc1，再交付新包；没有新增配置开关。当前历史库无需为软件更新而立即重发。大库成本见本文第 5 节。

这些记录是来源和处理说明，并非逐像元贡献地图。`used` 表示进入运算，不表示对每个像元都有贡献。

## 4. 发布与失败恢复

所有操作复用现有数据根锁。顺序为：

**源声明与有效输出预检查 → 冻结 intent/pending → 原 writer 写影像 → 持久化 receipt → 协调 tile/global Catalog → 发布 metadata release/current → 标记完成并清 pending。**

明确的 dB／幅度声明与线性功率输入要求冲突，或目标月份没有有效输出时，在创建写入意图前拒绝；不自动换算，也不删除旧月层。该类拒绝不阻止后续新 run 的有效任务；已开始实际写入的失败仍按下表保留 pending。

Catalog 文件逐个原子替换；整体一致性由 pending、冻结计划和最终 current 约束，不能把两个单文件替换称为跨文件原子事务。

| 中断状态 | 重跑行为 |
| --- | --- |
| 已有 receipt，Catalog 尚未全部完成 | 从冻结副本补齐；不再下载、合成或改像元 |
| current 已发布，完成标志或 pending 尚未收尾 | 核对本次发布身份后只收尾 |
| 已开始写像元但没有持久 receipt | 保留 pending 并停止；不能仅凭月份存在就自动续写、删除或回滚 |
| Catalog 出现既非旧版也非目标版的第三种内容 | 拒绝恢复，保留现场 |
| current 已变为无关版本 | 在改动 Catalog 前拒绝恢复 |

读取方应在批次停止、根锁释放且没有 pending 后重新打开。Resolver 已接入代次失效保护及数值记录校验；不支持边写边读或跨版本继续使用旧缓存。也不要在 pending 存在时手工运行 Catalog resync 或删除记录。

Windows 深层证据路径使用扩展路径读取，并有超过 260 字符的发布/恢复测试；这不代表所有第三方影像驱动的任意长路径都已验证。

## 5. 离线验收与当前边界

安装测试依赖后，可复现小库流程和恢复检查：

```powershell
python -m pytest tests/test_provenance_workflow.py tests/test_provenance_incremental_cost.py -q -p no:cacheprovider --basetemp .test-tmp/provenance
```

`--basetemp` 必须是可清空的专用测试目录。测试使用本地合成 COG、真实解码/QC/月合成/Zarr/Catalog，仅替代网络和注入故障；不验证 ASF 网络、账户权限或上游定标。

已完成追加／替换／skip、三处提交中断恢复、v1/v2 兼容与隔离目录迁移测试。数据实证仍需随实际交付目录提供；源码仓库不包含私人运行日志、数据或 SSH 配置。

当前性能边界：112 个 store、6,063 个初始输出、32×32 合成像元的 HDD 校准中，前三次追加分别约 46.78、47.90、48.98 秒，伴随元数据累计约 117 MiB。每次仍有多次全库结构检查与旧证据复制；预计超出两小时预算后，在稳定提交点停止。未完成 12 轮，不代表全年性能合格，不能外推整景吞吐。先用小批次测量本机成本；持续全库生产需要先优化并复测。只读解释不受该写入吞吐结论替代。
