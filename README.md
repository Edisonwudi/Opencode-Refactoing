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

直接编辑路径：

```bash
opencode run "<包含 project root、smell、location、evidence 的完整任务输入>" \
  --agent "java-refactor-agent"
```

IDEA CLI 增强路径：

```bash
opencode run "<包含 project root、smell、location、evidence 的完整任务输入>" \
  --agent "java-refactor-agent-idea"
```

交互式 OpenCode 会话中也可以使用命令包装器。它们不会额外构造或注入异味上下文，只负责选择对应 agent，并把剩余文本透传给 agent：

```text
/java-refactor-run --project-root /abs/java-project --smell long_method --location src/main/java/Foo.java:42 --language java
/java-refactor-run-idea --project-root /abs/java-project --smell long_method --location src/main/java/Foo.java:42 --language java
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

runner 默认为每个样本创建 git worktree。运行完成后查看：

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

## 9. Docker 完整镜像

如果需要构建完整镜像：

```bash
docker build \
  -f docker/java-refactor-delivery/Dockerfile \
  --build-arg NODE_VERSION=22.22.2 \
  -t opencode-java-refactor-delivery:0.1.1-amd64 \
  .
```

镜像构建阶段会重新安装 `@opencode-ai/plugin@1.17.13`，并执行 `scripts/self_check_smell_verify.mjs`。

Docker 内部自检：

```bash
docker run --rm opencode-java-refactor-delivery:0.1.1-amd64 self-check
```

镜像内部 `self-check` 会额外读取真实数据集样本：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --verification-mode local \
  --dry-run
```

该步骤只验证 dataset runner 能读取真实 CSV 并选中固定样本，不访问模型 API。

Docker 批量 dry-run：

```bash
docker run --rm opencode-java-refactor-delivery:0.1.1-amd64 \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --dry-run
```

Docker 真实运行：

```bash
docker run --rm \
  -v "$PWD/runs:/runs" \
  -e SMELL_OPENCODE_API_KEY="<api-key>" \
  opencode-java-refactor-delivery:0.1.1-amd64 \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url https://api.z.ai/api/coding/paas/v4 \
  --verification-mode local \
  --agent java-refactor-agent
```

IDEA 路径只需把最后的 agent 改为：

```bash
--agent java-refactor-agent-idea
```
