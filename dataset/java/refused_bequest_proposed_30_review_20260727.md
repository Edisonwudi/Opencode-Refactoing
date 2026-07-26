# Refused Bequest 30 条候选复核说明（2026-07-27）

## 结论边界

- 本文件对应 `refused_bequest_proposed_30_20260727.csv`。
- 当前共 30 个方法行、9 个 `case_id`、8 个独立 `refactor_group_id`、5 个项目，MyBatis 为 0。
- H2 `Page.NonLeaf` 与 `Page.Leaf` 虽保留两个异味 `case_id`，但属于同一次层次拆分，只计一个独立重构任务。
- 30 行都只是 `ACCEPT_CONDITIONAL` 候选，不是已经进入 canonical 数据集的 30 个最终通过样本。
- 条件的含义是：源码证据支持 Refused Bequest，且存在具体重构路径；但行为 Oracle 尚未实现并在原始/重构后版本上实际通过。
- 实验抽样、训练/测试切分和成功率都必须按
  `refused_bequest_proposed_30_groups_20260727.csv` 中的 `refactor_group_id`
  聚类。不能把同一继承设计缺陷下的多个拒绝方法当成彼此独立样本。

## 候选构成

| `case_id` | 方法行 | 结论 | 主要风险和进入 canonical 的条件 |
|---|---:|---|---|
| `canal_bidirectional_ipacket` | 11 | CONDITIONAL | 同一个双向 `IPacket` 能力污染；需逐 packet 的协议字节 fixture 和调用链 Oracle |
| `h2_page_nonleaf` | 4 | CONDITIONAL | 定义强，但 `Page` 是公开层级；需外部兼容编译和多层 B-tree 持久化 Oracle |
| `h2_page_leaf` | 5 | CONDITIONAL | 定义强，但 `Page` 是公开层级；需外部兼容编译和多层 B-tree 持久化 Oracle |
| `arc_tiled_drawable` | 1 | CONDITIONAL | 必须改变 `TiledDrawable` 的公开父类或改用组合，复用已有 `TransformDrawable` 窄能力；需调用方编译和渲染 Oracle |
| `jhotdraw_text_area_editing_tool` | 1 | CONDITIONAL | 删除拒绝 override 后应继承安全 no-op；需真实编辑工作流和拖拽不异常的修复验收 |
| `jhotdraw_text_editing_tool` | 1 | CONDITIONAL | 必须删除人工异常冻结测试并重建真实编辑工作流 Oracle |
| `h2_offheap_store_backup` | 1 | CONDITIONAL | 内存 store 拒绝持久化备份能力；需公开 API/`BackupCommand` 分派兼容、内存生命周期和两种持久化 store 的 ZIP 备份 Oracle |
| `arc_mix_filter` | 2 | CONDITIONAL | 多输入 filter 拒绝单输入能力；需双输入 GL 渲染 Oracle |
| `mindustry_consume_item_explode` | 4 | CONDITIONAL | 源码明确“不消费任何物品”；需库存、爆炸、UI、统计 Oracle |

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
