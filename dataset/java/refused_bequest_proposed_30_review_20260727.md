# Refused Bequest 30 条候选复核说明（2026-07-27）

## 结论边界

- 本文件对应 `refused_bequest_proposed_30_20260727.csv`。
- 当前共 30 个方法行、9 个 `case_id`、8 个独立 `refactor_group_id`、5 个项目，MyBatis 为 0。
- H2 `Page.NonLeaf` 与 `Page.Leaf` 虽保留两个异味 `case_id`，但属于同一次层次拆分，只计一个独立重构任务。
- 30 行仍是 `ACCEPT_CONDITIONAL` 候选，不是已经进入 canonical 数据集的 30 个最终通过样本。
- 8 个重构组的行为 Oracle 已实现，并在重构前固定提交上实际通过；对应的
  `test_commit`、`test_tree`、测试文件 SHA256 和命令记录在
  `refused_bequest_proposed_30_groups_20260727.csv`。
- `refused_bequest_oracle_ready_30_20260727.csv` 是 30 个目标方法的审计目录，
  不直接交给 runner，避免把同一设计缺陷重复统计。
- `refused_bequest_oracle_groups_8_20260727.csv` 才是复用现有 delivery schema
  和 runner 的执行清单：一个 `refactor_group_id` 对应一个 checkout、一次重构、
  一次行为测试，结构 guard 必须逐项通过组内全部 location。
- `PRE_REFACTOR_PASS` 只表示行为基线通过，重构后仍须同时通过行为 Oracle
  与异味消除 guard。严格 Refused Bequest Oracle 禁止使用 `local` 验证；
  测试命令返回 0 还不够，runner 会要求固定测试类产生新的非空 JUnit XML 报告。
- 实验抽样、训练/测试切分和成功率必须按 8 个 `refactor_group_id` 统计，
  不能把同一继承设计缺陷下的多个拒绝方法当成彼此独立样本。

## 行为 Oracle 与结构重构的兼容性

- Canal 只验证客户端写包和服务端读包；被删除的是相反方向的拒绝能力。
- H2 通过 MVStore 和 JDBC 公开流程验证，不直接调用 Page/OffHeap 的拒绝方法。
  Page 的 9 个目标会在重构前固定为 4 个 `Page.NonLeaf` 和 5 个
  `Page.Leaf`，删除方法后不再依赖可能漂移的旧行号定位类。
- JHotDraw 只验证编辑、提交、撤销和重做，不要求保留 `mouseDragged` 异常。
- Arc 只验证四参数平铺绘制和双输入 MixFilter；guard 用目标参数个数区分
  待删除重载与必须保留的合法重载。
- Mindustry 通过 `Block`/`Building` 框架入口验证，合法的 `extends Consume`
  重构不需要保留四个空 override。

因此测试与异味消除没有先天冲突；无法同时通过时应优先判断为重构不完整、
公开调用方未迁移或 guard 契约错误，而不是放宽行为测试。

## 候选构成

| `case_id` | 方法行 | 结论 | 主要风险和进入 canonical 的条件 |
|---|---:|---|---|
| `canal_bidirectional_ipacket` | 11 | PRE_REFACTOR_PASS | 协议方向字节 Oracle 已通过；重构后仍需能力拆分 guard 和调用方编译 |
| `h2_page_nonleaf` | 4 | PRE_REFACTOR_PASS | 多层 B-tree 插入、修改、删除、遍历和持久化重开 Oracle 已通过；公开层级兼容仍待重构后确认 |
| `h2_page_leaf` | 5 | PRE_REFACTOR_PASS | 与 NonLeaf 共用同一 B-tree 行为 Oracle；必须作为同一重构组验收 |
| `arc_tiled_drawable` | 1 | PRE_REFACTOR_PASS | 四参数平铺绘制与 tint/尺寸 Oracle 已通过；公开父类及调用方兼容仍待重构后确认 |
| `jhotdraw_text_area_editing_tool` | 1 | PRE_REFACTOR_PASS | 原人工异常冻结测试已删除，真实编辑、提交、撤销、重做 Oracle 已通过 |
| `jhotdraw_text_editing_tool` | 1 | PRE_REFACTOR_PASS | 与 TextArea 工具共用真实编辑工作流 Oracle；拒绝 override 的删除由 guard 验收 |
| `h2_offheap_store_backup` | 1 | PRE_REFACTOR_PASS | OffHeap 生命周期及持久化数据库 ZIP 备份 Oracle 已通过；分派兼容仍待重构后确认 |
| `arc_mix_filter` | 2 | PRE_REFACTOR_PASS | 双纹理输入与绑定单元 Oracle 已通过；单输入能力拆分由 guard 验收 |
| `mindustry_consume_item_explode` | 4 | PRE_REFACTOR_PASS | 通过 Block/Building 真实入口验证爆炸、库存、UI、统计和 item-acceptance，未直接调用待删除方法 |

## JHotDraw 测试来源处理

`jhotdraw-core/src/test/java/org/jhotdraw/draw/tool/TextEditingToolRefusedBequest_ESTest.java`
不是原项目测试。它由 `Edisonwudi` 的提交
`542e307bbc9013c6d381f1d54e07e04ff0db034e`（`test: add refused bequest coverage tests`）
手工加入，并把 `UnsupportedOperationException` 冻结成期望行为。

因此：

1. 该测试不得作为历史行为 Oracle，也不得用于判定重构回归。
2. JHotDraw 候选进入 canonical 前，应删除该测试。
3. 先用原项目的提交、取消、撤销、重做、选择等编辑行为构建行为 Oracle。
4. “拖拽不再抛异常、拒绝 override 消失”只作为异味修复验收，和历史行为保持分开报告。

## 已剔除

- 所有 MyBatis 行：接口中的不支持方法主要是框架扩展协议、兼容钩子或外部 API 约束，且此前存在测试冻结，局部重构价值不足。
- Kerby 1–9：缺少实现或资源清理，不是 Refused Bequest。
- JHotDraw passive outline handle：父类已经提供合法 no-op，子类重复空 override 不等于拒绝继承能力。
- JHotDraw `DragHandle.draw`：源码明确它是有意不可见的交互 handle。
- Arc `MixFilter.resize`：父实现本身已经是 no-op。
- H2 `MVDelegateIndex`：共享 primary index 下的 mutation no-op 是适配语义，拆分会扩散到核心索引协议。
- Guava forwarding 默认实现：装饰器扩展点，不是拒绝继承。
- Commons Lang `ExtendedMessageFormat`：原项目公开契约明确冻结不支持行为，不适合本数据集。
- Commons Compress `Deflate64Decoder`：不支持写入是上游明确的产品能力限制和公开说明，不用未实现 encoder 凑数。

## 升级为正式样本的门禁

每个 `refactor_group_id` 必须依次满足：

1. 原始版本上的行为 Oracle 可执行并通过；
2. 重构仅移除拒绝能力，不改业务语义；
3. 重构 diff 经过人工检查，确认没有删除断言、放宽测试或把异常改成静默错误；
4. 编译、项目测试、目标行为 Oracle 全部通过；
5. 对公开层级变更增加外部调用方编译 Oracle；
6. 结构验收证明目标拒绝方法或不适用能力已从子类型契约中消失。

任何一步失败都保持 `CONDITIONAL` 或降为 `REJECT`，不能用 guard 单独替代业务 Oracle。
