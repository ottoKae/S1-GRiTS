# YAML、命令和 Agent skills 如何配合

S1-GRiTS 的计算由同一套 CLI／Python 实现执行。Agent 负责理解任务、选择产品、填写已有参数并检查结果；skill 不实现另一套算法。
本文对应包含视图、多轨月产品和本地分析工具的开发候选。最终发布目标为 V4.0.0，但当前环境的版本及命令应实际检查，不能把目标版本当成已发布版本。CLI／Agent／Web 的逐项边界、旧 Catalog 接入和任务文件位置见[入口支持矩阵](entry_points.md)。

| 用户任务 | 保存的任务描述 | Agent 使用的 skill | 无 Agent 的入口 |
| --- | --- | --- | --- |
| 新建或续跑 Cube | 现有生产 YAML | `s1grits-cube` | `init`、`doctor`、`process_scenes` |
| 查看集合、检查覆盖或恢复目录 | 选定根目录、产品身份和具体操作 | `s1grits-cube` | `catalog inspect/doctor/register/resync` 与 Resolver |
| 使用已有 Cube 做派生或分析 | CLI/API 参数及操作生成的合同／收据 | `s1grits-analysis` | `derive_cube`、分析命令与 Python API |
| 保存计算定义、按需读取或物化 | 已登记的视图 JSON | `s1grits-analysis` | `view create/multitrack/materialize` 与 Resolver |

YAML 继续服务生产配置；分析命令参数和视图 JSON 已有各自语义，不需要统一改成另一个分析 YAML 引擎。
Web 表单将所选参数交给同一 CLI：生产保存 YAML，工具保存 request.json、执行命令和原生回执。它只开放支持矩阵中列出的功能，并不提供所有 CLI 参数的编辑器。Agent 也不需要操纵 Web 才能使用已有 Cube。
两类 skill 都是通用数据工作流，不包含林业应用或私有研究检测器。

## 新用户从哪里开始

下面的具名 skill 示例适用于已安装对应 skill 的环境。新克隆仓库或只安装 wheel 的用户，可以直接让 Agent 先阅读本文及对应命令指南，再通过同一 CLI 执行；无需先安装一个未提供的 skill 包。可用性和版本核对见文末。

没有 Cube、准备生产数据时，向 Agent 提供区域、时间范围、升降轨方向、需要的产品和输出目录。例如：

> 使用 s1grits-cube，为指定瓦片生成 2026 年 1 月的升轨月产品。先生成并检查 YAML，输出到我指定的新目录。

Agent 应使用当前安装版本的 `init` 模板，按任务修改后执行 `doctor`。默认模板中的 `time.full` 是全档案请求，短任务需要替换成明确年份／月份，不能留下互相冲突的时间选项。只生成配置的请求不等于已经要求下载；请求执行时再运行生产命令。单个月份也可能涉及整瓦片和多个完整 burst，需结合资源预算判断任务规模。

没有 Agent 时，执行同样的流程：

```sh
python -m s1grits init job.yaml
# 按配置指南修改区域、时间、产品和输出。
python -m s1grits doctor --config job.yaml
python -m s1grits process_scenes --config job.yaml
python -m s1grits catalog inspect --output-dir /data/output
```

具体参数见当前命令 `--help` 和[配置指南](configuration.md)，不要复制旧版本的默认参数表。
续跑使用兼容的原 YAML 和输出位置，先核对现有处理身份和完成状态；不要为了消除报错而切换覆盖／重建。

已有 Cube 时，提供集合根目录、目标操作、时段／窗口和输出意图。例如：

> 使用 s1grits-analysis，列出这个集合的单轨产品；选定轨道 18 的场景实例，在指定 8×8 窗口生成 Ratio 和 RVI 的虚拟视图，再保存到新的输出目录，保留重放命令。

产品有多个变体时先明确 item、格网和处理身份，不默认取第一条记录。
已有 Cube 分析不需要再次下载或运行构建流水线。可先运行[已有 Cube 教程](getting_started_existing_cube_ZH.md)，再阅读[虚拟视图](cube_views.md)或[多轨月产品](multitrack_monthly.md)。

## 怎样核对结果和重放

保留实际解释器／导入位置、最终 YAML 或确切命令、输入 item ID、时段／窗口、输出位置和原生收据。
结果检查包括实际时间轴、坐标、NoData、科学变量、计数含义和完成状态，而不是仅看命令退出码。
`catalog inspect` 的目录行数不能当作每像元覆盖率。

视图 JSON 保存计算定义；打开时才按需计算，物化才写出实体数组。
`n_obs` 与多轨结果的 `n_contributing_tracks` 含义不同；不能把缺测解释为零后向散射或无变化。
同一配方重放到新的独立目录时，科学签名、父引用和数值应一致；新实体 generation ID 和创建时间可以不同。

目录重建是写操作；`resync --no-stac` 还会删除 STAC 文件，不是只读查看。
父版本改变时旧视图需要明确处理；缺 generation 的父数据只提供元数据检查，不能保证发现像元原位修改。
本地 CCDC 尚未实现；本地 LT-rs／CUSUM 依赖另装的实验后端，不能因缺少依赖自动改用 GEE。

## Skills 的可用性、版本与维护

当前规范源位于工作区 `.agents/skills/s1grits-cube/` 与 `.agents/skills/s1grits-analysis/`。
这两个目录不在本软件仓库中，Python wheel 也不会安装它们；当前仓库未提供其公开安装包。工作区已安装的 skill 可直接使用，修改后 Agent 的发现列表可能需要新会话才刷新。

公开用户现阶段可以：

- 让 Agent 读取本文、[入口支持矩阵](entry_points.md)和当前命令 `--help`，执行相同任务。
- 直接使用 CLI／Python／Web；已有 Cube 从[小样本教程](getting_started_existing_cube_ZH.md)开始。
- 数据交接任务可直接指定本仓库的 [s1-data-handoff](../agents/skills/s1-data-handoff/SKILL.md)；它链接到同一公开交接指南，自动发现取决于 Agent 配置。

本文和两类通用 skill 对应 V4.0.0 候选功能。执行前核对 `python -m s1grits --version`、`s1grits.__file__` 和所需子命令帮助；V3 安装不因能读取 skill 就具备 V4 功能。未来公开导出应从上述规范源生成，随导出记录源码哈希及适用的软件版本，不另维护可编辑副本。

本轮操作验收包含真实单景小窗口的视图／物化重放，以及受控离线 COG 的配置／生产入口／续跑；后者不等同于在线下载或大规模生产验收。
