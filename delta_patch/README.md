# OpenCode Java Smell Dependency Patch

本目录用于把已有旧镜像升级为当前 Java smell refactor 最小交付形态。

补丁不重新打包 Java 项目快照、数据集、OpenCode 二进制、IDEA CLI 或模型配置；这些继续来自已有旧镜像。补丁覆盖新的 OpenCode agent/skill/plugin、Python bridge、Java smell guard runtime、批量 runner，并把运行时对齐到参考补丁的 Node 18.19.1、OpenCode 1.17.8 和 OpenCode plugin 1.15.10。

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

默认 Node 版本是 `18.19.1`，并要求基础镜像中的 OpenCode 为 `1.17.8`。如需使用内部 Node 分发地址，可在构建时设置 `NODE_DIST_BASE`。

## 3. 先校验补丁包

在本目录执行：

```bash
scripts/verify_delta_package.sh
sha256sum -c SHA256SUMS
```

校验内容：

- 必要文件是否存在
- payload 是否包含 `.opencode`、`runtime/python`、`scripts/run_smell_dataset.py`
- package 和 lockfile 是否锁定 `@opencode-ai/plugin@1.15.10`
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
NODE_VERSION=18.19.1 \
OPENCODE_VERSION=1.17.8 \
NODE_DIST_BASE=https://nodejs.org/dist \
  scripts/build_delta_image.sh <旧镜像名> opencode-java-refactor-delivery:0.1.1-patch
```

构建过程会检查：

- `opencode` 和 `rg` 是否存在
- `node --version` 是否为 `v18.19.1`
- `opencode --version` 是否为 `1.17.8`
- `@opencode-ai/plugin` 是否为 `1.15.10`
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

正式交付前还应运行 baseline build/sample-test 门禁：

```bash
mkdir -p "$PWD/runs"
docker run --rm \
  -v "$PWD/runs:/runs" \
  opencode-java-refactor-delivery:0.1.1-patch \
  baseline-check
```

该命令不访问模型。它逐条处理数据集样本，在独立 Git checkout 中依次执行当前样本的 build 和 test，不跨样本去重 test command 或复用 build 结果；任一失败都会返回非零，并写出 `runs/baseline-preflight.json`。镜像通过 Gradle 官方 init DSL 禁用专用离线缓存的运行时清理，避免预置依赖被 Gradle GC 删除。

薄层构建会同时把数据集 `project_path` 归一化为 `/opt/projects/<project_name>`、固定 `TZ=Asia/Shanghai`，并从验证命令中隔离 Checkstyle/Spotless 格式门禁。构建期 baseline 结构预检会拒绝源码文本锚点和绑定长参数目标签名的反射测试；项目测试变更必须先进入镜像内对应项目的 Git `HEAD`。

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

- Node 18.19.1
- OpenCode 1.17.8
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
- OpenCode plugin 依赖与参考补丁对齐到 `@opencode-ai/plugin@1.15.10`。
- 新增 `scripts/self_check_smell_verify.mjs`，用于定位 bridge、插件返回结构和 OpenCode 工具结果消费问题。
- 新增 command 级统一 loop policy；用户从初始 `java-refactor-run` 参数控制验证和续跑，见第 13 节。
- 批量 runner 改为调用同一 command，只启动一次 OpenCode；native agent loop 结束后做一次独立审计，见第 14 节。

## 13. Command 级统一 loop policy

```bash
opencode run --command java-refactor-run -- \
  --verification-mode=local --loop-mode=verify-failure --loop-max=2 \
  --loop-no-progress-limit=1 --loop-on=smell,compile,test -- \
  Project root: /abs/project Smell type: long_method Target location: src/main/java/Foo.java:42
```

第一个 `--` 结束 OpenCode 自身 flag 解析，第二个 `--` 分隔 policy 与任务正文。插件通过
`command.execute.before` 获取原始 arguments，调用 Python bridge 统一解析并按 session 固化。
非法参数返回 `INVALID_LOOP_POLICY`，不静默降级。

`smell_verify` 只负责验证，并返回 `loop.decision`、`termination_reason`、剩余次数和用户指令。
可停止原因包括 `PASS`、`LOOP_DISABLED`、`NON_REPAIRABLE_FAILURE`、
`MAX_CONTINUATIONS_REACHED`、`NO_PROGRESS` 和 `SAMPLE_DEADLINE_REACHED`。

## 14. 批量 runner

runner 使用与手动 command 相同的 policy 参数，并调用 `opencode run --command
java-refactor-run`。它不再维护第二套跨进程 retry allowlist，也不再执行隐式新 session
fallback；command 内的 OpenCode 原生 agent loop 是唯一修复循环。进程结束后 runner 只做一次
独立 `smell_verify` 审计并保存最终 artifacts。

验证：

```bash
npm run check
npm run check:self
python3 scripts/self_check_runner_continue.py
```
