# 历史数据只读元数据导出：P1/P2 使用说明

`python -m s1grits.provenance build` 和 `verify` 为明确选定的本地 Zarr 生成、核验独立的 **metadata_only 配套目录**，记录已有结构、坐标、来源声明与未知项；不下载、不改写影像、不重建 Catalog，也不生成完整影像 ZIP。另有受限的 [`attach` 入库入口](#attach-historical-metadata)，用于把专门准备并核验的历史 release 附入现有数据根；下面第 1—6 节仍说明独立导出。

生产追加／替换留痕由受限的 [S1 月度工作流](PROVENANCE_WORKFLOW_ZH.md)负责；配套 [GRiTS-Resolver](https://github.com/ottoKae/GRiTS-Resolver/blob/main/docs/PROVENANCE_READING_ZH.md)支持发布 v1/v2 读取保护。这些能力与本只读导出入口分开使用，通用规则见[数据交接说明](DATA_HANDOFF_ZH.md)。

## 1. 开始前

在 S1-GRiTS 仓库根目录使用可运行本仓库的 Python 环境；该功能需要已有的 zarr、pyarrow、pandas、numpy、numcodecs 等依赖。无需为了导出改动生产环境或存储格式。

- 明确数据根和需要说明的 store；`--store` 使用相对数据根的路径、正斜杠，可以重复指定。不自动扫描并导出整个数据集。
- 导出根必须与生产根相互独立：不能相同，不能是生产根的子目录，也不能包含生产根。示例目录只是占位，请换成实际路径。
- 先确认所选源数据已停止写入，再使用 `--quiescent`。这个参数是操作者的确认，**不会暂停下载、writer 或其他程序**。
- 源根存在 `provenance/pending/*.json` 时，当前 P2 保守地拒绝整个根的核查。应由相应生产任务完成恢复与收尾，不要手工删除 pending 绕过保护；缺少历史证据或 SCL 本身不生成这种写入保护。
- 若源根已有生产 `provenance/current.json`，应复用其真实 generation 和执行证据，不能通过历史导出将它们改成 unknown；当前只读入口不负责把历史观察合并进生产 release。
- `--dataset-id` 使用稳定、可公开的标识，最长 128 字符，只含字母、数字、点、下划线和连字符，首字符为字母或数字。搬运后沿用同一标识。
- 不在 `--note` 中填密码、私人路径或认证信息。它是原样保留的说明，不是自动脱敏的运维日志。

## 2. 首次生成

以下 PowerShell 示例从两个月度 store 导出 2025 年的元数据。在仓库根目录运行：

```powershell
$env:PYTHONPATH = (Resolve-Path "./src").Path
$datasetRoot = "D:/datasets/ecuador_s1"
$metadataRoot = "D:/deliveries/ecuador_s1_metadata"

$buildArgs = @(
    "build",
    "--dataset-root", $datasetRoot,
    "--output-root", $metadataRoot,
    "--dataset-id", "ecuador-s1-archive-v3",
    "--store", "18NUF/smonthly_ASCENDING/zarr/s1grits_smonthly_18NUF_ASCENDING_TK120.zarr",
    "--store", "18NUF/smonthly_ASCENDING/zarr/s1grits_smonthly_18NUF_ASCENDING_TK47-48.zarr",
    "--start", "2025-01-01",
    "--end", "2025-12-31",
    "--quiescent"
)
python -m s1grits.provenance @buildArgs
```

首次发布**省略** `--expected-release`，表示预期目标目录尚无发布版本。不要传入字符串 `None`。如果目录已经有 `current.json`，这个首次命令会拒绝覆盖，须按下一节明确基准版本。

`--start`、`--end` 均包含边界：场景产品按 UTC 日筛选，月度产品按日历月筛选；省略则纳入所选 store 的全部时间层。若明确选择了 static store，其静态层不受时间筛选影响。时间坐标保持原值，不把月层代表时间改成月初。

S2 原生场景库也可显式指定相应 store；每个分辨率组独立定位时间与场景身份，不假设 10 米和 20 米索引相同。这是元数据盘点能力，不是生成 SCL 或签发光谱—SCL 配对的入口。

## 3. 重复运行与修订说明

先读取已经发布的版本：

```powershell
$previousRelease = (
    Get-Content -LiteralPath "$metadataRoot/provenance/current.json" -Raw |
    ConvertFrom-Json
).metadata_release_id

python -m s1grits.provenance @buildArgs --expected-release $previousRelease
```

当选取范围、源盘点、说明、工具源码与依赖等均未改变时，返回 `unchanged: true`，不会重复创建输出层。仅修订说明可以在相同命令末尾添加 `--note "补充本次交付范围说明"`：新元数据 release 保留旧 release，不改影像。

发布过程使用导出根的排他锁，并核对预期元数据基准。若其他任务已经发布了新版本，旧基准会报错；先检查新版本，再决定是否重做，不要自动跳过冲突。导出锁只协调这个配套目录，不锁住生产 writer。

输出的 `metadata_release_id` 与 `data_revision` 含义不同：前者标识说明版本，后者绑定此次所选输出及已有数据结构见证。工具或依赖更新可产生新的说明版本；这不表示重写了影像，也不能替代未来生产代次记录。

当前每次 build 按本次显式选择生成完整的当前元数据快照，**不会自动合并上一次选择**。改变 store 或日期范围时，应明确新快照的范围；旧范围仍保留于旧 release。实际生产发生追加或替换后，应在 writer 完全停止且原数据已稳定时重新盘点，不能把旧说明直接沿用成新批次执行证据。

## 4. 验证与搬运

只检查配套目录的文件摘要、契约类型、引用与发布指针：

```powershell
python -m s1grits.provenance verify --output-root $metadataRoot
```

再与源数据核对结构和坐标；执行前仍须确认源数据停止写入：

```powershell
python -m s1grits.provenance verify --output-root $metadataRoot --dataset-root $datasetRoot
```

不传 `--dataset-root` 时，`source_checked` 为 false；传入后会按 release 中保存的选择范围重新盘点。发现版本不符就报错，需要重新 build。两种方式都不会验证影像块，返回的 `pixel_chunks_checked` 为 false。

搬运时保持 store 的相对路径结构，并显式传入搬运后的数据根。配套目录不包含影像，需要和对应影像一起交给接收者。仅搬运配套目录，只能称元数据补充，不能称完整数据交付。

## 5. 实际输出结构

```text
ecuador_s1_metadata/
  DATASET_README.md
  provenance/
    current.json
    releases/
      meta-<摘要>/
        dataset.json
        variables.json
        outputs.parquet
        sources.parquet
        source_links.parquet
        evidence/
          data_witnesses.json
          observer.json
          stores/<标识>.json
        limitations.json
        verification.json
        FILES.sha256
```

工具运行时可能使用导出根的锁和 staging；它们不是影像或历史生产记录。正式读取从 `current.json` 定位不可变 release。不要把这三张增强表改名为原 `catalog.parquet`，也不要覆盖旧 Catalog、sidecar 或 SCL 发布文件。

| 文件 | 解释 |
| --- | --- |
| `dataset.json` | 数据集标识、选择范围、两个版本 ID、观察时间与表行数 |
| `outputs.parquet` | store、组、变量范围、实际时间和索引、逻辑输出身份与证据引用 |
| `sources.parquet` | 从已有记录取得的源资产身份、公开地址及声明的 scale/offset |
| `source_links.parquet` | 输出到资产的关联；目前的 selected 声明不能证明资产实际参与每次运算 |
| `variables.json` | 当前 dtype、shape、chunks 与已有属性；不能证明的物理语义保持未知 |
| `evidence/observer.json` | **本次盘点工具**的源码摘要、Python 与依赖版本，不是历史生产者的运行版本 |
| `evidence/data_witnesses.json` | 已检查的原元数据文件及坐标值摘要，不包含影像块摘要 |
| `limitations.json` | 历史执行、实际数值变换与替换检测等缺口 |
| `FILES.sha256` | 本 release 文件摘要，排除清单自身与外部 current，避免循环引用 |

三张 Parquet 使用固定 schema v1：`time_index` 为可空 int64，`variable_scope` 为字符串列表，声明的 scale/offset 为可空 float64；其余契约字段为可空字符串并受语义约束。空来源表仍有明确类型。未经证据确认的 `generation_id`、`run_id`、`processing_id` 为 null，并有 `unknown_reason`；不能填成 0、false 或按当前默认值猜测。

## 6. 能确认到什么程度

P2 读取现有 Catalog、提交 sidecar、Zarr 属性与坐标，在发布前后比较盘点结果。它能发现所检查结构或关联发生变化，不能保证发现同时间、同格网的像元替换，更不能证明历史数值变换真正执行过。没有读取影像块，所以不是全像元校验，也不是生产与元数据的并发一致快照。

对 S1，不能仅凭 `VV_dB` 推断输入域或 nanmedian 所在域。对 S2，源元数据中的 scale/offset 与本地实际执行校正必须分开。来源资料不足时允许 partial_evidence；当前程序不虚构历史 runs，也不把当前工具代码摘要登记为生产代码。

本入口不执行生产写入、追加／替换提交恢复或 SCL 新配对；这些操作按前述 P3 工作流实施，读取端按 P4 说明使用。自动完整影像文件夹复制及通用 ZIP 导出仍未提供。已有影像的正常生产功能不受本工具替代或接管。

扩大历史覆盖时先冻结 store／时间范围，再报告成功、失败和未知项。新增伴随目录仍是 metadata_only，覆盖扩大不等于历史处理过程已全部查明。

<a id="attach-historical-metadata"></a>

## 7. 将已核验的历史元数据附入现有数据根

`attach` 用于把已准备并核验的历史 release 放到已有影像根的 `provenance/` 中，使“影像＋说明＋处理记录”位于同一个根目录。它不复制或改写影像、Catalog、坐标、原 sidecar 或 SCL 绑定，也不替代正常生产追加／替换。以下前提必须全部满足。

### 7.1 入库前提

- 源 `metadata_root` 与目标 `dataset_root` 必须是两个独立、已经存在的目录，不可互相包含，不可通过 symlink／junction 访问。
- 源 current 所指 release 必须是 `execution_kind=historical_import`，且通过 `verify` 与目标影像的结构核对。两端不得存在活动 provenance pending。
- 源 release 的 `dataset.json` 必须含 `attachment_readme_sha256`，绑定源根中专门编写的 UTF-8 中文 `DATASET_README.md`。说明应适用于“此根已有影像”的交付方式，写明范围、读取方法和未知项。**普通 P5 配套包不能直接套用**：缺少该绑定时 attach 会拒绝；应先另行准备并验证新的不可变 release，不手工补改已发布版本或伪造摘要。
- 先真正停止写入所选数据的下载、合成、修复及其他 writer，再传 `--quiescent`。该参数是操作者确认，不会自动停任务。attach 在核对目标基准至发布期间持有目标根的 `output_lock(dataset_root)`（`.s1grits.lock`），与使用同一根锁的操作互斥；它不能拦住绕过该锁的外部写入。
- 目标已有 current 时，仅允许更新同一 dataset ID、同一 data revision 的历史元数据。**不能覆盖生产 current，不能把实际生产的 generation 降为 unknown，也不能用 attach 改变已发布 data revision。** 实际新增／替换应通过 P3 工作流记录新代次，再由 P4 重载。

### 7.2 首次附入与重复执行

以下路径是占位；`$metadataRoot` 必须指向满足上述 README 绑定约定的准备目录。首次附入要求目标没有 current，省略 `--expected-release`：

~~~powershell
$metadataRoot = "D:/deliveries/ecuador_s1_attachment_ready"
$datasetRoot = "D:/datasets/ecuador_s1"

python -m s1grits.provenance verify --output-root $metadataRoot --dataset-root $datasetRoot
python -m s1grits.provenance attach --metadata-root $metadataRoot --dataset-root $datasetRoot --quiescent
~~~

目标已有历史 current 时，先读取目标当前基准，再执行。这里的预期版本来自**目标数据根**，不是待附入的源目录：

~~~powershell
$expectedRelease = (
    Get-Content -LiteralPath "$datasetRoot/provenance/current.json" -Raw |
    ConvertFrom-Json
).metadata_release_id

python -m s1grits.provenance attach --metadata-root $metadataRoot --dataset-root $datasetRoot --quiescent --expected-release $expectedRelease
python -m s1grits.provenance verify --output-root $datasetRoot --dataset-root $datasetRoot
~~~

基准已变化时拒绝发布；不要自动跳过冲突。相同 release 重复执行返回 `unchanged: true`，不会重复写入影像或创建新代次。旧 release 保留，不能把换说明版本解释成数据回滚。

目标没有 `DATASET_README.md` 时，独占创建已绑定的说明；已有说明原样保留，不覆盖。返回的 `readme_preserved` 表示是否保留已有文件。若原说明尚未提及 provenance，应结合入库报告和源目录的专门说明阅读，不能把“已有文件”当作“内容已自动更新”。

入库仅复制选中的不可变 release 及其列明的证据，最后更新 current；源配套目录及其旧版本保留。源目录中未被该 release 引用的独立补充材料不会自动纳入，交付前应确认所需证据已在 release 中。

### 7.3 从同一根读取

附入后，支持 P4 的 Resolver 可直接发现数据根内的 provenance，无需再传 `metadata_root`：

~~~python
from grits_resolver import ProvenanceView

view = ProvenanceView("你的影像数据根", required=True)
info = view.explain(
    store_path="18NUF/smonthly_ASCENDING/zarr/s1grits_smonthly_18NUF_ASCENDING_TK120.zarr",
    zarr_group="",
    time_index=0,
)
~~~

store、组和索引按当前 `outputs.parquet` 选择。release 的 `delivery_kind=metadata_only` 描述这一份元数据 release 不含影像，并不表示附入后的数据根没有原影像。交付仍应保留整份原数据目录及新增说明，而不是只发送 provenance 子目录。

这一操作改善文件组织与可核验性，不提高证据等级：S1 历史输入域、合成域及准确代码版本，或 S2 实际是否执行 scale／offset，仍以具体证据和 limitations 为准。
