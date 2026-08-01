# Feature Envy / Data Clumps 固定 10 条约束可满足性审计

日期：2026-08-01

## 目的与口径

本审计不筛除数据集样本，也不把 `IMPROVED` 记作 PASS。目标是先判断每个固定样本是否存在同时满足以下条件的合法重构，再决定是否继续追逐通过率：

1. 原始产品 detector finding 确实存在；
2. 同一 detector 不再报告目标 finding；
3. 不修改测试，不用 varargs、`Object`、数组、map、反射改签名等方式规避 detector；
4. 生产编译和样本测试通过；
5. 不复制业务逻辑、不把 finding 搬到原类的另一个方法、不制造跨领域参数袋；
6. 测试可见 API/override 合同得到保留，或者有明确、预算内的兼容根。

分类：

- `PROVEN`：已有原始 diff、detector、build/test 三方证据，且人工审计认为语义可信。
- `FEASIBLE`：已有具体合法结构路线，但尚无完整通过 diff。
- `SCALE_BLOCKED`：原则上存在机械迁移路线，但当前模型、时限和编辑工具难以完成。
- `CONTRACT_CONFLICT`：严格 detector 与不可修改的测试/API 合同不能同时满足。

## Data Clumps 固定 10 条

| ID | 产品基线规模 | 当前最好证据 | 可满足性 | 审计结论 |
|---:|---:|---|---|---|
| 1 | 3 | M6 PASS | `PROVEN` | `WrapSizeLimitParams` 覆盖同一 GSS wrap-size family，调用方和 V1/V2 实现同步迁移，测试通过。 |
| 2 | 3 | M6 自动 PASS | `FEASIBLE` | 自动 diff 把 `TableLinkOptions` 同时用于 TableLink 与 TCP shutdown，形成跨领域参数袋，不计语义可信 PASS。合法窄域路线是只迁移 TableLink occurrence，使两个 shutdown 旧声明保留，detector 降至 2。 |
| 3 | 16 | M4 16→5，测试旧 override 失败 | `FEASIBLE` | `FilterContext` 合理。可在 `Index` 保留一个双向兼容根：生产子类 override 新入口，测试子类仍 override 旧入口；`getCostRangeIndex` 再保留一个薄旧入口，总旧声明不超过 2。 |
| 4 | 3 | M4 PASS | `PROVEN` | `RowContext(session, rows)` 迁移 PgCatalogTable family，未复制业务体，剩余两个独立旧 occurrence 低于阈值，测试通过。 |
| 5 | 3 | M6 PASS | `PROVEN` | 局部 `ConcatenationValuePair(left, right)` 消除目标三元组，责任和行为保持在 concatenation 领域。 |
| 6 | 6 | M4 6→5 | `FEASIBLE` | 项目已有 `DrawingEvent`/`FigureEvent`，可迁移 event family；只需在两个不同名称的共享 protected 根保留旧签名，满足最大 2 个兼容声明。 |
| 7 | 至少 4 个测试可见 family | detector 归零后出现 11 个 test-compile 错误 | `CONTRACT_CONFLICT` | 测试直接调用/override `exportView`、`loadViewFromURI`、`openViewFromURI`、`saveViewToURI` 等至少 4 个不同旧方法名。恢复精确旧签名后 detector occurrence 至少为 4，高于 PASS 最大值 2。 |
| 8 | 25，覆盖 24 个类 | 无有效 diff | `SCALE_BLOCKED` | 多数是 expression-function `getValue(session,v1,v2...)` family，可用共享 `FunctionArguments`/value context 机械迁移；但需改动至少 23 个声明，当前 1800 秒模型编辑无法可靠完成。 |
| 9 | 7 | M6 PASS | `PROVEN` | `LinkConnectionCredentials` 在 linked-database connection family 内聚使用，Schema、Database、TableLink、TableLinkConnection 和调用方同步迁移，测试通过。 |
| 10 | 8 | M4 8→4 | `FEASIBLE` | geometry utilities 的 `(target,index,total)` 属于同一坐标遍历语义；已有部分合法 index/total holder 迁移。继续迁移 GeoJSON/Target family，最多保留一个公开根即可降至 2。 |

初步上界：严格合同下至少 8 条有明确合法路线；ID 8 是规模阻塞而非逻辑冲突，ID 7 是已证实合同冲突。因此 7/10 理论上可达，但不是继续增加 prompt 轮次即可达到。

## Feature Envy 固定 10 条

| ID | 当前最好证据 | 可满足性 | 审计结论 |
|---:|---|---|---|
| 1 | IMPROVED；当前 diff 形成 `Rules → Dialog → Rules` 循环 | `FEASIBLE` | 不应把 UI 构建放入 `Rules`。可在 UI 层抽取 `RulesUiBinder`/binding workflow，由原目标单次委托；需证明不是简单跨类搬运 finding。 |
| 2 | IMPROVED，20→11 receiver accesses | `FEASIBLE` | `SimpleAsyncConfig.resolveBasePaths/resolvePluginsDir/configurePipesNode` 是合理的 receiver-owned operation；继续关闭剩余 config access cluster。 |
| 4 | PASS | `PROVEN` | `buildClassFile` 整体迁到数据占主导的 `ClassBands`，Segment 只保留单次委托；少量 Segment orchestration 输入未形成反向主导访问。 |
| 5 | detector 基本解决，测试语义 20→17 | `FEASIBLE` | 当前 default-method 迁移漏掉原 ACK/错误写出顺序。按 effect ledger 恢复原有写出行为，或抽取 `CanalAdminCommandDispatcher`，无需回退 receiver access。 |
| 6 | 大幅迁移后 `SAMPLE_TEST_EVIDENCE_MISSING` | `FEASIBLE` | Ticket policy 下沉方向合理；需要合并为一个更粗粒度 receiver operation，并修复测试证据执行链。证据缺失不是 smell 修复失败。 |
| 8 | detector 解决后 Mockito `wanted but not invoked` | `FEASIBLE` | Mockito 不执行新加到被 mock `TaskManager` 上的真实方法。应抽取独立 `TaskResultWorkflow`，内部调用既有 `setFileResult/setComplete`，让测试继续观察原交互；禁止同类 fallback。 |
| 10 | IMPROVED，8→5 receiver accesses | `FEASIBLE` | `WebSession.initDatabaseState` 是合理的 cohesive operation；继续合并剩余 session-owned cluster。 |
| 11 | PASS | `PROVEN` | `ServerConfig.initServer` 完成 server configuration factory 责任迁移。应清理同一 diff 中未使用的重复 configure helpers，但核心路线合法。 |
| 15 | PASS | `PROVEN` | `OcrConfig.RenderingDetails` 是 OCR rendering 字段的内聚子集，不是整个 config 的原始快照；目标方法只取一次 purpose-specific value object。 |
| 17 | 自动 PASS，但同类 fallback 仍是新 finding | `FEASIBLE` | 当前 PASS 无效。可抽取独立 `CertificateValidationWorkflow`，由其调用既有 service API；Mockito 仍能观察旧交互，controller 只保留单次 workflow 调用。 |

初步上界：除 ID 1 仍需证明 binder 不只是跨类搬运外，其余 9 条都有具体合法路线。80% 在逻辑上并未被否定，但当前单一路线“全部移到 receiver”无法覆盖 mocked receiver、UI binder 和协议 dispatcher 场景。

## 对现有机制的处置建议

1. 保留 Data Clumps M6 的“原参数槽位连续性”修正；它消除了同类型无关参数造成的 guard 误判。
2. 保留 M5 已证明有效的 body-dispersion 防伪信号，但不能宣称它完整：历史 sample 10 被拒绝，sample 4 的复制式伪修复仍被放过。
3. 保留 Feature Envy 同原类、同 receiver type 新 finding 的拒绝合同；自检和历史 sample 17 回放均已证明它能拒绝原类内搬运。
4. Data Clumps skill 从“全项目只能一个 holder”改为“每个调用/override/领域连通分量一个内聚 holder”；跨领域 occurrence 不得共享参数袋。
5. Feature Envy 在 receiver-owned operation 之外增加 workflow/adapter 路线。检测到 Mockito interaction failure 时，禁止在被 mock receiver 上继续加真实方法，也禁止同类 fallback；改由独立 workflow 调用既有 receiver API。
6. 在结构路线修正前不增加 continuation 次数。更多轮次已经被证明会扩大复制、搬运和兼容壳伪修复。

## M6 最终证据

运行目录：`/home/testuser/minimax-m27-dc-slot-continuity-m6-10-5c-20260801`。

| ID | 最终状态 | resolution | 耗时（秒） | 关键证据 |
|---:|---|---|---:|---|
| 1 | PASS | resolved | 418.6 | 语义可信。 |
| 2 | PASS | resolved | 845.2 | detector finding 消失、槽位连续 occurrence=1、build/test guard 成功；但人工审计为跨领域参数袋，不计可信成功。 |
| 3 | SMELL_GUARD_FAILED | unresolved | 1324.5 | 兼容根尚未闭合。 |
| 4 | SMELL_GUARD_FAILED | unresolved | 547.8 | 本轮退化；保留 M4 的可信 PASS 作为可满足性证据。 |
| 5 | PASS | resolved | 877.7 | 语义可信。 |
| 6 | IMPROVED | improved | 1872.0 | 同一 finding 6→5，槽位连续 occurrence=4；未冒充 PASS。 |
| 7 | SAMPLE_TEST_FAILED | unresolved | 1041.7 | 与至少四个测试可见旧 API family 的合同冲突一致。 |
| 8 | SMELL_GUARD_FAILED | unresolved | 1271.5 | 普通 finding 被隐藏为 0，但原槽位仍有 24 个 occurrence，M6 正确拒绝。 |
| 9 | PASS | resolved | 646.3 | detector finding 消失、槽位连续 occurrence=2、build/test guard 成功；语义可信。 |
| 10 | SMELL_GUARD_FAILED | unresolved | 757.4 | 普通 finding 为 0，但原槽位仍有 3 个 occurrence，高于 passing max=2，M6 正确拒绝。 |

自动分布为 `PASS=4, IMPROVED=1, SMELL_GUARD_FAILED=4, SAMPLE_TEST_FAILED=1`；平均耗时
960.27 秒，中位数 861.45 秒。人工语义可信的本轮 PASS 为 3/10（ID 1、5、9）。实验结束后
`controller.completed`、`secret.env.cleaned` 均存在，结果数为 10，容器为 0，临时密钥不存在，
Windows 托管计划任务已删除。

## Feature Envy sample 17 回放证据

使用当前本地 guard、原始镜像基线项目和 M3 历史 `diff.patch`，在无网络、无 API 密钥容器中
重新 capture baseline 后验证。结果由旧版自动 PASS 变为 `SMELL_GUARD_FAILED / unresolved`，
checkpoint reason 为 `SEMANTIC_CONTRACT_REGRESSION`，精确回报：

`same_owner_receiver_finding_relocated:...ValidateSignatureController.java#buildValidationDetailsFallback(...)`

这证明新合同拒绝的是“同一原类内把同 receiver finding 搬到新 helper”，而不是按项目名或样本 ID
写规则。回放证据保存在远端 `/home/testuser/fe-relocation-replay17-20260801/verify.json`。

## 本地验收

- `npm run check`：PASS，覆盖 bridge、TypeScript plugin、finding/checkpoint contracts、11 类
  adapters、runner continuation、build/test evidence、focused guards 与完整 self-check。
- `python3 .../skill-creator/scripts/quick_validate.py .opencode/skills/java-smell-edit-patterns`：
  `Skill is valid!`
