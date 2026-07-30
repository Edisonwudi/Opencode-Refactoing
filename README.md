# OpenCode 异味自动重构交付包

单 Agent Java 异味自动重构系统：OpenCode 插件 `smell_verify` + Python
checkpoint 契约（通用 contract + 每种异味的指标适配器）。Agent 负责定位并
重构，checkpoint 负责用"前后指标对比 + build/test"做真实验收。

- 本仓库（GitHub）：agent 源码、checkpoint 契约与适配器、批量 runner、自检、文档。
- 四个环境镜像（java / python / c / cpp，压缩包单独交付，不进 Git）：
  语言工具链、项目、离线依赖缓存、dataset、OpenCode/Node 运行时。当前 Java
  环境镜像不包含 IDEA/IDEA-Refactoring。

> 拿到仓库和镜像后，照第 1 节的编号顺序执行即可跑通第一个样本。

---

## 1. 拿到仓库 + 镜像后的完整流程

### 1.1 克隆仓库

```bash
git clone https://github.com/Edisonwudi/Opencode-Refactoing.git
cd Opencode-Refactoing
```

### 1.2 把镜像压缩包放进 `images/` 并校验

```bash
mkdir -p images
cp /path/to/smell-refactor-env-java.tar.gz images/
cp /path/to/SHA256SUMS images/
(cd images && sha256sum -c SHA256SUMS)
```

`images/` 已在 `.gitignore` 中，不会被提交。压缩包与镜像 tag、hash 的
对照见 `delivery/README.md`。四个镜像均为 **linux/amd64**，在 amd64
Linux 主机上开箱即用；ARM 主机（如 Apple Silicon）需
`--platform linux/amd64` 仿真运行（慢）或按 Dockerfile 重建 ARM 版。
这里只运行 Java 时只需 Java 归档；同时交付其他语言时，再把对应归档加入
`images/`，并使用覆盖实际归档集合的 `SHA256SUMS`。

### 1.3 载入镜像

```bash
docker load -i images/smell-refactor-env-java.tar.gz
docker image inspect \
  opencode-java-refactor-env:0.1.1-rb-certified-no-idea-mounted-source-v2 \
  --format '{{.Id}}'
```

如交付了其他语言，再分别 `docker load` 对应归档。

### 1.4 安装本地依赖并验证源码契约

```bash
npm ci && (cd .opencode && npm ci && cd ..)
python3 -m pip install pyyaml tree_sitter tree_sitter_language_pack
# Ubuntu 24.04(PEP 668)请改用 venv,或加 --break-system-packages
npm run check && npm run check:self
```

要求：Node.js ≥ 18（推荐 22)、Python ≥ 3.10、Docker 24+。

### 1.5 配置模型（环境变量，见第 2 节）

```bash
export SMELL_OPENCODE_API_KEY="<api-key>"
export SMELL_OPENCODE_BASE_URL="https://api.minimaxi.com/v1"   # 按 provider 选择,见下表
```

### 1.6 容器自检（不调用模型）

以 Java 镜像为例（其余语言换镜像名即可）：

```bash
docker run --rm \
  --pull=never \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  opencode-java-refactor-env:0.1.1-rb-certified-no-idea-mounted-source-v2 \
  self-check
```

`self-check` 会校验：只读挂载、lockfile hash、运行时依赖、项目版本清单、
项目级验证配置，全部就位才放行。

**非 Java 镜像注意**：`self-check` 默认走 Java dataset 路径，非 Java 镜像
必须显式指定本语言的 dataset:

```bash
docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  opencode-smell-python-refactor-env:0.1.1-amd64-delivery-20260720 \
  self-check --dataset-smoke-dataset /opt/dataset/smells/python/long_method_30.csv
```

### 1.7 跑一个真实样本

```bash
: "${SMELL_OPENCODE_API_KEY:?请先设置并 export SMELL_OPENCODE_API_KEY}"

docker run --rm \
  --pull=never \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  -e SMELL_OPENCODE_API_KEY \
  opencode-java-refactor-env:0.1.1-rb-certified-no-idea-mounted-source-v2 \
  --dataset /opt/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode sample_optimized \
  --agent java-refactor-agent
```

当前 Java 环境镜像已移除 IDEA 运行时，只支持
`--agent java-refactor-agent`。如需 IDEA 语义重构，应使用单独的 IDEA
开发镜像，不能在本交付镜像中选择 `java-refactor-agent-idea`。

### 1.8 看结果

```text
runs/<run-name>/results.csv          # 汇总:status / 耗时 / continuation 次数
runs/<run-name>/samples/<sample>/
  verify.json        # 最终验收:status、resolution、checkpoint 指标对比、build/test
  diff.patch         # 生产源码 diff(有效 PASS 的必要条件)
  run.log            # opencode 过程日志
  failure_pack       # 失败时的结构化原因(在 verify.json 内)
```

### 1.9 日常更新

```bash
git pull    # agent 源码即最新版;镜像无需任何操作
```

Java 环境镜像的唯一构建入口是
`docker/java-refactor-delivery/Dockerfile.mounted-source`。该 Dockerfile
只允许固化 Java 工具链、项目快照、dataset、离线仓库、Node/OpenCode 依赖及版本清单；
Agent prompt、skill、plugin、checkpoint、Python runtime 和 runner 必须在运行时
从 `/agent-src` 只读装配。`npm run check:mounted-source` 会阻断重新复制这些源码
或绕过挂载入口的 Dockerfile 改动。

只有以下环境契约变化才重建镜像：Java 工具链、项目快照、dataset、离线依赖，
或 `package-lock.json` / `.opencode/package-lock.json`。其余 Agent 逻辑更新只
同步仓库；实验结果同时记录 Git commit 与环境镜像 ID，避免把两种版本混为一谈。

只有环境本身（Java 工具链、项目快照、依赖缓存、dataset）变化才需要重新
交付镜像（回到 1.2）。

### 1.10 非 Java 语言（python / c / cpp）

非 Java 与 Java 共用同一套 checkpoint 契约、runner 和容器入口，差异只在
镜像、dataset 路径和 agent 选择。

**dataset 与异味范围**：镜像内路径为
`/opt/dataset/smells/<lang>/<smell>_30.csv`，权威源 CSV 在本仓库
`dataset/nonjava/<lang>/<smell>_30.csv`（每语言 10 种异味 × 30 样本，
**已是容器路径格式**——`/opt/projects/<lang>/<name>`，与快照 payload
逐字节一致，容器内可直接通过 `/agent-src` 挂载使用）。
Java 数据集的权威源同样在本仓库 `dataset/java/delivery_schema/<smell>.csv`
（对应镜像内 `/opt/dataset/java/delivery_schema/`，同为容器路径格式）。
8 种基础异味：long_method、long_parameter_list、nested_complexity、
switch_statements、data_clumps、code_clone_type1、god_class、dead_code。
检测层面非 Java 现已
支持全部 10 种通用异味：feature_envy 与 mysterious_name 走 tree-sitter 通用
检测（feature_envy 的接收者按根标识符统计，无类型解析；evidence 优先
`envied_receiver=<名字>`，回退 `envied_type=<类型名>`);feature_envy 计数
带别名折算：把接收者字段缓存进局部变量（`x = r->f`，含元组解包与 walrus)
不会降低访问计数——别名后续的每次读取使用（裸名或后接属性）都折算为对
原接收者的一次访问，重赋非别名值才解除；refused_bequest 仍
仅 Java 支持，非 Java 暂无对应 dataset。

**agent 选择**：非 Java 样本**省略 `--agent`**(runner 按 CSV 的 `language`
列自动选用 `smell-refactor-agent`)，或显式 `--agent smell-refactor-agent`;
不要传 `--idea` / `java-refactor-agent-idea`（会被 `IDEA_UNSUPPORTED_LANGUAGE`
拒绝）。

**跑一个 python 样本**（c/cpp 只换镜像名和 CSV 路径）：

```bash
docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  -e SMELL_OPENCODE_API_KEY \
  opencode-smell-python-refactor-env:0.1.1-amd64-delivery-20260720 \
  --dataset /opt/dataset/smells/python/long_method_30.csv \
  --sample-id 1 \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode project_full
```

`--dataset` 也可指向仓库内 CSV(如
`/agent-src/dataset/nonjava/python/feature_envy_30.csv`),与镜像内内容
一致;feature_envy 与 mysterious_name 目前**只有**仓库内路径可用(旧
镜像 dataset 未含这两种)。

注意：

- 非 Java 的 build/test 配置来自镜像内 `/opt/buildenv/projects.docker.yaml`
  （仓库内的 `defaults/projects.yaml` 是空的）；**在容器外直接跑 runner**
  时需显式 `--projects <yaml>`。
- 镜像内项目：python 17 个（django、requests、airflow 等）、c 11 个
  (redis、nginx、curl 等）、cpp 12 个（rocksdb、protobuf、fmt 等）。
- 验收语义（resolved / improved、loop 预算、有效 PASS 硬规则）与 Java 完全一致。

---

## 2. 模型配置（环境变量）

key 只允许来自环境变量或 secret 文件，**不要**写进仓库、日志、命令行、
CSV。runner 生成的 `opencode.runtime.json` 里 key 永远只是 `{env:...}` 引用。

| 环境变量 | 作用 | 示例 |
|---|---|---|
| `SMELL_OPENCODE_API_KEY` | 模型 API key | （保密，轮换暴露过的 key） |
| `SMELL_OPENCODE_BASE_URL` | provider 的 OpenAI 兼容端点 | `https://api.minimaxi.com/v1` |
| `OPENCODE_AUTH_JSON` | 可选：已有 opencode auth 文件路径 | `/abs/path/auth.json` |

`--model` 的形式是 `provider/model-id`，配合 runner 参数
`--opencode-api-key-env <环境变量名>` 与 `--opencode-base-url <端点>`：

| provider | `--model` 示例 | baseURL |
|---|---|---|
| minimax | `minimax/MiniMax-M2.7` | `https://api.minimaxi.com/v1` |
| zai | `zai/glm-4.7` | `https://api.z.ai/api/coding/paas/v4`（默认可省略） |

key 来源优先级：`--opencode-api-key`（不推荐）>
`--opencode-api-key-env`（推荐）> `OPENCODE_AUTH_JSON` / 镜像内置 auth。

---

## 3. 机制与结果语义（最新版）

### 3.1 两层验收：contract + adapter

- **通用 contract**(`runtime/python/smell_core/checkpoint_contract.py`):
  统一处理 baseline、生产 diff、指标差值、build/test、失败原因与续跑条件。
- **异味 adapter**(`checkpoint_adapters.py`,11 种异味全覆盖）：只负责该
  异味的指标采集、目标定位与改善判断（如 god_class 取 nom/wmc/loc/atfd,
  feature_envy 取 expected_receiver_access,long_method 取 ast_ncss)。

### 3.2 PASS 的两个层级（`resolution`）

- `resolved`：检测器不再报告目标异味。这是**唯一提前终止通行证**。
- `improved`:checkpoint 确认"真实生产 diff + 任一目标指标相对基线下降"
  （此时 build/test 也会强制执行）。`improved` 不终止 loop：插件按同一
  预算让 agent 继续冲 `resolved`，并把剩余检测器信号和"不要回撤已保存
  的 best partial"注入续跑提示；预算耗尽后按最终工作树的真实状态结算
  `resolved / improved / failed`。

### 3.3 有效 PASS 的硬规则

- 必须有真实、非空、命中目标异味的生产源码 diff；无 diff PASS 恒为 0。
- 只改注释/格式/测试/生成文件不算（`EDIT_REQUIRED`)。
- 目标实体被改到"找不到"不算改善（`TARGET_NOT_LOCATED`；真删除由严格
  检测器复核原签名是否仍在，只有 dead_code / mysterious_name 以消失为目标）。
- build/test 回归按 `BUILD_FAILED` / `TEST_FAILED` / `SAMPLE_TEST_FAILED`
  如实归因，不会被 smell 判定吞掉。

### 3.4 loop 与预算

- 唯一时间预算：`--sample-deadline`（默认 1800s，范围 60–7200)，没有
  step 数上限。
- 续跑预算：`--loop-max`（默认 3，范围 0–5）、
  `--loop-no-progress-limit`（默认 2)、`--loop-mode=verify-failure`。
- 预算内 checkpoint 失败会把"基线/当前/差值/失败原因/下一步建议"反馈
  回同一 session 继续修复。

---

## 4. 手动使用（不经 runner)

交互式 OpenCode 会话或 `opencode run` 中直接给完整任务输入；loop 策略
由 command 前缀显式声明（与批量 runner 同一入口）：

```text
/java-refactor-run --verification-mode=sample_optimized --loop-max=2 -- Project root: /abs/java-project; Smell type: long_method; Target location: src/main/java/Foo.java:42
```

支持的 policy 参数：`--verification-mode=local|auto|sample_optimized|project_full`、
`--loop-mode=off|verify-failure`、`--loop-max=0..5`、
`--loop-no-progress-limit=1..5`、`--loop-on=smell,compile,test`、
`--sample-deadline=60..7200`。参数非法直接报 `INVALID_LOOP_POLICY`。

验证模式：`local` 只跑 Python guard（不含 build/test）;
`sample_optimized` / `auto` / `project_full` 才执行严格 build/test。

---

## 5. 批量使用（runner)

```bash
python3 scripts/run_smell_dataset.py \
  --dataset /opt/dataset/java/delivery_schema/<smell>.csv \
  --sample-id <id> \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode sample_optimized \
  --agent java-refactor-agent        # 或 --idea
```

要点：

- 每个样本独立 git checkout、独立容器，requested commit/tree 与 actual
  必须一致，禁止 HEAD fallback。
- 离线约束：Maven/Gradle 全部走镜像内离线仓库；模型 API 是唯一外联。
- 外部并发控制器使用交付镜像的 `benchmark-worker` 入口时，必须同时提供
  `--results-root`。入口会创建可写的 `<results-root>/artifacts`，并将
  `SMELL_ARTIFACT_ROOT` 唯一设置为该目录后再降权启动 worker；目录不可写时
  会在调用模型前以退出码 73 失败。不要另行挂载未对齐的 `/runs` artifact
  目录。
- 更多 runner 参数（`--limit`、`--offset`、`--projects`、
  `--project-revisions`、dry-run 等）见 `python3 scripts/run_smell_dataset.py --help`。

外部并发控制器的容器调用契约如下；`run_worker.py` 与 plan 由控制器提供，
而 Agent 源码始终从当前 Git checkout 只读挂载：

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

---

## 6. 仓库结构

```text
.opencode/agents/            三个公开 agent(java-refactor-agent[-idea]、smell-refactor-agent)
.opencode/commands/          java-refactor-run[-idea]、smell-refactor-run(loop policy 入口)
.opencode/plugins/smell.ts   smell_verify 工具 + loop 状态机
.opencode/skills/            编辑模式与 IDEA 重构路径知识
runtime/python/bridge/       smell_bridge(verify/capture-baseline/guard 入口)
runtime/python/smell_core/   checkpoint contract、adapters、检测器、guards
scripts/                     run_smell_dataset.py 与全部自检
delivery/                    交付镜像清单(tag / sha256 / 使用说明)
docker/                      mounted-source 与 delivery entrypoint
```

自检：`npm run check`、`npm run check:self`，以及 `scripts/self_check_*.py`
（契约、各 adapter、guard、runner 续跑、LPL 兜底等回归用例）。

约定：实验结果、worktree、`runs/`、`node_modules/`、`images/` 都不进 Git;
key 不进任何文件。
