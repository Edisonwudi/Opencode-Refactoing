# 验证与结果合同

本文说明 `smell_verify`、checkpoint、Target Guard 和 runner 最终验收共同执行的
正式合同。它是行为解释文档；实际 schema 和状态枚举以代码与自检为准。

## 1. 两层验收

### 1.1 通用 checkpoint contract

`runtime/python/smell_core/checkpoint_contract.py` 统一处理：

- c000 baseline；
- 生产源码 diff；
- 目标指标及其前后差值；
- build/test 结果；
- 失败原因、剩余工作和续跑条件。

### 1.2 Target Guard

调用方必须提供异味类别和目标位置或身份。Guard 只验证冻结目标，不负责扫描项目并
发现所有异味：

- Long Method 等局部异味只解析目标文件；
- Feature Envy、Code Clone 等结构异味会在有界 diff scope 内检查搬运或复制；
- Guard 不通过全仓遍历扩大目标，也不会在目标解析失败时猜测替代目标。

c000 冻结 Guard rule/profile、唯一目标身份、baseline objectives、selector、项目
revision、目标源码、测试修改策略、解析后的 build/test 命令和验证配置；controller
另持有外部 baseline seal。身份、合同或 seal 缺失时直接拒绝。

dataset 只通过 `target_context_json` 传递白名单内的 selector 身份，例如 symbol、
container、receiver、参数组或父类。CSV 的 `evidence` 只供审计展示，不能反向构造
异味或 PASS。

## 2. Guard 的有界范围

Guard 的基本解析边界是冻结目标文件。Git diff 中的生产路径只是候选元数据，不会
自动变成全项目 AST 输入。

- 局部异味、God Class 和 Dead Code 始终只读显式目标；
- Long Parameter List 只考虑包含冻结方法名的 changed successor；
- Data Clumps 只考虑冻结参数组的显式 occurrence；
- Refused Bequest 只考虑目标的显式祖先合同链；
- Feature Envy 与 Code Clone 只在实际变更行相交的方法内执行 anti-relocation 或
  anti-copy。

Java Guard 最多解析 32 个生产文件、8 MiB 源码；超限返回
`GUARD_SCOPE_TOO_LARGE`，不会退回全仓扫描。checkpoint 只保留 bounded witness，
模型侧 decision 硬上限为 64 KiB，详细 Guard 证据硬上限为 2 MiB；build/test 日志和
diff 独立保存。

## 3. PASS 与 IMPROVED

### PASS / resolved

唯一接受状态。必须同时满足：

- 同版本 Target Guard 唯一定位冻结目标并确认异味消失；
- diff scope 内不存在搬运或复制违规；
- 存在命中目标的非空生产源码 diff；
- 测试修改合同满足；
- 正式 build/test 全部通过；
- bridge 返回码、`status`、`resolution`、`success`、`accepted` 彼此一致。

Guard 不可用、结果无效或目标歧义时 fail closed。dataset evidence 不参与 PASS
判定。

### IMPROVED / improved

表示存在真实生产 diff，且至少一个冻结目标指标相对 baseline 下降，同时正式
build/test 通过。`IMPROVED` 不是接受状态，也不会终止修复循环；仍有预算时 Agent
继续向 `resolved` 修复，预算耗尽后保持 `IMPROVED`。

只有 `build_test_success=true` 的 checkpoint 才能成为可恢复的 `best` 或
`best_partial`。编译或测试失败的候选只保留为诊断证据，不能恢复为接受候选。

## 4. 有效 PASS 的硬规则

- 无生产源码 diff 时 PASS 恒为 0；只改注释、格式、测试或生成物返回
  `EDIT_REQUIRED`。
- capture 后只跟踪冻结目标；selector 不能唯一定位 baseline 时直接拒绝。
- Feature Envy 以解析后的方法身份和参数类型冻结目标，receiver 指标变化不能伪装成
  目标消失。
- Data Clumps 只接受冻结参数组和 occurrence scope 内的变更。
- `BUILD_FAILED`、`TEST_FAILED`、`SAMPLE_TEST_FAILED` 按真实失败阶段归因，不能被
  smell 状态覆盖。
- test source 不进入生产 diff；构建描述符或验证脚本变化返回
  `VERIFICATION_CONFIG_MODIFIED`。
- 正式 `project_full` 必须产生 fresh、非零且非全 skipped 的测试证据。版本输出、
  帮助输出、文件存在性和 collection-only 不能证明测试通过。

## 5. 正式验证生命周期

验证按以下阶段执行：

1. source-only Guard 判断目标异味是否已经越线；
2. 如项目配置了重预检，对未越线候选在一次性 fresh worktree 中执行聚焦编译和固定
   测试或行为探针；
3. 越线后，在正式 fresh worktree 中执行一次 `project_full`。

前两阶段只提供修复反馈，`acceptance=false`，不能缓存或复用为最终 PASS。最终快照
以 c000 为基线采集完整 Git diff/status，包括已提交、已暂存、未暂存和未跟踪的生产
源码与构建元数据；模型自行 commit 不能隐藏改动。

runner 最终只选择一个权威终态：

- 若最后一次 Agent `project_full` PASS 的候选 diff、fresh isolation、build/test、
  Guard、测试冻结和 artifact 都与当前候选一致，复用该次正式验证并写独立 receipt；
- 否则执行一次新的 `runner_final` 正式验证。

同一 diff 若 Agent 测试失败而 final 单次转绿，记为
`FLAKY_TEST_INCONCLUSIVE`；Agent 已通过而 final 仅因超时或 OOM 翻红，记为
`FINAL_VERIFY_INFRA_FAILED`。二者都不自动接受。

## 6. 修复循环与预算

- `--sample-deadline` 是样本唯一时间预算，默认 1800 秒，范围 60–7200；
- `--model-event-inactivity-timeout` 默认 300 秒；模型没有新 JSON 事件且没有正在运行
  的 bridge/build/test 子进程时，runner 以 `OPENCODE_TIMEOUT` fail closed；
- `--max-smell-verify-cycles` 默认 10，范围 0–10；
- `--loop-no-progress-limit` 默认 3，范围 1–5；
- 初始验证与终态 receipt 重放不消耗修复周期。

只有 production diff、当前 blocker、指标缺口和结构失败数量都未变化时才累计一次
no-progress；任一项真实变化即清零。基础设施等待不计入停滞周期。预算内失败会把
有界的基线、当前值、差值、剩余目标和唯一下一步动作反馈给同一 session。

## 7. 测试变更合同

默认策略是 `immutable`。启用 `--allow-test-changes` 后切换为
`api_migration`，但仍必须满足：

- 不删除已有测试文件；
- 不减少测试方法或断言；
- 不新增 disabled、ignored 或 assumption-skip；
- 不修改测试资源、构建描述符和验证脚本；
- 所有声明测试产生 fresh、非零的执行证据。

未变化测试复用冻结 manifest 的强度审计，added/changed test source 重新审计。
任何 PASS 或 IMPROVED 都必须通过这份合同。

## 8. 状态解释

| 类别 | 典型状态 | 是否接受 |
|---|---|---:|
| 目标已解决且正式验证通过 | `PASS` / `resolved` | 是 |
| 指标改善但目标仍存在 | `IMPROVED` / `improved` | 否 |
| 模型候选不满足合同 | `EDIT_REQUIRED`、`BUILD_FAILED`、`TEST_FAILED`、Guard failure | 否 |
| 同候选测试不稳定 | `FLAKY_TEST_INCONCLUSIVE` | 否 |
| 最终验证基础设施失败 | `FINAL_VERIFY_INFRA_FAILED` | 否 |
| Provider 或执行异常 | 对应 infra/provider 状态 | 否 |

判断结果时应同时查看 `results.csv`、`result.json`、`verify.json` 和
`runner-final-receipt.json`，不要只根据某一个 `status` 字段下结论。
