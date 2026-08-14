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

当前 Java 产品只有 `java-refactor-agent` 这一条执行路径，不再注册或解析
旧 IDEA agent/command/runner 参数。

### 1.8 看结果

```text
runs/<run-name>/results.csv          # 汇总:status / 耗时 / continuation 次数
runs/<run-name>/samples/<sample>/
  verify.json        # 有界 decision:status、resolution、指标差值、build/test 摘要和 artifact 路径
  artifacts/.../
    guard-evidence.json # 有界 Guard/checkpoint 证据（按需读取，<2 MiB）
    build.log / test.log / diff.patch # 独立的完整过程证据
  diff.patch         # 生产源码 diff(有效 PASS 的必要条件)
  run.log            # opencode 过程日志
  failure_pack       # 失败时的结构化原因(在 verify.json 内)
```

`results.csv` 会独立记录 `status`、`resolution`、`accepted`、`progress`
和 `termination_reason`，`note` 另记录 `final_verify_source`。无论 OpenCode
是否超时或已在循环中调用过验证，runner 都只执行一次独立的最终
`runner_final` 验收；该结果是终态唯一权威。OpenCode 超时和 Provider 错误仅作为
执行元数据记录，不触发第二套验收。每样本容器内 Maven `install` 产生的临时
本地仓库元数据不会覆盖 smell/build/test 的最终状态。

`smell_verify` 和数据集 runner 默认只传递固定投影的
`smell.verify.decision/v1`（硬上限 64 KiB）；不会把 clone catalog、完整
worklist、测试清单或源码 diff 复制进模型上下文。模型需要进一步诊断时，按
`artifacts` 中的路径读取对应文件。`smell_bridge.py` 的 `--output-detail audit`
仅供自检和人工审计使用。

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
`dataset/nonjava/<lang>/<smell>_30.csv`（除 Dead Code 外，每语言每种异味
30 样本；**已是容器路径格式**——`/opt/projects/<lang>/<name>`，与快照 payload
逐字节一致，容器内可直接通过 `/agent-src` 挂载使用）。
Java 数据集的权威源同样在本仓库 `dataset/java/delivery_schema/<smell>.csv`
（对应镜像内 `/opt/dataset/java/delivery_schema/`，同为容器路径格式）。
8 种基础异味：long_method、long_parameter_list、nested_complexity、
switch_statements、data_clumps、code_clone_type1、god_class、dead_code。
检测层面非 Java 现已
支持全部 10 种通用异味：feature_envy 与 mysterious_name 走 tree-sitter 通用
检测。非 Java Feature Envy 的接收者按根标识符统计，无类型解析；CSV 必须在
`target_context_json.receiver_type` 显式冻结别名折算后的 canonical receiver root，
运行时不从 evidence、类型名或当前 dominant receiver 回退选择。feature_envy 计数
带别名折算：把接收者字段缓存进局部变量（`x = r->f`，含元组解包与 walrus)
不会降低访问计数——别名后续的每次读取使用（裸名或后接属性）都折算为对
原接收者的一次访问，重赋非别名值才解除；refused_bequest 仍
仅 Java 支持，非 Java 暂无对应 dataset。

非 Java Data Clumps 的每一行同时冻结完整关系 witness：`location` 用分号列出
本组所有函数位置，`target_context_json.group` 保存规范化参数组。runner 对缺少
group、少于 3 个位置或缺少方法 selector 的样本直接拒绝。运行时 Guard 只解析这些
显式位置，不遍历项目源码。Mysterious Name 只解析显式目标文件；
非 Java Dead Code 只判断显式目标声明是否仍存在，不在 Guard 内搜索全项目引用。
Feature Envy c000 还冻结目标文件、parser owner/declared name、完整参数 fingerprint
和声明起始行。验证只解析该显式目标文件，并用该文件的 production patch 做旧锚到
当前声明的一对一映射；保留 wrapper 仅允许同一 hunk、相同 owner/name/完整参数身份
且唯一候选的窄重锚。目标删除、跨 hunk、同 hunk 多候选、owner/name/参数身份改变
全部 fail-closed。Mysterious Name 的 `symbol_kind` 与 `symbol_name` 同样在 CSV 的
`target_context_json` 中冻结，不从审计 evidence 反推。三种语言默认记录
`nonjava-target-guard/<language>/<smell>/v2` profile；Feature Envy 使用 v3，
Mysterious Name 使用 v6，Code Clone 使用 v5。Code Clone 的局部编辑闭包只检查
传入 production patch 中完整删除的 exact declaration，不遍历项目源码；只有模型
确实改写了保留端点时，才允许同文件、同一固定 hunk、相同 owner/name/完整签名、
唯一一对一的窄重锚，跨 hunk、owner 改变和多候选继续 fail-closed。

Mysterious Name 的 v6 profile 中，c000 冻结 parser 声明的
container name/owner、symbol kind/name、声明 slot 和声明行；参数只能在同一声明
slot 一对一改名，局部变量只能在目标文件 patch 的同一 hunk 唯一一对一改名。
同 owner/name/参数形状的完整 container cohort 也会冻结声明边界与摘要；验证时必须
在同一目标文件 patch 中建立数量一致、唯一且全局一对一的旧锚到当前锚映射。
新名称仍命中 too-short/low-info（例如 `n -> q`、`tmp -> data`）、改动其他声明、
遗留旧引用、删除或改变 container owner、跨 hunk 或多候选都会 fail-closed。C/C++
允许目标函数之外已经存在的 parser recovery，但目标函数本身必须完整可解析；c000
会冻结文件级 recovery witness，验证时新增 recovery 仍会拒绝。

Dead Code 正例另走 fail-closed 的离线语料审计：全项目引用、宏拼接、注册/回调、
动态协议和公共 API 风险只在整理数据时检查，不进入运行时 Guard。2026-08-09
清洗后的权威文件为各语言目录下的 `dead_code_curated.csv`，样本数分别为
Python 30、C 16、C++ 10；旧 `dead_code_30.csv` 已移除，避免继续把宏调用、
协议钩子、vendored/public header 方法当作正例。在非 Java 环境镜像重建前，
Dead Code 运行必须使用 `/agent-src/dataset/nonjava/<lang>/dead_code_curated.csv`，
不能使用旧镜像内置的 `/opt/dataset/smells/<lang>/dead_code_30.csv`。

`dataset/nonjava` 只保留每种语言、每种异味的一份最终 CSV，不提交候选池、审计
JSON、构建日志或固定 10 条子集。需要子集实验时由调用方从最终 CSV 显式选择样本，
不再维护第二套数据文件。

非 Java dataset-aligned 指标口径为：Nested Complexity 使用显式目标函数内的纯
AST 控制流嵌套深度（3 种语言阈值均为 5）；Long Method 阈值为 Python 50、C/C++
60；Long Parameter List 阈值为 Python 6、C 5、C++ 6；Type-1 Clone 的最小规范化
token 数分别为 Python 17、C 18、C++ 25。上述值是冻结正例语料兼容边界，不代表已经
完成负例误报率校准。

**agent 选择**：非 Java 样本**省略 `--agent`**(runner 按 CSV 的 `language`
列自动选用 `smell-refactor-agent`)，或显式 `--agent smell-refactor-agent`;
Java 样本自动选用 `java-refactor-agent`。runner 不再提供 IDEA 选择参数。

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
  （仓库内的 `defaults/projects.yaml` 是空的）；运行时仅用
  `projects.runtime-overrides.yaml` 对已存在的精确项目 root 做窄覆盖，不复制整份
  镜像配置。Lua 的旧 `./lua -e "print('ok')"` 已被覆盖为上游
  `testes/all.lua` portable user suite；DuckDB 的 `duckdb --version` 已被覆盖为上游
  `test/smoke_tests.list`（清单 253 项；固定 revision 实际调度 252 项）经原生 Catch2
  `unittest` 执行；
  RocksDB 的 `test -f librocksdb.a` 已被覆盖为原生 GTest `db_basic_test` suite。
  三者都会生成 fresh JUnit 执行证据。DuckDB 使用上游 Debug/O0、关闭 sanitizer 的
  build 内两阶段冷构建（core 后增量构建 `unittest`），两步都成功才写 ready stamp
  并进入 test 阶段。默认 1 job、项目级 shell timeout 最低为 1500 秒，以覆盖三容器共享
  两核时的资源放大；这只是 exact-root 预算，不会吞掉失败或重试。独占 CPU 时可用
  `SMELL_BUILD_JOBS` 显式提高。RocksDB 同样在 build 阶段依次完成上游 Debug/O0
  `static_lib` 与增量 `db_basic_test`，默认 1 job、exact-root timeout 1500 秒；test 阶段
  只执行固定 GTest suite。suite 始终固定为项目级入口，不按样本选择测试。
  C 项目的弱 smoke 也由相同的 exact-root overlay 覆盖：Curl 使用 Autotools 冷构建后
  运行上游 `tests/runtests.pl` 非易波动套件；Redis 以 `REDIS_TEST` 编译并执行内建
  `redis-server test all`；libssh2 运行关闭外部 Docker/sshd 依赖的原生 CTest；tmux
  逐项执行仓库 `regress/*.sh`（不再把 GNU Make 对 BSD Makefile 的空跑当成功）。固定
  delivery image 下仅 `utf8-test.sh` 因固定的 display-width 期望与该环境输出不一致而
  被显式记为 skipped，
  其余 34 项必须全部真实通过；nginx
  使用官方 `nginx/nginx-tests` commit
  `1502b87f5fa712ff485a1bb6baeab50153719d03` 的八文件固定行为套件。nginx fixture
  测试以非 root 身份在可写临时目录执行；缺文件、
  runner 不可用或真实测试失败都会直接失败，不退回 `-v`/`--version`/文件存在性检查。
  Git 的 `git --version` 已替换为八个上游 `t/` 行为脚本，覆盖 basic、path、cat-file、
  config、fsck、rev-parse、log 与 line-log；RRDtool 的 `rrdtool --version` 已替换为官方
  Automake `make check`：从配置后 Makefile 的官方 `TESTS` 读取该 revision 的 28 项，
  固定镜像内 27 项实际执行，唯一依赖镜像未提供的外部 `dc` 计算器的
  `create-with-source-4` 被显式记为 skipped；构建生成的测试程序不会被误判为缺失源码脚本。
  libuv 保留十二个稳定原生测试；root 容器会在启动原生测试进程前固定降权为 `nobody`，
  而不是屏蔽项目的 root 检查，并用 JUnit 封装提供可核验的
  非零执行证据。OpenTTD 的 `openttd -h` 已替换为 `openttd_test` 的 94 项 CTest 单元
  套件；两项依赖外部 OpenGFX 的 gameplay regression 不在这个离线入口中，不会被冒充
  为已执行。探索性数据额外涉及的 CMake 与 aria2 也不再使用 `--version`：CMake
  运行八项固定且无外部能力依赖的项目原生 CTest 行为测试，aria2 运行上游 Automake/CppUnit
  `make check`；aria2 在离线容器中通过项目配置关闭 BitTorrent/LPD 外部组播路径，不按
  测试名事后忽略失败。aria2 所需 CppUnit 由
  `docker/cpp-refactor-delivery/Dockerfile.project-test-dependencies` 以固定包版本构建进
  C++ 交付镜像；正式运行仍为 `--network none`，没有运行时安装或备用测试路径。
  fmt、nlohmann/json 与 yt-dlp 继续使用其原生 CTest/pytest。
  当前最终 CSV 涉及的项目测试入口由三份 delivery `projects.docker.yaml` 与
  `projects.runtime-overrides.yaml` 共同提供；交付镜像验收会直接检查解析后的命令，
  不接受版本、帮助、文件存在或 print-only 验证。
  **在容器外直接跑 runner** 时仍需显式 `--projects <yaml>`。
- `project_full` 必须得到 fresh、非零且非全 skipped 的测试执行证据；原生 JUnit XML
  或严格的 pytest、Python `unittest`、CTest、Curl `TESTDONE` 与 Redis native-test
  终态可作为证据；后两者必须与对应真实 runner invocation 同时出现。`--version`、`-v`、
  帮助输出、文件存在性和 collection-only 均不能把返回码 0 变成测试 PASS。
- 最终交付快照在 build/test 前以 c000 冻结 commit 为基线采集完整 Git diff/status，
  包含已提交、已暂存、未暂存、未跟踪生产源码及构建元数据；候选自行 commit 不能
  隐藏改动。禁止的测试源码改动会先于构建测试 fail-closed。
- runner 在 `result.json.attempts` 中保留每次 agent 验证与最终独立验证。同一 diff
  若最后一次 agent 测试失败而 final 单次转绿，标记 `FLAKY_TEST_INCONCLUSIVE`；
  若 agent 已通过而 final 仅因超时/OOM 翻红，标记 `FINAL_VERIFY_INFRA_FAILED`。
  两者都不自动计为接受，也不会伪装成确定的代码回归。
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

### 3.1 两层验收：checkpoint contract + target Guard

- **通用 contract**(`runtime/python/smell_core/checkpoint_contract.py`):
  统一处理 baseline、生产 diff、指标差值、build/test、失败原因与续跑条件。
- **目标 Guard**（11 种异味全覆盖）：调用方必须提供异味类别和目标位置/身份；
  Guard 只确认该目标是否具有该异味，不提供“扫描项目并发现所有异味”的 Detect
  能力。Long Method 等局部异味只解析目标文件；Feature Envy、Clone 等结构异味
  在 verify 时额外解析本次 diff 中的生产 Java 文件，以拒绝搬运或复制。
- **c000 Guard contract**（schema v5 / contract v5）：冻结 guard rule/profile hash、唯一
  target identity、baseline objectives、selector、项目 revision、目标源码 hash、测试修改策略、
  解析后的 build/test 命令与 verification 配置；controller 另持有外部
  baseline seal。会话身份、验证合同或 seal
  丢失时直接拒绝，不能退回模型参数或由 c000 自签名代替。
- dataset 与产品调用统一使用 `target_context_json` 传递需要的 selector 身份
  （如 symbol kind/name/container、receiver、参数组、父类）；字段经过同一白名单
  校验，只能选择目标，不能提供 score、threshold 或预期 verdict。CSV `evidence`
  仅保留审计展示，runner、checkpoint 和 PASS 判定均不会从中反向构造异味。
- verify 的基本解析边界只有 `冻结目标文件`；Git diff 的生产 Java 路径只是候选元数据，
  不会自动进入 AST。局部异味、God Class 和 Dead Code 始终只读目标；LPL 只追加包含
  冻结方法名的 changed successor 候选；Data Clumps 只追加精确三参数组的真实 occurrence；
  Refused Bequest 只追加目标的精确祖先合同链。Feature Envy 与 Clone 为防止把异味搬到
  本次重构代码中，会读取 diff 文件，但只对实际变更行相交的方法执行 anti-relocation/
  anti-copy。Guard 不 `rglob/os.walk` Java 源码、不建立全项目 finding catalog，也不执行
  所有异味规则。一次异味 Guard 最多解析 32 个生产 Java 文件、8 MiB 源码，超限返回
  `GUARD_SCOPE_TOO_LARGE`，不会退回全仓扫描。
- checkpoint 只保存 bounded witness，不保存全项目 AST、finding catalog、clone
  body token catalog 或调用图。stdout decision 小于 64 KiB，详细 Guard 结果写入
  `guard-evidence.json`（硬上限 2 MiB）；build/test 日志和 diff 分开保存，避免重复
  大对象造成 OOM。

### 3.2 PASS 与 IMPROVED（`resolution`）

- `PASS / resolved`：同版本 Target Guard 确认冻结目标异味消失，diff scope 内没有
  搬运/复制违规，并且存在生产源码改动且 build/test 均通过。这是唯一验收通过状态；dataset
  evidence 不参与异味或 PASS 判定。Guard 必须成功并唯一定位目标；不可用、结果无效或歧义都会 fail closed，resolution plan 也不会
  把这类快照标成 resolved。
- `IMPROVED / improved`：checkpoint 确认"真实生产 diff + 任一目标指标相对基线下降"
  （此时 build/test 也会强制执行）。`improved` 不终止 loop：插件按同一
  预算让 agent 继续冲 `resolved`，并把同一 Guard 计算的剩余目标指标、优先
  worklist 与精确 `next_action` 直接注入续跑提示；预算耗尽后仍有异味就保留 `IMPROVED`，
  不得转换为 `PASS`。

checkpoint 会保存每次指标、production patch 和 build/test 结果，但只有
`build_test_success=true` 的 checkpoint 才能成为可恢复的 `best` 或
`best_partial`。
合法候选先按 `resolved > improved` 排序，再比较指标改善；编译或测试失败的
checkpoint 只保留作诊断证据，`restorable=false`。

### 3.3 有效 PASS 的硬规则

- 必须有真实、非空、命中目标异味的生产源码 diff；无 diff PASS 恒为 0。
- 只改注释/格式/测试/生成文件不算（`EDIT_REQUIRED`)。
- selector 无法唯一定位 baseline 目标异味时直接拒绝；capture 后只跟踪冻结目标
  身份，并由同一 Guard rule/profile 复检。
- Feature Envy 以 `file/class/方法名+解析后参数类型` 冻结方法级目标（不把参数名
  或等价的类型限定写法当作身份）；dominant field/type 变化只更新指标，不能伪装成异味
  消失。verify 会在显式 diff scope 中复检每个改变/新增方法，拒绝跨方法、跨类和跨文件
  搬运；与目标和本次 diff 无关的源码不进入 Guard。Guard 不自行实现
  全项目 Java 调用图或重载分派来扩大目标身份。Data Clumps 只冻结精确组和有界
  occurrence scope，不保存全项目 occurrence catalog。
- build/test 回归按 `BUILD_FAILED` / `TEST_FAILED` / `SAMPLE_TEST_FAILED`
  如实归因，不会被 smell 判定吞掉。
- 最终接受是原子合同：bridge 返回码为 0，且 `status=PASS`、
  `resolution=resolved`、`success=true`、`accepted=true`、build/test 成功必须
  同时成立；旧字段缺失或彼此矛盾一律 fail closed。
- Java Guard 只读取目标/变更生产文件；标准 test source 会从 Guard/production diff
  排除，完整测试修改策略仍由 c000 test-change contract 冻结；build/test 描述符或本地验证脚本被修改时返回
  `VERIFICATION_CONFIG_MODIFIED`。
- Maven `build-helper:add-test-source` 的非测试命名目录被标为
  `auxiliary_test_compile`：工具/benchmark 源码仍是可重构产品输入；路径中明确
  带 test/spec 的行为测试源码仍归入冻结测试树。target admission、production
  diff 与测试冻结共用这一 source-role 解析，不再把所有 test-compile 输入一律
  当作不可改测试。自定义 source-role 的完整判定属于 test/build contract，不会触发
  Java 异味 AST 的全仓扫描。

### 3.4 loop 与预算

- 唯一时间预算：`--sample-deadline`（默认 1800s，范围 60–7200)，没有
  step 数上限。
- 续跑预算：`--loop-max`（默认 3，范围 0–5）、
  `--loop-no-progress-limit`（默认 2)、`--loop-mode=verify-failure`。
- 预算内 checkpoint 失败会把"基线/当前/差值/真实剩余总数/优先 worklist/唯一下一步动作"
  反馈回同一 session 继续修复；不再用只覆盖部分异味的特判提示。

---

## 4. 手动使用（不经 runner)

交互式 OpenCode 会话或 `opencode run` 中直接给完整任务输入；loop 策略
由 command 前缀显式声明（与批量 runner 同一入口）：

```text
/java-refactor-run --verification-mode=sample_optimized --loop-max=2 -- Project root: /abs/java-project; Smell type: long_method; Target location: src/main/java/Foo.java:42
```

command 展开后的用户消息会原样保留；plugin 不再重写 `output.parts`。解析出的
验证模式、目标身份、测试修改策略、backend 与 loop 上限通过独立且稳定的
controller system context 注入。失败类别、`failure_pack`、`next_action` 和
`loop.instruction` 只以最新一次 `smell_verify` 工具结果为准，自动续跑消息仅恢复
同一 session 并提示重新验证，不复制这些可变内容。

批量 runner 生成的用户任务同样只包含项目、语言、异味、目标位置和修复目标；
backend、验证模式与测试修改策略不在用户任务中重复展示。

批量运行会按 attempt 保存 `raw-user-input.txt*`、`controller-context.json*`、
`controller-system.txt*` 与 `message-manifest.json*`；`synthetic-events.jsonl`
单独记录 runner 注入的恢复消息。由此可以分别核对原始输入、稳定控制上下文和
合成消息 provenance。

Java 支持的 policy 参数：`--verification-mode=sample_optimized|project_full`、
`--allow-test-changes`（默认关闭并冻结到 c000；启用时必须使用
`project_full`）、
`--loop-mode=off|verify-failure`、`--loop-max=0..5`、
`--loop-no-progress-limit=1..5`、`--loop-on=smell,compile,test`、
`--sample-deadline=60..7200`。参数非法直接报 `INVALID_LOOP_POLICY`。

两种验证模式都会执行严格 build/test；`sample_optimized` 在 build 后只执行数据行
中已物化的聚焦测试命令。`project_full` 是固定的两段测试合同：先执行
`projects.yaml` 的项目级测试，再执行数据行的 `sample_test_command`，并要求每个
声明测试类都有新鲜、非零的 JUnit 执行证据。build、项目测试和样本测试三段任一
失败都不通过；两个测试命令及其工作目录均冻结进 c000，不能互相替代。
允许测试迁移时 runner 会无条件切到 `project_full`。
此时 c000 将测试策略冻结为 `api_migration`（默认是 `immutable`）：只允许测试
源码的 API 同步迁移，已有测试文件不得删除，测试方法/断言不得减少，不得新增
disabled/ignored/assumption-skip，测试资源和验证配置仍不可改；声明的测试类必须
在最终命令中产生新鲜、非零的执行证据。verify 会按冻结 manifest SHA 复用
未变化测试源码的强度审计，只重读 added/changed source；判定规则不因此放宽。
任何 PASS 和 IMPROVED 都必须通过。

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
  --agent java-refactor-agent
```

要点：

- 每个样本独立 git checkout、独立容器，requested commit/tree 与 actual
  必须一致，禁止 HEAD fallback。
- 测试默认以 `immutable` 冻结；只有 controller 显式传 `--allow-test-changes`
  才切到 `api_migration`。启用后统一执行 `project_full`；完整 test SHA 与测试强度
  审计、c000 冻结的 build/项目测试/样本测试合同仍必须通过，基线测试文件身份不得删除，
  测试方法/断言不得减少，不能新增跳过信号，声明测试必须实际执行；构建描述符、
  测试资源与验证脚本始终不可改。
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
.opencode/agents/            两个公开 agent(java-refactor-agent、smell-refactor-agent)
.opencode/commands/          java-refactor-run、smell-refactor-run(loop policy 入口)
.opencode/plugins/smell.ts   smell_verify 工具 + loop 状态机
.opencode/skills/            每种异味一个主 skill；按需加载 Java/Python/C/C++ reference，IDEA 机械路线独立保留
runtime/python/bridge/       smell_bridge(verify/capture-baseline 入口)
runtime/python/smell_core/   checkpoint contract、adapters、检测器、guards
scripts/                     runner、自检及维护入口（见 scripts/README.md）
delivery/                    交付镜像清单(tag / sha256 / 使用说明)
docker/                      mounted-source 与 delivery entrypoint
```

自检：`npm run check`、`npm run check:self`，以及 `scripts/self_check_*.py`
（契约、各 adapter、guard、runner 续跑、统一结构闭包等回归用例）。

异味 skill 采用“主流程 + 语言 reference”组织：10 个通用异味支持
Java/Python/C/C++，Java 特有的 `refused_bequest` 只暴露 Java reference。旧
`java-smell-edit-patterns` 内容作为兼容归档保留，新 Java reference 与其逐字节一致；
IDEA 后端仍按异味加载 `idea-refactor-cli` 的单个 YAML，不与语言语义规则混写。

约定：实验结果、worktree、`runs/`、`node_modules/`、`images/` 都不进 Git;
key 不进任何文件。
