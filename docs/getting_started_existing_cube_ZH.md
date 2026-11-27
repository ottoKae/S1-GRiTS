# 从已有 Cube 开始

这条路线使用本地 Cube 生成独立的月度产品和 Ratio／RVI，不需要下载 burst、配置 GEE 或使用 Agent。计算调用现有 `derive_cube`；原 Cube 保持只读。

本文对应包含 `tools/create_demo_cube.py` 的开发候选源码，不代表 PyPI 的 V3.0.0 已提供这些命令。先按[安装说明](installation.md)准备 Python 3.12 环境，在本候选源码根目录安装并确认实际导入位置：

Windows／Conda 安装需先激活所创建的环境，使 PROJ／GDAL 能找到对应的数据目录。如果出现 `Valid PROJ data directory not found`，先检查环境激活和解释器路径，再运行教程。

```sh
python -m pip install -e .
python -c "import s1grits; print(s1grits.__version__); print(s1grits.__file__)"
python -m s1grits derive_cube --help
```

如果命令不存在，先核对版本与解释器。不要仅凭版本号假定本地未发布功能已经可用。下面的命令从同一源码根目录执行，PowerShell 和常用 Unix shell 均可使用。`demo-run` 是示例工作目录，可统一替换成自己选定的位置。

## 1. 准备独立小样本

已有数据的用户可以跳到第 2 步，将 `demo-run/input` 替换为自己的 Cube 根目录，并使用自己的 item ID、时段和窗口。没有数据时运行：

```sh
python tools/create_demo_cube.py --output demo-run/input
```

脚本只生成确定性人工数据，拒绝覆盖已有目录。输入为 4 个日期、4×5 个 30 m 像元、VV_dB／VH_dB 两个变量；日期是 2026-01-01、01-13、01-25 和 03-01。17MPU、轨道 18 和坐标仅演示存储结构，**数值不是该地区的真实观测**，不能用于精度评价。

普通像元的 VV 线性功率依次为 1、2、9、4，VH 是 VV 的四分之一。像元 `(x=0,y=0)` 全缺测，`(x=1,y=1)` 缺少第二次观测，2 月无观测。生成器随源码交付，样本生成在用户指定目录；样本数据、论文和研究结果不作为 Python 包资源。

## 2. 查看 catalog 并明确选择父对象

```sh
python -m s1grits catalog inspect --output-dir demo-run/input
python -c "from s1grits.resolver import CubeResolver; r=CubeResolver('demo-run/input'); print(r.catalog[['item_id','tile_id','product_type','track','processing_signature']].to_string(index=False))"
```

本例的 item ID 是 `demo-scenes`。真实目录可能包含多个轨道、产品和处理实例，不能直接取第一行。选定一个完整的 scenes 或 monthly Zarr 对象；`item_id` 选择其引用的整个 Zarr，随后用时段筛选内部时间轴。不会把多个轨道自动融合。

`catalog inspect` 汇总的是 catalog 行及其日期，不读取 Zarr 内部完整时间轴，也不证明每个像元都有观测。本例一行引用四个日期，实际时序和缺测应以下面的数据读取及输出 `n_obs` 为准。

读取该对象中一个像元的原始时序：

```sh
python -c "from s1grits.resolver import CubeResolver; r=CubeResolver('demo-run/input'); row=r.catalog.loc[r.catalog.item_id.eq('demo-scenes')].iloc[0]; ds=r.open(row); print(ds[['VV_dB','VH_dB']].isel(x=2,y=2).to_dataframe().to_string()); ds.close()"
```

本例在 `(2,2)` 的 VV_dB 约为 0、3.010300、9.542425、6.020600。0 dB 是有效值。

## 3. 生成独立月度产品

```sh
python -m s1grits derive_cube --catalog-root demo-run/input --item-id demo-scenes --output demo-run/monthly-mean --start 2026-01-01 --end 2026-03-31T23:59:59Z --features ratio rvi --filter none --monthly mean
```

输出目录必须不存在，且与父 Cube 分离。重跑时换一个输出目录，不删除或覆盖原始数据。日期按 UTC、两端包含；不写时刻的日期代表当天零点。真实大 Cube 首次运行建议增加 `--window 0 0 32 32` 等适合输入大小的窗口，参数为 x 偏移、y 偏移、宽、高。

这次执行顺序是：父 VV/VH → 线性功率月均值 → 不滤波 → 计算 Ratio／RVI。`n_obs` 统计配对有效的父场景数量，不是 burst 数。RVI 是双极化代理量；具体参数、处理顺序和空间滤波的内存限制见[派生指南](cube_derivatives.md)。

## 4. 打开结果与来源记录

```sh
python -c "from s1grits.resolver import CubeResolver; r=CubeResolver('demo-run/monthly-mean'); ds=r.open(r.catalog.iloc[0]); print(ds[['VV_dB','Ratio','RVI','n_obs']].isel(x=2,y=2).to_dataframe().to_string()); ds.close()"
python -c "import json; from pathlib import Path; r=json.loads(Path('demo-run/monthly-mean/derivation.json').read_text()); print(json.dumps(r['contract'], indent=2))"
```

第一个命令仅针对本例新建的单对象输出使用第一行。预期 `(2,2)` 的结果为：

| 月份 | VV_dB | Ratio | RVI | n_obs |
| --- | ---: | ---: | ---: | ---: |
| 2026-01 | 约 6.020600 | 0.25 | 0.8 | 3 |
| 2026-02 | NaN | NaN | NaN | 0 |
| 2026-03 | 约 6.020600 | 0.25 | 0.8 | 1 |

1 月在线性功率域得到 `(1+2+9)/3=4`，转换为约 6.020600 dB。NaN 表示无有效数据，不能解释为零后向散射。`(1,1)` 的 1 月计数为 2；`(0,0)` 所有月份的计数为 0。

输出包含 Zarr、根目录／瓦片 catalog 和 `derivation.json`。来源记录包括父 item、generation、网格、时间范围、输入指纹与操作参数。更改 `--monthly median` 并另写到 `demo-run/monthly-median`，普通像元 1 月约为 3.010300 dB；处理签名也不同。输出可继续作为后续分析的父 Cube。

## YAML、命令与 Agent 的关系

两类通用 skill 的职责、配置／续跑与无 Agent 重放流程见 [Agent 工作流说明](agent_workflows_ZH.md)。

生产 YAML 保存新建 Cube 的任务配置；CLI／Python 执行计算。当前派生入口使用命令参数，并将生效的处理约定写入 `derivation.json`，**不接收任意分析 YAML**。已有 Cube 分析无需先执行 `process_scenes`。新建 Cube 的路线见[配置指南](configuration.md)及 `python -m s1grits init --help`。

Agent 的通用 `s1grits-analysis` skill 负责识别输入、调用相同命令并核对输出，不实现另一套算法，也不自动启用 GEE。可以直接向 Agent 提供“Cube 根目录、需要的操作、时段、输出目录”；多实例时补充 item ID。没有 Agent 时，本页命令就是完整的可重放入口。

开发中的 skill 规范源位于工作区 `.agents/skills/s1grits-analysis/SKILL.md`，独立于 Python 包；安装本包不会自动安装 Agent skill。通用 skill 不依赖任何林业应用或私有研究模块。[LandTrendr 分析](segmentation.md)默认选择另装的本地 LT-rs 实验后端，支持单像元、小点集和小窗口；GEE 需显式选择，目前限单像元。CCDC 的本地实现仍未接入，不能自动切换 GEE。[CUSUM](cusum.md)仍是另装后端的实验入口。它们都不是完成本教程的前置条件。

验证本教程的测试位于 `tests/test_onboarding.py`，覆盖真实文件读写、mean／median、缺测、CLI／Python 一致性和源文件保持不变。教学样本用于验证上手路径，不证明科学检测精度或大洲规模性能。
