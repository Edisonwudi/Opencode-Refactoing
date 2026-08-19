# 语言与数据集合同

本文记录 Java、Python、C、C++ 数据集路径、支持范围、非 Java 目标身份约束和项目级
验证要求。运行命令见 [`advanced-usage.md`](advanced-usage.md)，通用验收语义见
[`verification-contract.md`](verification-contract.md)。

## 1. 支持范围

| 语言 | 通用异味 | 额外异味 | 默认 Agent |
|---|---:|---|---|
| Java | 10 | `refused_bequest` | `java-refactor-agent` |
| Python | 10 | 无 | `smell-refactor-agent` |
| C | 10 | 无 | `smell-refactor-agent` |
| C++ | 10 | 无 | `smell-refactor-agent` |

10 种通用异味为：`long_method`、`long_parameter_list`、
`nested_complexity`、`switch_statements`、`data_clumps`、
`code_clone_type1`、`god_class`、`dead_code`、`feature_envy`、
`mysterious_name`。

## 2. 权威数据集路径

- Java：`dataset/java/delivery_schema/<smell>.csv`；
- 非 Java：`dataset/nonjava/<lang>/<smell>_30.csv`；
- 非 Java Dead Code：`dataset/nonjava/<lang>/dead_code_curated.csv`。

容器运行时优先使用 `/agent-src/dataset/...` 中当前 Git checkout 的权威 CSV。
镜像内 `/opt/dataset/...` 只代表镜像验收时冻结的历史快照。

当前非 Java 交付镜像早于 `feature_envy` 和 `mysterious_name` 数据集，因此这两类必须
使用 `/agent-src/dataset/nonjava/...`。Dead Code 清洗后样本数为 Python 30、C 16、
C++ 10；旧镜像中的 `dead_code_30.csv` 不得用于正式运行。

`dataset/nonjava` 每种语言、每种异味只保留一份最终 CSV，不维护第二套固定子集。
子集实验由调用方从最终 CSV 显式选择 sample id。

## 3. 非 Java dataset-aligned 指标

| 指标 | Python | C | C++ |
|---|---:|---:|---:|
| Nested Complexity 最大深度 | 5 | 5 | 5 |
| Long Method 行数 | 50 | 60 | 60 |
| Long Parameter List 参数数 | 6 | 5 | 6 |
| Type-1 Clone 最小规范化 token | 17 | 18 | 25 |

这些值是冻结正例语料的兼容边界，不代表已完成负例误报率校准。

## 4. 非 Java 目标身份

非 Java selector/Guard 只使用 CSV 中明确冻结的目标，不扫描全项目寻找替代目标。

### Data Clumps

每行必须冻结完整关系 witness：

- `location` 用分号列出组内所有函数位置；
- `target_context_json.group` 保存规范化参数组；
- 至少 3 个位置且每个位置都有方法 selector。

运行时只解析显式位置。

### Feature Envy

CSV 必须在 `target_context_json.receiver_type` 冻结 canonical receiver root。接收者
字段缓存到局部别名后，别名读取仍折算为原接收者访问，直到被非别名值重赋。

c000 同时冻结目标文件、parser owner/name、完整参数 fingerprint 和声明起始行。
验证只在显式目标文件及其 production patch 中做一对一旧锚映射；仅允许同一 hunk、
相同 owner/name/参数身份且唯一候选的窄重锚。删除目标、跨 hunk、多候选或身份变化
都拒绝。

### Mysterious Name

CSV 必须显式冻结 `symbol_kind`、`symbol_name`、container、声明 slot 和声明行。
参数只能在同一声明 slot 一对一改名；局部变量只能在目标文件 patch 的同一 hunk
唯一改名。

确实需要同时迁移的条件编译声明或同名局部声明，必须完整列入
`target_context_json.declaration_lines`。运行时不会按同名自动扩张目标集合。
too-short/low-info 的新名称、旧引用残留、改变 container owner、跨 hunk 或多候选
都拒绝。

C/C++ 可以使用 baseline 已存在的 parser recovery，但目标函数仍必须有唯一、完整、
具名的声明边界；验证时新增 recovery 会被拒绝。

### Code Clone Type-1

局部编辑闭包只检查 production patch 中完整删除的 exact declaration，不遍历项目。
保留端点被模型改写时，只允许同文件、同一固定 hunk、相同 owner/name/完整签名且
唯一的一对一窄重锚。

### Dead Code

正例通过离线语料审计排除全项目引用、宏拼接、注册/回调、动态协议和公共 API 风险。
运行时 Guard 只判断显式目标声明是否仍存在，不重新扫描全项目引用。

## 5. 非 Java 容器运行

以 Python 为例，C/C++ 只需替换镜像和 CSV 路径：

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

非 Java `self-check` 必须显式指定本语言 dataset：

```bash
docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  opencode-smell-python-refactor-env:0.1.1-amd64-delivery-20260720 \
  self-check \
  --dataset-smoke-dataset /opt/dataset/smells/python/long_method_30.csv
```

## 6. 项目级验证入口

非 Java build/test 配置来自镜像内 `/opt/buildenv/projects.docker.yaml`，运行时仅由
`projects.runtime-overrides.yaml` 对已存在的精确项目 root 做窄覆盖。容器外直接运行
runner 时仍需显式传 `--projects <yaml>`。

正式 `project_full` 必须执行项目的原生行为测试，而不是版本输出、帮助输出、文件存在
检查或 collection-only。当前主要入口包括：

- Python：项目原生 pytest/unittest；
- C：Curl 原生 test suite、Redis 内建测试、libssh2 CTest、tmux regress、nginx
  官方行为套件、Git 上游 `t/` 脚本、RRDtool `make check`、libuv 原生测试；
- C++：DuckDB Catch2 `unittest`、RocksDB GTest、OpenTTD CTest、CMake CTest、aria2
  Automake/CppUnit、fmt/nlohmann-json CTest。

fixture、runner 或测试报告缺失都按失败处理，不退回弱 smoke。因固定离线环境明确缺少
外部能力而排除的测试必须以显式 skip 记录，剩余测试仍需非零执行。

C/C++ 重项目默认单 build job；可用 `SMELL_BUILD_JOBS` 在独占 CPU 环境显式提高。
跨样本只允许共享按镜像、语言和项目隔离的 ccache 对象，不能共享 build 目录、JUnit、
`verify.json` 或 PASS 结论。

## 7. 镜像与源码边界

环境镜像固定语言工具链、项目快照、离线依赖、Node/OpenCode 运行时和验收时 dataset。
Agent、Skill、plugin、checkpoint、runner 和当前权威 dataset 在运行时从
`/agent-src` 只读装配。

因此：

- Agent、Skill、plugin、checkpoint 或 runner 更新只需同步 Git；
- 工具链、项目快照、离线依赖或 lockfile 变化才需要重建镜像；
- 需要把新 dataset 固化进 `/opt/dataset` 时才随新镜像重新验收。

镜像 tag、归档 hash、离线依赖验收和重建规则见
[`../delivery/README.md`](../delivery/README.md)。
