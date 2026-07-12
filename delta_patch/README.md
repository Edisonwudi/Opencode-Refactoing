# OpenCode Java Smell Dependency Patch

本目录用于把已有旧镜像升级为当前 Java smell refactor 最小交付形态。

补丁不重新打包 Java 项目快照、数据集、OpenCode 二进制、IDEA CLI 或模型配置；这些继续来自已有旧镜像。补丁覆盖新的 OpenCode agent/skill/plugin、Python bridge、Java smell guard runtime、批量 runner，并在构建薄层镜像时安装 Node 22 运行时和新版 OpenCode plugin 依赖。

## 1. 确认目录内容

```text
.
├── Dockerfile.delta
├── VERSION
├── README.md
├── SHA256SUMS
├── scripts/
│   ├── build_delta_image.sh
│   ├── patch_running_container.sh
│   └── verify_delta_package.sh
└── payload/
    └── opencode-refactor/
```

`payload/opencode-refactor/` 是当前最小源码包，会被复制到镜像内 `/opt/opencode-refactor`。该目录内部还有一份 README，说明源码包本身的手动模式、批量模式和验证方式。

## 2. 确认旧镜像条件

已有旧镜像需要具备：

- 可用的 `opencode`
- 可用的 `rg`
- Python 3 和 Java smell runtime 所需 Python 依赖
- Java dataset 和项目快照
- 模型 provider/key 配置
- 可选：IDEA CLI 环境

本补丁不复用旧镜像里的 `node_modules`。薄层镜像构建时会下载官方 Node.js Linux x64 包并安装：

- `/usr/local/bin/node`
- `/usr/local/bin/npm`
- `/opt/opencode-refactor/node_modules`
- `/opt/opencode-refactor/.opencode/node_modules`

默认 Node 版本是 `22.22.2`。如需使用内部 Node 分发地址，可在构建时设置 `NODE_DIST_BASE`。

## 3. 先校验补丁包

在本目录执行：

```bash
scripts/verify_delta_package.sh
sha256sum -c SHA256SUMS
```

校验内容：

- 必要文件是否存在
- payload 是否包含 `.opencode`、`runtime/python`、`scripts/run_smell_dataset.py`
- package 和 lockfile 是否锁定 `@opencode-ai/plugin@1.17.13`
- 不应包含 `.DS_Store`、`__pycache__`、`node_modules`
- Python bridge 和 batch runner 能否编译
- 如果当前环境已有 npm 依赖，TypeScript 插件能否 typecheck
- 如果当前环境已有 npm 依赖，`smell_verify` 插件自检能否通过
- 如果当前环境已有 Java smell runtime Python 依赖，`smell_bridge.py verify --help` 能否运行

## 4. 基于旧镜像构建薄层镜像

推荐使用薄层镜像方式。它不会修改旧镜像，只是在旧镜像上覆盖本补丁包并安装新 Node/OpenCode plugin 依赖。

```bash
scripts/build_delta_image.sh <旧镜像名> opencode-java-refactor-delivery:0.1.1-patch
```

构建脚本默认使用无缓存构建，避免旧构建缓存把已经清理掉的本地资源文件带回镜像。如需复用 Docker cache，可显式设置 `NO_CACHE=0`。

例如：

```bash
scripts/build_delta_image.sh opencode-smell-java-delivery:0.1.0-amd64 opencode-java-refactor-delivery:0.1.1-patch
```

如果需要指定 Node 版本或分发地址：

```bash
NODE_VERSION=22.22.2 \
NODE_DIST_BASE=https://nodejs.org/dist \
  scripts/build_delta_image.sh <旧镜像名> opencode-java-refactor-delivery:0.1.1-patch
```

构建过程会检查：

- `opencode` 和 `rg` 是否存在
- `node --version` 是否为 Node 22
- `@opencode-ai/plugin` 是否为 `1.17.13`
- Python bridge 和 batch runner 是否能编译
- TypeScript 插件是否能 typecheck
- `smell_bridge.py verify --help` 是否能运行
- `scripts/self_check_smell_verify.mjs` 是否能真实调用 bridge 和 `smell_verify`

构建后先跑镜像内部自检：

```bash
docker run --rm opencode-java-refactor-delivery:0.1.1-patch self-check
```

该自检不访问模型 API。它会创建临时 Java 项目，直接调用 Python bridge，再加载 OpenCode 插件并执行 `smell_verify`，最后模拟 OpenCode 对工具结果做 `split()` 和 JSON 解析。

镜像内部 `self-check` 还会读取一条真实数据集样本：

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --verification-mode local \
  --dry-run
```

这一步验证 dataset runner 能读取真实 CSV，并能选中固定样本 `8`。它仍然不访问模型 API。

## 5. 配置模型认证

批量 runner 会为每个样本生成临时 `opencode.runtime.json`。模型认证和 baseURL 可以直接在 Docker 批量命令里配置。

推荐方式是把 key 放在环境变量里，runner 命令只引用环境变量名：

```bash
-e SMELL_OPENCODE_API_KEY="<api-key>" \
...
--opencode-api-key-env SMELL_OPENCODE_API_KEY \
--opencode-base-url https://api.z.ai/api/coding/paas/v4
```

对于 `zai/*` 模型，默认 baseURL 已经是 `https://api.z.ai/api/coding/paas/v4`；仍建议在交付验证命令里显式写出，便于排查环境差异。

也可以使用已有 OpenCode auth 文件：

```bash
-v /path/to/auth.json:/auth/opencode-auth.json:ro \
-e OPENCODE_AUTH_JSON=/auth/opencode-auth.json
```

如果旧镜像已经在 `/opt/buildenv/offline-home/.local/share/opencode/auth.json` 提供 auth，可以省略这两个参数。

key 来源优先级为：`--opencode-api-key`、`--opencode-api-key-env`、`--opencode-auth-json`/`OPENCODE_AUTH_JSON`。不建议在共享脚本中使用 `--opencode-api-key`，避免把明文 key 写入命令历史或日志。

## 6. 跑一个批量样本

未指定 `--agent` 或 `--idea` 时，默认使用直接编辑路径：`java-refactor-agent`。

直接编辑路径：

```bash
docker run --rm \
  -v "$PWD/runs:/runs" \
  -e SMELL_OPENCODE_API_KEY="<api-key>" \
  opencode-java-refactor-delivery:0.1.1-patch \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url https://api.z.ai/api/coding/paas/v4 \
  --verification-mode local \
  --agent java-refactor-agent
```

IDEA CLI 增强路径：

```bash
docker run --rm \
  -v "$PWD/runs:/runs" \
  -e SMELL_OPENCODE_API_KEY="<api-key>" \
  opencode-java-refactor-delivery:0.1.1-patch \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url https://api.z.ai/api/coding/paas/v4 \
  --verification-mode local \
  --agent java-refactor-agent-idea
```

也可以使用 `--idea` 作为 IDEA 路径简写。

## 7. 批量运行完整数据集

去掉 `--sample-id` 即可跑整个 CSV：

```bash
docker run --rm \
  -v "$PWD/runs:/runs" \
  -e SMELL_OPENCODE_API_KEY="<api-key>" \
  opencode-java-refactor-delivery:0.1.1-patch \
  --dataset /opt/dataset/java/delivery_schema/long_method.csv \
  --model zai/glm-4.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url https://api.z.ai/api/coding/paas/v4 \
  --verification-mode local \
  --agent java-refactor-agent
```

严格 build/test 验证只在明确需要时启用：

```bash
--verification-mode auto
--verification-mode sample_optimized
--verification-mode project_full
```

## 8. 查看运行输出

容器内输出目录是 `/runs`，上面的命令会把它挂载到当前目录的 `runs/`。

常用文件：

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

## 9. 手动模式

进入容器 shell：

```bash
docker run --rm -it \
  -v "$PWD/runs:/runs" \
  -v /path/to/auth.json:/auth/opencode-auth.json:ro \
  -e OPENCODE_AUTH_JSON=/auth/opencode-auth.json \
  --entrypoint bash \
  opencode-java-refactor-delivery:0.1.1-patch
```

在目标 Java 项目目录中运行：

```bash
opencode run "<包含 project root、smell、location、evidence 的完整任务输入>" --agent java-refactor-agent
opencode run "<包含 project root、smell、location、evidence 的完整任务输入>" --agent java-refactor-agent-idea
```

## 10. 临时覆盖已有容器

如果只想临时验证，不想 build 新镜像：

```bash
scripts/patch_running_container.sh <running-container-name-or-id>
```

该方式会把 payload 覆盖到容器内 `/opt/opencode-refactor`，并运行基本编译检查。它适合 smoke test，不建议作为最终可追溯交付方式。

该方式要求运行中容器已经具备：

- Node 22
- npm
- 可联网或已配置 npm registry，以便执行 `npm ci`

如果运行中容器不满足这些条件，脚本会直接失败。此时使用第 4 步的薄层镜像方式。

## 11. 回滚

薄层镜像不会修改旧镜像。回滚时直接继续使用已有旧镜像即可。

如果使用了 `patch_running_container.sh`，请丢弃该容器或从旧镜像重新启动容器。

## 12. 本版覆盖内容

- 新增 `java-refactor-agent`：无 IDEA CLI 的直接编辑路径。
- 新增 `java-refactor-agent-idea`：带 IDEA CLI 增强的路径。
- 新增 `/java-refactor-run` 和 `/java-refactor-run-idea` 命令入口。
- skill 收敛为：
  - `java-smell-edit-patterns`
  - `idea-refactor-cli`
- OpenCode 插件只暴露 `smell_verify`，异味上下文由用户输入或批量 runner 注入。
- 批量 runner 统一走同一套 agent，支持 direct 和 idea 两种模式。
- 默认验证为 `local` smell guard；严格 build/test 只在显式 `verificationMode` 下启用。
- OpenCode plugin 依赖升级到 `@opencode-ai/plugin@1.17.13`。
- 新增 `scripts/self_check_smell_verify.mjs`，用于定位 bridge、插件返回结构和 OpenCode 工具结果消费问题。
- 新增交互模式下的 `session.idle` 有限自动续跑兜底机制（默认关闭），见第 13 节。
- 新增批量 runner 单样本失败重试 loop（最多 2 轮，复用插件 allowlist），见第 14 节。

## 13. 交互模式 session.idle 自动续跑

OpenCode 插件内置一个交互模式兜底机制：当 `java-refactor-agent` 或
`java-refactor-agent-idea` 在最新一次 `smell_verify` 返回可修复失败后提前结束并进入
`session.idle`，插件会向同一 session 注入一条**对用户可见**的 continuation 消息，让
agent 读取 `failure_pack` 再做一次窄修复并重新验证。它只在 agent 提前结束时触发，优先
依赖 OpenCode 原生 agentic loop。

该机制**默认关闭**。

启用命令（TUI 交互模式）：

```bash
SMELL_IDLE_CONTINUE_MODE=interactive opencode
```

`SMELL_IDLE_CONTINUE_MODE` 取值：

- `off`（默认）：完全不记录、不续跑。未识别的值一律按 `off` 处理。
- `shadow`：判断是否应续跑并写结构化日志，但**不**调用 `promptAsync`。用于在不影响
  对话的前提下验证触发条件。
- `interactive`：满足全部条件后调用 `promptAsync` 注入续跑消息。

真正调用 `promptAsync` 必须同时满足：

1. `SMELL_IDLE_CONTINUE_MODE=interactive`
2. 本次 `smell_verify` 显式传入 `autoContinue=true`（两个 agent 默认已传）

强制不续跑的场景：

- 批量模式：`SMELL_BATCH_RUN=1` 或存在 `SMELL_PROJECT_ROOT`
- 单次运行模式：`opencode run`（按 `process.argv` 识别 `run` 子命令，识别不确定时保守不续跑）
- agent 不是 `java-refactor-agent` / `java-refactor-agent-idea`
- 失败不可修复（依赖/离线/授权/Provider/模型/配置/工具/基础设施/超时/未知失败）
- 已达到最大续跑次数

边界：

- 最多自动续跑 **2 轮**，硬限制，不可通过模型参数提升。
- 续跑消息对用户可见，包含当前轮次、上限、status、failure category 和少量 failure
  highlights / artifact paths；总长度限制在约 2 KB；不含 API key、auth 内容或环境变量值。
- 同一 `session.idle` 不重复注入；每次 `smell_verify` 失败最多触发一次 continuation。
- 用户随时可用 `Esc` / `Ctrl+C` 中断。用户真正发送的新消息会清空该 session 的续跑状态；
  插件自己注入的 `[smell-auto-continue ...]` 消息不会重置。
- `session.deleted` 与插件 dispose 时清理状态；状态超过 30 分钟自动清理。
- 不写磁盘状态，不新增数据库、MCP、依赖或第三个 agent。
- `opencode run` 和批量模式**不使用**该机制；批量模式仍由 runner 负责续跑。

验证：

```bash
npm run check:self   # 含 fake-client 集成自检，不访问真实模型
```

## 14. 批量 runner 单样本失败重试

批量 runner（`scripts/run_smell_dataset.py`）现在对**可修复失败**的单样本自动重试,真正让批量场景"吃到 loop"。

为什么是 runner 而不是插件：`opencode run` 是单次进程,Agent 结束即退出,**不发 `session.idle`**,所以插件续跑在批量下无法触发。批量续跑由 runner 统一负责。

**同一 session 续跑(保留完整对话历史)**:

重试使用 `opencode run -s <session_id>` 在**同一 session** 上续跑,而不是起新 session。Agent 在重试时**保留完整对话历史**——能看到上一轮的思考、工具调用、编辑过程,而不是只看到一段失败摘要。首轮用 `--format json` 创建 session 并解析 session id;重试用 `--session <id>` 续接。

规则：

- **最多重试 2 轮**（`MAX_RUNNER_CONTINUE_ATTEMPTS = 2`），与插件 `MAX_IDLE_CONTINUE_ATTEMPTS` 对齐。
- **只重试可修复失败**：`SMELL_GUARD_FAILED`、`BUILD_COMPILE_ERROR`、`TEST_BEHAVIOR_REGRESSION`、`TEST_REFLECTION_ENTRY_STALE`、`SAMPLE_TEST_FAILED`（`REPAIRABLE_FAILURE_CATEGORIES`,镜像自插件 `REPAIRABLE_CATEGORIES`)。
- **不重试**：依赖/离线、授权/Provider/模型、超时、配置、工具、基础设施、未知失败。
- **同 session 续跑**：重试用 `run -s <id>`,agent 看到完整历史。续跑提示只含简短的 failure category 指引(不需重复全部 highlights,agent 能从对话历史里看到)。
- **worktree 复用**：重试在同一 git worktree 上继续,保留上一轮的编辑。
- **每轮独立超时**：每次重试获得完整的 `--timeout` 秒数(不做总预算),与 `--verify-timeout` 一致。
- **降级安全**:如果 session id 解析失败(事件格式变化等),回退为新 session + 完整 failure_context,保证不崩。

产物：

- 每轮产物带后缀留存:`run.log.1`/`run.events.jsonl.1`/`verify.json.1`/`task.txt.1`/`command.json.1`/`diff.patch.1`/`diff.stat.1`(首轮无后缀)。`run.events.jsonl` 是 `--format json` 的原始事件流,含 session id 和 agent 的工具调用事件。
- 首轮产物在 promote 时移到 `.0`,最终轮复制到无后缀路径,保证现有消费脚本兼容。
- `results.csv` 字段不变(向后兼容);重试信息记在 `note` 列(`attempts=N`、`final_category=...`)。
- `result.json` 增加完整 `attempts` 汇总(含每轮 `session_id`、`is_continuation`)。

停止条件(任一即停):

1. 当前轮 `status == PASS`
2. 当前轮失败分类不在 allowlist(不可修复)
3. 已达到 2 轮重试上限

验证：

```bash
python3 -m py_compile scripts/run_smell_dataset.py
python3 scripts/self_check_runner_continue.py   # 纯函数 + loop 决策 + session-id 解析,不跑模型
```

`self_check_runner_continue.py` 覆盖:PASS 不重试、可修复重试、不可修复不重试、上限耗尽停止、failure_context 拼装、常量对齐。不访问真实模型。

