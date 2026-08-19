# OpenCode 异味自动重构交付包

面向 Java、Python、C、C++ 的单 Agent 自动重构系统。模型负责定位和修改代码，
`smell_verify`、checkpoint 与 Target Guard 负责用冻结目标、生产源码 diff 和
build/test 做独立验收。

仓库包含 Agent、Skill、插件、checkpoint、数据集 runner、自检和文档；四种语言的
环境镜像单独交付，包含固定工具链、项目快照、离线依赖和 OpenCode 运行时。

## 支持范围

| 语言 | 异味范围 | 默认 Agent |
|---|---|---|
| Java | 10 种通用异味 + `refused_bequest` | `java-refactor-agent` |
| Python / C / C++ | 10 种通用异味 | `smell-refactor-agent` |

10 种通用异味：Long Method、Long Parameter List、Nested Complexity、Switch
Statements、Data Clumps、Type-1 Code Clone、God Class、Dead Code、Feature Envy、
Mysterious Name。

当前 Java 交付镜像不包含 IDEA/IDEA-Refactoring。IDEA backend 仅用于另行配置了
IDEA service 的开发环境。

## 快速上手

### 1. 获取仓库和镜像

```bash
git clone https://github.com/Edisonwudi/Opencode-Refactoing.git
cd Opencode-Refactoing

mkdir -p images
cp /path/to/smell-refactor-env-java.tar.gz images/
cp /path/to/SHA256SUMS images/
(cd images && sha256sum -c SHA256SUMS)

docker load -i images/smell-refactor-env-java.tar.gz
```

镜像 tag、归档文件和验收记录见 [`delivery/README.md`](delivery/README.md)。镜像均为
`linux/amd64`；ARM 主机需要仿真运行或按对应 Dockerfile 重建。

### 2. 安装本地依赖并自检源码

```bash
npm ci && (cd .opencode && npm ci && cd ..)
python3 -m pip install pyyaml tree_sitter tree_sitter_language_pack
npm run check
```

要求：Node.js ≥ 18（推荐 22）、Python ≥ 3.10、Docker 24+。Ubuntu 24.04 的系统
Python 建议使用 venv。

### 3. 配置模型

```bash
export SMELL_OPENCODE_API_KEY="<api-key>"
export SMELL_OPENCODE_BASE_URL="https://api.minimaxi.com/v1"
```

key 只放在环境变量或 secret 文件中，不要写进仓库、CSV、日志或命令参数。其他
provider 的配置见 [`docs/advanced-usage.md`](docs/advanced-usage.md)。

### 4. 自检环境镜像

```bash
docker run --rm \
  --pull=never \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  opencode-java-refactor-env:0.1.1-rb-certified-no-idea-mounted-source-v2 \
  self-check
```

该命令不调用模型，会检查只读源码挂载、运行时依赖、项目 revision、离线构建环境和
项目级验证配置。非 Java 镜像需要显式指定本语言的 smoke dataset，命令见
[`docs/language-and-dataset-contracts.md`](docs/language-and-dataset-contracts.md)。

### 5. 跑一个 Java 样本

```bash
: "${SMELL_OPENCODE_API_KEY:?请先设置并 export SMELL_OPENCODE_API_KEY}"

docker run --rm \
  --pull=never \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  -e SMELL_OPENCODE_API_KEY \
  opencode-java-refactor-env:0.1.1-rb-certified-no-idea-mounted-source-v2 \
  --dataset /agent-src/dataset/java/delivery_schema/mysterious_name.csv \
  --sample-id 8 \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode sample_optimized \
  --agent java-refactor-agent
```

Java 产品只注册 `java-refactor-agent`；不再解析旧 IDEA agent/runner 参数。

### 6. 查看结果

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

最先看 `results.csv` 的 `status`、`resolution`、`accepted`、
`termination_reason` 和 `duration_seconds`。正式接受必须是 `PASS / resolved`，且
`accepted=true`；`IMPROVED` 只表示指标改善，仍不接受。完整判定方法见
[`docs/verification-contract.md`](docs/verification-contract.md)。

## 常用运行方式

### Python、C、C++

三种语言共用 runner 和验证合同，只替换镜像与 dataset。以 Python 为例：

```bash
docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  -e SMELL_OPENCODE_API_KEY \
  opencode-smell-python-refactor-env:0.1.1-amd64-delivery-20260720 \
  --dataset /agent-src/dataset/nonjava/python/long_method_30.csv \
  --sample-id 1 \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode project_full
```

Feature Envy、Mysterious Name、Dead Code 清洗集以及非 Java selector/Guard 约束见
[`docs/language-and-dataset-contracts.md`](docs/language-and-dataset-contracts.md)。

### 直接调用 runner

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

真实项目也可通过 `--build-command`、`--project-test-command` 和
`--verification-cwd` 显式声明验证命令。参数优先级、测试迁移、手动 command 和外部
`benchmark-worker` 见 [`docs/advanced-usage.md`](docs/advanced-usage.md)。完整参数以
`python3 scripts/run_smell_dataset.py --help` 为准。

### 原生 OpenCode 能力对照

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

该模式不加载交付 Agent、command、Skill 或插件控制环，也不向模型提供
`smell_verify`。模型单轮退出后，runner 基于预先冻结的 c000 做一次独立最终验证。
它只支持 `direct` backend，且不会降低正式 PASS 合同。实验边界和审计方法见
[`docs/advanced-usage.md`](docs/advanced-usage.md#22-原生-opencode-对照)。

## 结果语义速查

| 结果 | 含义 | 接受 |
|---|---|---:|
| `PASS / resolved` | 冻结目标消失，production diff、Guard、build/test 全部通过 | 是 |
| `IMPROVED / improved` | 指标下降但目标仍存在 | 否 |
| `EDIT_REQUIRED` | 没有有效生产源码改动 | 否 |
| `BUILD_FAILED` / `TEST_FAILED` | 正式构建或测试失败 | 否 |
| `FLAKY_TEST_INCONCLUSIVE` | 同候选测试结果不稳定 | 否 |
| `FINAL_VERIFY_INFRA_FAILED` | 最终验证基础设施失败 | 否 |

不要只根据一个 `status` 字段判断 PASS。`results.csv`、`result.json`、
`verify.json` 与 `runner-final-receipt.json` 必须唯一且语义一致；最终 receipt 使用
`canonical_status`、`canonical_accepted` 记录权威结果。

## 更新与镜像边界

```bash
git pull
npm run check
```

Agent、Skill、plugin、checkpoint、runner 和权威 dataset 从 `/agent-src` 只读装配，
一般只需更新 Git。只有语言工具链、项目快照、离线依赖、lockfile，或需要固化新的
`/opt/dataset` 快照时才重建镜像。

Java 环境镜像的唯一构建入口是
`docker/java-refactor-delivery/Dockerfile.mounted-source`；交付和离线依赖验收流程见
[`delivery/README.md`](delivery/README.md)。

## 文档导航

| 文档 | 适合查什么 |
|---|---|
| [`docs/advanced-usage.md`](docs/advanced-usage.md) | provider、runner 模式、原生对照、手动 command、参数与外部控制器 |
| [`docs/verification-contract.md`](docs/verification-contract.md) | c000、Target Guard、PASS/IMPROVED、loop、最终 receipt 与测试合同 |
| [`docs/language-and-dataset-contracts.md`](docs/language-and-dataset-contracts.md) | 四种语言范围、dataset 路径、非 Java selector、项目原生测试入口 |
| [`docs/legacy-java-image-maven-repair.md`](docs/legacy-java-image-maven-repair.md) | 旧 Java 镜像 Maven 仓库 ID 不匹配的原因与修复流程 |
| [`delivery/README.md`](delivery/README.md) | 镜像 tag/hash、载入、离线依赖和发布验收 |
| [`scripts/README.md`](scripts/README.md) | dataset 与交付物维护、审计脚本 |

`docs/` 下带日期的报告是固定时点审计或实验记录，不是日常运行入口。

## 仓库结构

```text
.opencode/agents/            Java 与多语言 Agent
.opencode/commands/          command loop policy 入口
.opencode/plugins/smell.ts   smell_verify 与 loop 状态机
.opencode/skills/            异味主 Skill 与语言 reference
runtime/python/bridge/       baseline capture / verify 入口
runtime/python/smell_core/   checkpoint、detector 与 Guard
dataset/                     Java 与非 Java 权威 CSV
scripts/                     runner、自检和维护脚本
delivery/                    交付镜像清单与验收说明
docker/                      mounted-source 与 delivery entrypoint
docs/                        使用合同和固定时点审计报告
```

日常全量自检使用 `npm run check`。实验结果、worktree、`runs/`、`node_modules/`、
`images/` 和任何 API key 都不提交到 Git。
