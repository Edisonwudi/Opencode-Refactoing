# OpenCode Java Refactor Delivery

这是一个用于 Java 异味修复的 OpenCode 最小交付包。它提供两个公开入口：

- `java-refactor-agent`：直接使用 OpenCode 读/搜/编辑能力修复 Java 异味。
- `java-refactor-agent-idea`：在直接编辑能力之外，允许按需使用 IDEA CLI 做 Java 语义重构。

插件只暴露一个异味领域工具：`smell_verify`。异味上下文由手动输入或批量 runner 注入，agent 负责修复和最终验收。

## 1. 确认目录内容

本包包含：

- `.opencode/agents/`
- `.opencode/commands/`
- `.opencode/skills/`
- `.opencode/plugins/smell.ts`
- `runtime/python/bridge/smell_bridge.py`
- `runtime/python/smell_core/`
- `scripts/run_smell_dataset.py`
- `scripts/self_check_smell_verify.mjs`
- `package.json` / `package-lock.json`
- `opencode.json`

本包不包含实验结果、测试产物、本地试跑 worktree、Java 项目快照或数据集。Docker 镜像场景下，Java 数据集和项目快照继续复用已有运行环境或组件镜像。

## 2. 准备运行环境

需要已有：

- OpenCode
- Node.js 22 / npm
- Python 3
- Java 项目源码或 Java smell 数据集
- 模型 provider/key
- 可选：IDEA CLI

安装本包依赖：

```bash
npm ci
cd .opencode && npm ci && cd ..
python3 -m pip install pyyaml
```

如果需要 IDEA CLI 增强，设置其中一个环境变量：

```bash
export IDEA_REFACTOR_CLI=/absolute/path/to/idea-refactor
export SMELL_IDEA_REFACTOR_CLI=/absolute/path/to/idea-refactor
```

如果严格 build/test 验证需要项目级配置，设置：

```bash
export SMELL_PROJECTS=/absolute/path/to/projects.yaml
```

## 3. 配置模型认证

批量 runner 会为每个样本生成临时 `opencode.runtime.json`。模型认证和 baseURL 可以直接在 runner 命令里配置。

推荐方式是把 key 放在环境变量里，命令中只引用环境变量名：

```bash
export SMELL_OPENCODE_API_KEY="<api-key>"
export SMELL_OPENCODE_BASE_URL="https://api.z.ai/api/coding/paas/v4"
```

runner 参数：

```bash
--opencode-api-key-env SMELL_OPENCODE_API_KEY
--opencode-base-url "$SMELL_OPENCODE_BASE_URL"
```

对于 `zai/*` 模型，默认 baseURL 已经是 `https://api.z.ai/api/coding/paas/v4`；仍建议在交付验证命令里显式写出，便于排查环境差异。

也可以使用已有 OpenCode auth 文件：

```bash
export OPENCODE_AUTH_JSON=/absolute/path/to/auth.json
```

Docker 运行时可以挂载 auth 文件：

```bash
-v /path/to/auth.json:/auth/opencode-auth.json:ro \
-e OPENCODE_AUTH_JSON=/auth/opencode-auth.json
```

如果镜像已经在 `/opt/buildenv/offline-home/.local/share/opencode/auth.json` 提供 auth，可以省略上述参数。

key 来源优先级为：`--opencode-api-key`、`--opencode-api-key-env`、`--opencode-auth-json`/`OPENCODE_AUTH_JSON`。不建议在共享脚本中使用 `--opencode-api-key`，避免把明文 key 写入命令历史或日志。

## 4. 先做基础校验

在本包目录执行：

```bash
npm run check
npm run check:self
python3 runtime/python/bridge/smell_bridge.py verify --help
python3 scripts/run_smell_dataset.py --dataset /path/to/java.csv --dry-run
```

`--dry-run` 只检查数据集解析和任务构造，不调用模型。

`npm run check:self` 会创建临时 Java 项目，直接调用 Python bridge，再加载 OpenCode 插件并执行 `smell_verify`，最后模拟 OpenCode 对工具结果做 `split()` 和 JSON 解析。该自检不访问模型 API。

## 5. 手动修复一个 Java 异味

手动模式下，用户输入就是最终任务输入，需要包含项目根目录、语言、异味类型、目标位置、证据和验证模式等信息。

直接编辑路径（command 参数是显式配置验证与 loop policy 的入口）：

```bash
printf '%s\n' '--verification-mode=local --loop-mode=verify-failure --loop-max=2 --loop-no-progress-limit=1 --loop-on=smell,compile,test -- Project root: /abs/java-project Smell type: long_method Target location: src/main/java/Foo.java:42' \
  | opencode run --command java-refactor-run
```

IDEA CLI 增强路径：

```bash
printf '%s\n' '--verification-mode=local --loop-mode=verify-failure --loop-max=2 --loop-no-progress-limit=1 --loop-on=smell,compile,test -- Project root: /abs/java-project Smell type: long_method Target location: src/main/java/Foo.java:42' \
  | opencode run --command java-refactor-run-idea
```

command 参数与任务正文作为一个完整字符串从 stdin 传入。批处理 runner 也使用同一入口，避免 OpenCode CLI/yargs 将证据中的纯数字 token 转成 number 后破坏消息格式化。

支持的 command policy 参数如下。参数和任务正文必须用独立的 ` -- ` 分隔；参数非法时直接返回 `INVALID_LOOP_POLICY`，不会静默回退。

- `--verification-mode=local|auto|sample_optimized|project_full`
- `--loop-mode=off|verify-failure`
- `--loop-max=0..5`
- `--loop-no-progress-limit=1..5`
- `--loop-on=smell,compile,test`（可取子集）
- `--loop-instruction=...`（如使用，必须是 `--` 前最后一个 policy 参数）
- `--sample-deadline=60..7200`

`smell_verify` 的 `loop.decision` 是唯一的 continuation 决策，插件会在
`session.idle` 时自动恢复同一个 session；TUI、`opencode run`、`serve`、
`web`、`attach` 和批处理使用相同机制，不需要环境开关，也不需要模型传入
`autoContinue`。自动恢复与模型主动继续共享 `--loop-max` 预算，不会叠加第二套
重试次数。没有通过 command 启动的直接 `smell_verify` 会使用默认 policy
（`verify-failure`、最多 2 次 continuation）。

verify 的 `resolution` 分两层：`resolved` 表示检测器不再报告目标异味，是唯一
提前终止通行证；`improved` 表示 checkpoint 确认"真实生产 diff + 任一目标指标
相对基线下降"（此时 build/test 也会强制执行）。`improved` 不会终止 loop：插件
按同一预算让 session 继续冲 `resolved`，并把剩余检测器信号和"不要回撤已保存
的 best partial 收益"注入 continue 提示；预算（deadline、max_continuations、
no_progress）耗尽时按最终工作树的真实状态结算 `resolved / improved / failed`。

`--sample-deadline` 是唯一的时间预算入口。批处理 runner、command loop 和最终独立 verify 都从该值派生：loop 使用原值，runner 只额外保留 60 秒给 OpenCode 正常退出，最终 verify 使用同一预算。runner 不再提供独立的 `--timeout`、`--verify-timeout` 或基于日志静默的 `--opencode-log-idle-timeout`，避免外层时限提前截断 loop。

如果 OpenCode 到达截止时间但最终独立 verify 仍完整通过，结果记为 `PASS_AFTER_OPENCODE_TIMEOUT`；该状态表示重构验收通过，但模型进程未在预算内正常结束。

交互式 OpenCode 会话中也可以使用相同 command；参数契约不变：

```text
/java-refactor-run --verification-mode=local --loop-max=2 -- Project root: /abs/java-project; Smell type: long_method; Target location: src/main/java/Foo.java:42
/java-refactor-run-idea --verification-mode=local --loop-max=2 -- Project root: /abs/java-project; Smell type: long_method; Target location: src/main/java/Foo.java:42
```

## 6. 批量运行数据集

批量 runner 会把每一行数据集转换为完整任务输入，然后调用同一套公开 agent。

未指定 `--agent` 或 `--idea` 时，默认使用直接编辑路径：`java-refactor-agent`。

直接编辑路径：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode local \
  --agent java-refactor-agent
```

IDEA CLI 增强路径：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode local \
  --agent java-refactor-agent-idea
```

也可以使用简写：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode local \
  --idea
```

## 7. 查看批量输出

runner 默认为每个样本创建独立 Git checkout。checkout 位于容器原生的临时文件系统，`runs/` 挂载目录只保存可交付结果，避免 NTFS 等宿主机文件系统破坏 Git 权限语义。运行完成后查看：

```text
runs/<run-name>/results.csv
runs/<run-name>/samples/<sample>/task.txt
runs/<run-name>/samples/<sample>/run.log
runs/<run-name>/samples/<sample>/verify.json
runs/<run-name>/samples/<sample>/result.json
runs/<run-name>/samples/<sample>/diff.patch
runs/<run-name>/samples/<sample>/diff.stat
runs/<run-name>/samples/<sample>/sample.json
runs/<run-name>/samples/<sample>/command.json
runs/<run-name>/samples/<sample>/opencode.runtime.json
```

`results.csv` 是批量汇总。单样本最终是否通过，以 `verify.json` 和 `results.csv` 中的 `status` 为准。

## 8. 验证模式

默认验证模式是 `local`，只运行 Python smell guard，并记录 diff/status 快照。

只有明确需要严格 build/test 时，才使用：

- `verificationMode="auto"`
- `verificationMode="sample_optimized"`
- `verificationMode="project_full"`

## 9. GitHub 协作边界

协作服务器直接 clone 本仓库，并把仓库根目录只读挂载到环境镜像的
`/agent-src`。仓库是 agent 源码的唯一真相：

- `.opencode/`：agent、command、skill 和插件
- `runtime/`：Python bridge、guard 和 checkpoint runtime
- `scripts/`：dataset runner、baseline 验证和源码自检
- `docker/java-refactor-delivery/entrypoint.sh`：环境镜像调用的运行入口
- `package.json`、`package-lock.json`、`opencode.json`：固定依赖与配置

环境镜像负责提供 IDEA、Java 项目、delivery dataset、OpenCode/Node 依赖、
离线构建缓存、项目版本清单和项目级验证配置。上述环境材料不进入本仓库。
`runs/`、`node_modules/`、Python cache、本地项目配置和临时 worktree 也由
`.gitignore` 排除。

clone 后先验证源码契约：

```bash
npm ci
npm run check
npm run check:self
```

## 10. 使用环境镜像

下面的 `<environment-image>` 由环境提供方给出。无需构建或修改镜像：

```bash
git clone <repository-url> opencode-java-refactor
cd opencode-java-refactor
mkdir -p runs

docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  <environment-image> self-check
```

断网 baseline 验证不调用模型：

```bash
docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  <environment-image> baseline-check
```

运行一个真实样本时，key 仅通过环境变量或临时只读 secret 文件提供：

```bash
docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  -e SMELL_OPENCODE_API_KEY \
  <environment-image> \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url https://api.z.ai/api/coding/paas/v4 \
  --verification-mode sample_optimized \
  --agent java-refactor-agent
```

IDEA 路径使用 `--agent java-refactor-agent-idea`。更新 agent 时只需在服务器
拉取新的 Git commit 并启动新容器，不需要重建环境镜像。
