# S1-GRiTS STAC Generation 使用指南

**版本**: 1.0  
**日期**: 2026-06-16

---

## 概述

S1-GRiTS支持可配置的STAC生成策略，允许用户根据使用场景选择：
- **默认模式**：自动生成STAC Items和Collections（推荐）
- **延迟生成模式**：仅生成catalog.parquet，按需发布STAC

---

## 默认模式（推荐）

### 配置

```yaml
output:
  generate_stac: true  # 默认值，推荐大多数用户使用
```

### 适用场景

✅ **适合以下场景**：
- 持续更新的数据集（每天/每周/每月增量处理）
- 需要实时STAC同步的场景
- 小到中等规模数据集（<50K STAC Items）
- 不确定使用场景时（默认最安全）

### 行为

```
运行 workflow → 
  ├─ 生成 catalog.parquet ✅
  ├─ 生成 数据文件 (COG/Zarr) ✅
  ├─ 生成 STAC Items ✅
  ├─ 生成 STAC Collections ✅
  └─ 更新 root catalog.json ✅

结果：STAC始终与数据同步
```

### 优点
- ✅ STAC自动保持同步
- ✅ 无需额外操作
- ✅ 适合增量更新

### 缺点
- ⚠️ 大数据集文件数较多（~120K STAC Items对应120K个.json文件）

---

## 延迟生成模式（高级）

### ⚠️ 重要警告

**此模式仅适用于特定场景，误用会导致STAC与数据不同步！**

### 配置

```yaml
output:
  generate_stac: false  # 高级用户，特定场景使用
```

### ✅ 适用场景（必须同时满足）

**唯一推荐场景：一次性大批量历史数据处理**

必须满足以下**所有**条件：
1. ✅ 大规模数据集（>50K STAC Items，文件数过多）
2. ✅ 一次性处理所有历史数据（不增量）
3. ✅ 处理完成后不会继续添加新数据
4. ✅ 只在最后一次性发布STAC

**典型工作流**：
```bash
# 阶段1：批量处理（1-2周）
# 设置：generate_stac: false
s1grits process_monthly --config config.yaml  # 1月数据
s1grits process_monthly --config config.yaml  # 2月数据
s1grits process_monthly --config config.yaml  # 3月数据
# ... 处理所有120个月

# 阶段2：发布STAC（一次性）
s1grits catalog stac-publish --output-dir ./output

# 阶段3：完成，不再添加数据
```

### ❌ 不适用场景

**以下场景严禁使用 generate_stac: false**：

1. ❌ **增量更新场景**
```bash
# 错误示例！
Day 1: generate_stac: false, 处理1-10月
Day 2: 运行 stac-publish
Day 3: generate_stac: false, 处理11-12月  # ⚠️ 问题：新数据没有STAC
```

2. ❌ **反复切换场景**
```bash
# 错误示例！
Run 1: generate_stac: true   # 生成STAC
Run 2: generate_stac: false  # ⚠️ STAC过期
Run 3: generate_stac: true   # ⚠️ STAC状态混乱
```

3. ❌ **部分重新处理**
```bash
# 错误示例！
已有：1-12月数据 + STAC
重新处理：generate_stac: false, 处理1-3月  # ⚠️ 1-3月STAC过期
```

### 行为

```
运行 workflow → 
  ├─ 生成 catalog.parquet ✅
  ├─ 生成 数据文件 (COG/Zarr) ✅
  └─ 跳过 STAC Items ❌

手动发布 →
  运行 s1grits catalog stac-publish
  ├─ 读取 catalog.parquet
  ├─ 生成所有 STAC Items ✅
  ├─ 生成 STAC Collections ✅
  └─ 更新 root catalog.json ✅
```

### 优点
- ✅ 处理期间文件数减少99%（120K → 1K文件）
- ✅ 备份/传输更快
- ✅ 减少文件系统压力

### 缺点
- ⚠️ STAC与数据异步（需要手动同步）
- ⚠️ 误用会导致不一致
- ⚠️ 增加操作复杂度

---

## STAC同步问题详解

### 问题1：增量更新导致不同步

**场景**：
```
T1: generate_stac=false, 处理100个月 → catalog: 100条, STAC: 0个
T2: 运行 stac-publish                → catalog: 100条, STAC: 100个 ✅
T3: generate_stac=false, 处理20个月  → catalog: 120条, STAC: 100个 ⚠️
```

**问题**：T3新增的20条记录没有STAC Items

**解决**：
```bash
# 方案1：重新发布全部STAC（推荐）
s1grits catalog stac-publish --output-dir ./output

# 方案2：改用 generate_stac=true（更简单）
```

### 问题2：模式切换导致混乱

**场景**：
```
T1: generate_stac=true  → STAC自动生成 ✅
T2: generate_stac=false → STAC停止更新 ⚠️
T3: generate_stac=true  → STAC重新生成 ✅
```

**问题**：T2期间的STAC过期，T3重新生成时可能有冲突

**解决**：**不要切换模式！** 选定后保持一致

---

## 最佳实践

### 推荐1：默认使用 generate_stac: true

```yaml
output:
  generate_stac: true  # 对99%的用户，这是最佳选择
```

**适用**：
- 小到中等数据集
- 增量更新
- 不确定使用场景

### 推荐2：仅在确认场景时使用 generate_stac: false

**检查清单**：
- [ ] 数据集超过50K STAC Items？
- [ ] 一次性处理所有数据？
- [ ] 处理完成后不再增量更新？
- [ ] 理解STAC同步问题？

**全部✅ → 可以使用 generate_stac: false**  
**任意❌ → 使用 generate_stac: true**

### 推荐3：混合策略

```bash
# 阶段1：大批量历史数据（generate_stac: false）
# 处理2020-2025年数据
generate_stac: false
s1grits process_monthly --config config.yaml

# 发布STAC
s1grits catalog stac-publish --output-dir ./output

# 阶段2：日常增量更新（generate_stac: true）
# 从2026年开始，改为自动生成
generate_stac: true
s1grits process_monthly --config config.yaml  # 自动同步
```

---

## stac-publish 命令详解

### 基本用法

```bash
# 从catalog.parquet生成所有STAC
s1grits catalog stac-publish --output-dir ./output
```

### 选项

```bash
# 跳过确认提示
s1grits catalog stac-publish --output-dir ./output --force

# 指定偏振
s1grits catalog stac-publish --output-dir ./output --polarization VV+VH
```

### 行为

1. **读取** `catalog.parquet`（所有记录）
2. **生成** 每条记录的STAC Item JSON
3. **生成** 所有产品的Collections
4. **更新** root catalog.json
5. **覆盖** 已存在的STAC Items（确保一致性）

### 性能

- 1000条记录 → ~15秒
- 10000条记录 → ~2分钟
- 100000条记录 → ~20分钟

### 幂等性

✅ 可以重复运行，结果一致：
```bash
s1grits catalog stac-publish --output-dir ./output  # 第一次
s1grits catalog stac-publish --output-dir ./output  # 第二次，覆盖，无副作用
```

---

## 多产品类型支持

### 三种workflow共存

```bash
# 所有三种workflow可以共享同一个output_root
s1grits process_monthly --config monthly.yaml
s1grits process_scenes --config scenes.yaml
s1grits process_static --config static.yaml

# 目录结构
output/
├── catalog.parquet              # 包含所有产品类型的记录
├── 46SEG/
│   ├── monthly_DESCENDING/      # ✅ 共存
│   ├── scenes_DESCENDING_Ratio/ # ✅ 共存
│   └── static_DESCENDING/       # ✅ 共存
└── collections/
    ├── s1grits-monthly/
    ├── s1grits-scenes/
    └── s1grits-static/
```

### generate_stac 对所有产品生效

```yaml
# monthly.yaml
output:
  generate_stac: false  # 影响monthly产品

# scenes.yaml
output:
  generate_stac: false  # 影响scenes产品

# static.yaml
output:
  generate_stac: false  # 影响static产品
```

### stac-publish 处理所有产品

```bash
# 一次性为所有产品生成STAC
s1grits catalog stac-publish --output-dir ./output

# 自动处理：
# - monthly STAC Items
# - scenes STAC Items
# - static STAC Items
# - 所有Collections
```

---

## 故障排查

### 问题1：STAC Items缺失

**症状**：
```bash
ls 46SEG/items/  # 空目录
```

**原因**：generate_stac: false

**解决**：
```bash
s1grits catalog stac-publish --output-dir ./output
```

### 问题2：STAC与数据不同步

**症状**：catalog.parquet有120条记录，但只有100个STAC Items

**原因**：在generate_stac: false模式下增量添加了数据

**解决**：
```bash
# 重新生成所有STAC
s1grits catalog stac-publish --output-dir ./output --force
```

### 问题3：Collections缺失

**症状**：
```bash
ls collections/  # 不存在
```

**原因**：generate_stac: false且未运行stac-publish

**解决**：
```bash
s1grits catalog stac-publish --output-dir ./output
```

### 问题4：多产品互删（已修复）

**症状**：运行scenes后，monthly目录消失

**原因**：旧版bug（已在2026-06-16修复）

**解决**：更新到最新版本

---

## FAQ

### Q1: 我应该使用哪种模式？

**A**: 99%的情况使用默认模式（generate_stac: true）

只有在以下情况才考虑generate_stac: false：
- 数据集超大（>50K Items）
- 一次性批处理
- 不增量更新

### Q2: 可以中途切换模式吗？

**A**: **不推荐**

如果必须切换：
1. 切换到generate_stac: false → 运行stac-publish后不再增量
2. 切换到generate_stac: true → 安全，但会覆盖旧STAC

### Q3: stac-publish会删除旧的STAC吗？

**A**: 会覆盖，但不会删除

- 如果catalog中有记录 → 生成/覆盖对应STAC
- 如果catalog中无记录 → 旧STAC保留（成为"孤儿"）

### Q4: 如何清理孤儿STAC？

**A**: 手动删除items目录后重新发布

```bash
rm -rf output/*/items/
s1grits catalog stac-publish --output-dir ./output
```

### Q5: 性能影响有多大？

**A**: 
- 文件数：generate_stac: false减少99%
- 处理时间：影响<10%
- 备份/传输：显著加速（少99%文件）

### Q6: catalog.parquet的作用？

**A**: 权威元数据记录

- 无论generate_stac设置如何，catalog.parquet始终生成
- stac-publish从catalog读取元数据生成STAC
- catalog是STAC的"源"

---

## 技术细节

### catalog.parquet schema

```python
{
    'tile_id': '46SEG',
    'month': '2026-01',
    'product_type': 'monthly',
    'product_label': 'monthly_DESCENDING',
    'flight_direction': 'DESCENDING',
    'zarr_path': '46SEG/monthly_DESCENDING/zarr/xxx.zarr',
    'cog_path': '46SEG/monthly_DESCENDING/cog/xxx.tif',
    'preview_path': '46SEG/monthly_DESCENDING/preview/xxx.png',
    # ... 更多字段
}
```

### STAC生成流程

**generate_stac: true**:
```
数据处理 → 
  写入Zarr → 
  写入COG → 
  写入catalog.parquet → 
  写入STAC Item JSON → 
  完成
```

**generate_stac: false**:
```
数据处理 → 
  写入Zarr → 
  写入COG → 
  写入catalog.parquet → 
  跳过STAC → 
  完成

手动发布 →
  读取catalog.parquet → 
  生成所有STAC Items → 
  完成
```

---

## 版本历史

### v1.0 (2026-06-16)
- ✅ 实现generate_stac配置选项
- ✅ 实现stac-publish CLI命令
- ✅ 修复多产品互删bug
- ✅ 支持所有三种workflow（monthly/scenes/static）

---

## 参考资料

- [STAC规范](https://stacspec.org/)
- [S1-GRiTS文档](../README.md)
- [Bug修复报告](BUG_FIX_DATA_OVERWRITE.md)
- [实施完成报告](IMPLEMENTATION_COMPLETE.md)
