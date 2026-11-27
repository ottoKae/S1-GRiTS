# S1-GRiTS Web interface history / Web 界面历史

The maintained public interface is **S1-GRiTS Data Cube Console /
S1-GRiTS 数据立方体控制台**, started with `s1grits serve <workspace>`.
See the [current Web guide](webapp.md) for supported workflows. The current
frontend supports English/中文 switching in the same page, not as a second application.

唯一公开维护的界面是 **S1-GRiTS 数据立方体控制台**。以下是历史来源索引，
不是三个需要安装的版本。历史源码由固定 Git 提交保存，不复制到运行包，
也不通过清理历史目录或删除现有 API 实现“归档”。

| Generation / 界面 | Source snapshot / 历史源 | Status / 状态 |
| --- | --- | --- |
| Streamlit GUI | Commit `0be3fd83f6b30241f47ff53c1334da0559c090e7`, `src/gui/` | Retired in v3; historical Process, Mapping, Catalog, Mosaic and Tile pages / v3 已移除 |
| English Workspace Web | Commit `ef7de6b99ad7827eacbab0def01e7f9391bad0b6` introduced v2.3; `v3.0.0` resolves to `902232eab9c8611db24bbc4b2c8f37f4a7206197` | Historical frontend in `src/s1grits/webapp/static/`; not an alternative English installation / 历史界面，不作为独立英文版 |
| Current Console | Chinese-console baseline `167272f3394dc4ff883cb2f438d2adad62bebd23`, followed by the current candidate's changes | One maintained `src/s1grits/webapp/` application / 当前唯一维护入口 |

## Feature disposition / 功能去向

| Capability / 能力 | Current disposition / 当前处理 |
| --- | --- |
| AOI and MGRS selection, production planning, task recovery | Retained in the current console / 保留现有流程 |
| Local and external read-only Catalog browsing | Retained in the current console / 保留本地与外部只读目录访问 |
| Queue, logs, existing `/api/jobs` and `/api/tasks` | Shared service retained; log cursors fixed without changing their fields / 保留兼容接口 |
| Coverage and missing months | Current Catalog report and existing coverage API retained; old matrix layout is historical / 保留信息和 API，不复制旧布局 |
| Point time series, asset bounds and burst footprints | Existing APIs retained; historical Workspace controls are not all present in the current frontend / API 保留，旧界面的交互控件并非全部已接入 |
| Product-instance selection, group/view access and optional analysis tools | Incremental integration planned; their CLI availability does not imply that the current Web exposes them / 后续按已验收能力接入 |

Keeping an API does not certify support for every new product representation.
The current root-Zarr time-series endpoint must not be assumed to support a
virtual view or arbitrary Zarr group until its instance-access integration is tested.

保留 API 不等于自动支持所有新产品表示。旧根 Zarr 读取接口与虚拟视图／group
接入分别验收；未接入的能力不显示为已经完成。归档旧界面不改变科学处理默认值。

## Inspect a historical source / 查看历史源码

From a Git clone containing the historical commits, inspect source without
checking out over your working files:

```bash
git show 0be3fd83f6b30241f47ff53c1334da0559c090e7:src/gui/app.py
git show 902232eab9c8611db24bbc4b2c8f37f4a7206197:src/s1grits/webapp/static/index.html
```

For a complete historical checkout, use a separate worktree at the desired
commit, with a new empty destination outside your current source tree:

```bash
git worktree add --detach ../s1grits-web-history 902232eab9c8611db24bbc4b2c8f37f4a7206197
```

These references preserve source history, not a verified historical runtime.
Old dependencies have not been reinstalled or tested. A shallow clone may need
the relevant history first; a wheel/sdist does not contain Git history.

历史提交用于追溯与独立查看，不代表旧依赖已验证可安装。不要在当前工作目录中
用旧版本覆盖未提交修改，也不要给历史界面连接生产输出做恢复试验。

Build copies such as `build/lib/`, separate branch worktrees, and the FastAPI
`/docs` page are not additional supported Web applications. Historical GUI code,
manuscripts and research data are not part of the runtime frontend package.
