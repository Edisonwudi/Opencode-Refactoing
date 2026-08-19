# 进阶运行指南

本文记录根 `README.md` 快速上手之外的运行方式：模型配置、runner 参数、手动
OpenCode 会话、原生 OpenCode 对照，以及外部批量控制器。验收语义见
[`verification-contract.md`](verification-contract.md)，语言与数据集约束见
[`language-and-dataset-contracts.md`](language-and-dataset-contracts.md)。

## 1. 模型配置

API key 只允许来自环境变量或 secret 文件，不要写进仓库、CSV、日志或命令参数。
runner 生成的 `opencode.runtime.json` 中只保留 `{env:...}` 引用。

```bash
export SMELL_OPENCODE_API_KEY="<api-key>"
export SMELL_OPENCODE_BASE_URL="https://api.minimaxi.com/v1"
```

| provider | `--model` 示例 | base URL |
|---|---|---|
| MiniMax | `minimax/MiniMax-M2.7` | `https://api.minimaxi.com/v1` |
| Z.AI | `zai/glm-4.7` | `https://api.z.ai/api/coding/paas/v4` |
| 智谱 Coding Plan（中国站） | `zai/glm-4.7` | `https://open.bigmodel.cn/api/coding/paas/v4` |

调用时同时传递模型、key 所在环境变量名和端点：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /abs/samples.csv \
  --sample-id 1 \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL"
```

key 来源优先级为：`--opencode-api-key`（不推荐）>
`--opencode-api-key-env`（推荐）> `OPENCODE_AUTH_JSON` 或镜像内置 auth。

## 2. runner 的两种 Agent 模式

### 2.1 交付 Agent

Java 使用 `java-refactor-agent`；Python、C、C++ 默认根据 CSV 的 `language`
自动选择 `smell-refactor-agent`，也可显式指定。

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /agent-src/dataset/java/delivery_schema/<smell>.csv \
  --sample-id <id> \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode sample_optimized \
  --agent java-refactor-agent
```

该路径加载本交付件的 Agent、Skill、command loop 和 `smell_verify`，模型可以根据
有界验证反馈继续修复。

### 2.2 原生 OpenCode 对照

要测不使用本交付 Agent、command、Skill 和插件控制环的原生 OpenCode 能力：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /agent-src/dataset/java/delivery_schema/<smell>.csv \
  --sample-id <id> \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode project_full \
  --agent opencode-builtin
```

`opencode-builtin` 只支持 `direct` backend，并具有以下固定边界：

- 不向样本项目注入本交付件的 `.opencode`；
- OpenCode 启动参数不含 `--command` 或 `--agent`；
- 不初始化 command-loop 状态，模型不能调用 `smell_verify`；
- 模型只收到一次原始重构任务；
- 模型退出后，runner 基于启动前冻结的 c000 做一次独立最终验证；
- 模型侧验证次数和 runner 恢复次数都应为 0。

独立验证可以产生正式 PASS，但不会放宽 finding、production diff、测试变更或
build/test 合同。这一模式用于能力对照，不是交付 Agent 的降级路径。

## 3. 验证模式与项目命令

### 3.1 `sample_optimized` 与 `project_full`

两种模式都会先执行 source-only Guard，并在正式验收时执行严格 build/test：

- `sample_optimized`：build 后执行数据行中已物化的聚焦测试命令；
- `project_full`：执行解析后的项目级测试；若 CSV 还声明了样本测试，再追加执行，
  并要求每个声明测试类产生 fresh、非零的执行证据。

允许测试迁移时必须使用 `project_full`。真实用户只需声明 build 与项目测试，无需
虚构样本测试。

### 3.2 直接声明项目命令

无需先写项目 manifest：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /abs/samples.csv \
  --build-command './gradlew --offline classes' \
  --project-test-command './gradlew --offline test' \
  --verification-cwd .
```

解析优先级不会静默覆盖：

1. CLI 的 `--build-command`、`--project-test-command`、`--verification-cwd`；
2. CSV 的 `build_command`、`project_test_command`、`verification_cwd`；
3. `--projects <yaml>` 中匹配项目的 manifest。

CLI 与 CSV 同时声明时三项必须一致，否则 runner 在模型启动前报告冲突。同一
`project_path` 与 revision 的所有选中数据行也必须一致。复杂环境变量、多行脚本和
项目子根适合放在 manifest。

CSV 的 `test_file`、`test_command` 只表示样本级测试，不等同于项目级完整测试。
验证模式按“显式 CLI > CSV > `project_full`”解析；`--allow-test-changes` 始终强制
`project_full`。最终命令、cwd 和来源都会冻结进 c000。

## 4. 手动 OpenCode 会话

交互式会话或 `opencode run` 可以直接使用 command：

```text
/java-refactor-run --verification-mode=project_full --max-smell-verify-cycles=10 -- Project root: /abs/java-project
Smell type: long_method
Target location: src/main/java/Foo.java:42
Build command: ./gradlew --offline classes
Project test command: ./gradlew --offline test
Verification cwd: .
```

Java command 支持的 policy 参数包括：

- `--verification-mode=sample_optimized|project_full`；
- `--refactoring-backend=direct|idea`；
- `--allow-test-changes`；
- `--loop-mode=off|verify-failure`；
- `--max-smell-verify-cycles=0..10`；
- `--loop-no-progress-limit=1..5`；
- `--loop-on=smell,compile,test`；
- `--sample-deadline=60..7200`。

非法组合会在模型启动前报告 `INVALID_LOOP_POLICY`。

IDEA backend 只适用于另行配置了 IDEA service 的开发环境，不包含在当前 Java
交付镜像中。合法变更链为 `preview -> matching apply -> smell_verify`，或明确
`unsupported_target` 后的 `preview -> idea_edit -> smell_verify`；失败不会转回
普通文本编辑。

## 5. 测试迁移

测试默认以 `immutable` 冻结。只有 controller 显式传
`--allow-test-changes` 才切换为 `api_migration`：

- 已有测试文件不得删除；
- 测试方法和断言不得减少；
- 不得新增 disabled、ignored、assumption-skip；
- 测试资源、构建描述符和验证脚本仍不可改；
- 声明测试必须在最终命令中产生 fresh、非零的执行证据。

允许测试迁移只解决 API 同步问题，不降低 PASS 门槛。

## 6. 外部批量控制器

使用交付镜像的 `benchmark-worker` 入口时必须提供 `--results-root`：

```bash
docker run --rm \
  --pull=never \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="/path/to/control",dst=/control,readonly \
  --mount type=bind,src="/path/to/results",dst=/results \
  --mount type=bind,src="/path/to/secret",dst=/secret,readonly \
  opencode-java-refactor-env:0.1.1-rb-certified-no-idea-mounted-source-v2 \
  benchmark-worker \
  --plan /control/plan.json \
  --results-root /results \
  --secret-file /secret/model-api-key
```

入口会创建可写的 `<results-root>/artifacts`，再设置唯一的
`SMELL_ARTIFACT_ROOT`。目录不可写时会在模型调用前以退出码 73 失败。

C/C++ worker 如需跨样本复用编译对象，只能按“精确镜像 tag / 语言 / 项目”共享
`/var/cache/refactoragent/ccache`，并设置 `CCACHE_DIR` 与 `CCACHE_UMASK=000`。
不得共享 build 目录、测试报告或验收结论；Python worker 不挂该 volume。

每个样本仍必须使用独立 checkout 和独立结果目录，requested commit/tree 与 actual
必须一致，不允许退回当前 HEAD。更多筛选、revision 和 dry-run 参数以
`python3 scripts/run_smell_dataset.py --help` 为准。

## 7. 结果与审计文件

```text
runs/<run-name>/results.csv
runs/<run-name>/samples/<sample>/
  result.json
  verify.json
  runner-final-receipt.json
  diff.patch
  run.log
  artifacts/
    guard-evidence.json
    build.log
    test.log
```

`results.csv` 分开记录 `status`、`resolution`、`accepted`、`progress`、
`termination_reason`、总耗时、setup 耗时和样本预算耗时。`result.json.attempts`
保存每次 Agent 验证和最终验收选择；详细规则见
[`verification-contract.md`](verification-contract.md)。
