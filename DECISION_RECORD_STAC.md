# STAC Generation 功能决策记录

**日期**: 2026-06-16  
**决策**: 选项A - 保留功能，文档明确场景，默认启用

---

## 背景

在实施generate_stac配置功能后，发现了潜在的STAC同步问题：

### 问题1: 增量更新导致不同步
```
T1: generate_stac=false, 处理100个月 → catalog: 100条, STAC: 0个
T2: 运行 stac-publish                → catalog: 100条, STAC: 100个
T3: generate_stac=false, 处理20个月  → catalog: 120条, STAC: 100个 ⚠️
```
**结果**: 新增的20条记录没有STAC Items

### 问题2: 模式切换导致混乱
```
T1: generate_stac=true  → STAC自动生成
T2: generate_stac=false → STAC停止更新
T3: generate_stac=true  → STAC重新生成，状态混乱
```

---

## 考虑的方案

### 方案A: 保留功能 + 严格限定场景 ✅ 已选择
- 保留代码
- 默认 generate_stac: true（安全）
- 通过文档限定适用场景
- 配置文件加强警告

**优点**:
- 解决核心需求（大数据集文件数问题）
- 功能已完成
- 默认行为安全
- 通过文档控制风险

**缺点**:
- 需要用户理解场景
- 误用会有问题

### 方案B: 回退功能
- 移除 generate_stac 配置
- 只保留 stac-publish 命令

**优点**:
- 简单无歧义

**缺点**:
- 核心问题未解决
- 浪费已实现代码

### 方案C: 添加同步检测
- 实现STAC清单文件
- 自动检测不同步

**优点**:
- 支持更多场景

**缺点**:
- 需要额外开发
- 增加复杂度

---

## 最终决策

**选择方案A**

### 理由

1. **核心需求有效**: generate_stac=false确实解决大数据集文件数问题
2. **已完成实施**: 代码已实现并测试
3. **默认安全**: generate_stac: true确保大多数用户不会遇到问题
4. **文档可控**: 通过清晰的文档和警告限定使用场景

### 适用场景限定

**唯一推荐场景**: 一次性大批量历史数据处理

必须满足**所有**条件：
- ✅ 大规模数据集（>50K STAC Items）
- ✅ 一次性处理所有历史数据
- ✅ 处理完成后不会继续添加新数据
- ✅ 只在最后一次性发布STAC

**禁止场景**：
- ❌ 增量更新
- ❌ 反复切换模式
- ❌ 部分重新处理

---

## 实施措施

### 1. 文档 ✅
创建 `STAC_GENERATION_GUIDE.md`:
- 详细说明两种模式
- 明确适用/不适用场景
- 故障排查指南
- FAQ

### 2. 配置文件注释 ✅
更新所有3个配置文件：
- `s1grits_monthly.yaml`
- `s1grits_scenes.yaml`
- `s1grits_static.yaml`

增强警告：
```yaml
# ⚠️ WARNING: Read this carefully before changing from the default!
# 
# generate_stac: true  (DEFAULT - RECOMMENDED for 99% of users)
# generate_stac: false (ADVANCED - Use ONLY in specific scenarios)
#
# ✅ ONLY use if ALL of the following are true:
#    1. Large dataset (>50K STAC Items)
#    2. One-time batch processing
#    3. Will NOT incrementally add new data
#    4. Will run stac-publish ONCE after completion
#
# ❌ DO NOT use if: incremental updates, might add data later, etc.
```

### 3. 默认值 ✅
保持 `generate_stac: true` 作为默认值

---

## 风险管理

### 已识别风险

| 风险 | 严重性 | 缓解措施 |
|------|--------|---------|
| 用户误用导致不同步 | 🟡 中 | 配置文件强警告 + 详细文档 |
| 用户不理解场景 | 🟡 中 | 清晰的适用场景说明 |
| 增量更新误操作 | 🟡 中 | 明确禁止场景列表 |

### 风险接受

团队接受以下风险：
- 用户如果不按文档使用，可能遇到STAC不同步问题
- 依赖用户阅读并理解配置文件注释

**缓解**: 默认值安全（generate_stac: true），只有主动改配置才会遇到问题

---

## 用户指导

### 推荐使用方式

**99%的用户**:
```yaml
generate_stac: true  # 保持默认，不要改
```

**1%的高级用户**（大数据集一次性批处理）:
```yaml
# 阶段1: 批处理
generate_stac: false
# 处理所有数据...

# 阶段2: 发布
s1grits catalog stac-publish --output-dir ./output

# 阶段3: 完成，不再添加数据
```

### 不推荐的使用方式

❌ **增量场景**:
```yaml
Day 1: generate_stac: false, 处理1-10月
Day 2: stac-publish
Day 3: generate_stac: false, 处理11-12月  # 错误！不同步
```

❌ **反复切换**:
```yaml
Run 1: generate_stac: true
Run 2: generate_stac: false  # 错误！混乱
Run 3: generate_stac: true   # 错误！混乱
```

---

## 未来考虑

### 短期（不实施）
- 添加同步检测警告
- workflow结束时检查STAC状态

### 长期（可选）
- 实现STAC清单文件
- 支持增量STAC更新
- 添加 `stac doctor` 命令

### 不考虑
- 自动同步检测（太复杂）
- 强制一致性检查（限制太多）

---

## 总结

通过**选项A（保留功能 + 文档限定）**，我们：

✅ **保留了核心价值**: 解决大数据集文件数问题  
✅ **确保默认安全**: generate_stac: true  
✅ **控制了风险**: 通过文档和警告限定场景  
✅ **提供了灵活性**: 高级用户可以选择延迟生成  

**关键成功因素**: 用户理解并遵守适用场景限定

---

## 决策者
- 用户: raokeyi
- 实施者: Claude (AI助手)
- 决策日期: 2026-06-16

## 文档
- [STAC使用指南](STAC_GENERATION_GUIDE.md)
- [实施完成报告](IMPLEMENTATION_COMPLETE.md)
- [Bug修复报告](BUG_FIX_DATA_OVERWRITE.md)
