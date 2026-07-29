# 交付镜像清单（四种语言）

环境镜像以压缩包形式交付，不进入 Git 仓库。当前交付批次：2026-07-20。

| 语言 | 原始镜像 tag | 压缩包 | 大小 | sha256 |
|---|---|---|---|---|
| Java | `opencode-java-refactor-env:0.1.0-mounted-source-godclass-bounded-3a10c8ad` | `smell-refactor-env-java.tar.gz` | 6.4 GB | `c025b5d8c5695db683b4d140b87a9501bbfb45198dfe745024f8a3e5b4200cd0` |
| Python | `opencode-smell-python-refactor-env:0.1.1-amd64-delivery-20260720` | `smell-refactor-env-python.tar.gz` | 3.5 GB | `438520490b61a222e25eacc0a960ce2da2ca3799947e1439fe10a23915c67ebf` |
| C | `opencode-smell-c-refactor-env:0.1.1-amd64-delivery-20260720` | `smell-refactor-env-c.tar.gz` | 0.98 GB | `7dfc05fe4188d2894c70b4dd3e64767b97987244820f01715d5e613e6fc109dc` |
| C++ | `opencode-smell-cpp-refactor-env:0.1.1-amd64-delivery-20260720` | `smell-refactor-env-cpp.tar.gz` | 1.8 GB | `e02f716b70954cbda3320d3c9f9dc640ea81fcd3808583a7b2f2224ed3649a99` |

## 当前 Java refused_bequest 候选镜像（2026-07-30）

Edison 本地候选镜像：

- tag：`opencode-java-refactor-delivery:0.1.3-r7.3.19-rb-capability-closure-v1-canal-oracle-fix-csv-aligned-v1`
- image ID：`sha256:80de97ec2b0dbafbc99dfe2d52e3aa047c9a867be82fa766145ead4a55e1ea51`
- dataset：最终版 `refused_bequest.csv`，30 条，sha256
  `1d86919e64a3be4a25a67ff8cf6191312a0e04c6424fd6df029e8759272ee7ae`

该镜像已用两个全新容器在 `--network none` 下串行验收。每轮均为 30 条样本、
4 个唯一 execution plan 全部通过；两轮合计 8/8 plan PASS，
`OFFLINE_DEPENDENCY_MISSING=0`、`DEPENDENCY_RESOLUTION_FAILED=0`，且两轮
样本到 execution plan 的语义映射一致。机器可读记录见
[`java-current.json`](java-current.json)。

旧候选 tag 中仍可能保留已经删除的 8-group refused-bequest CSV，不能用于新的
30 条实验。本镜像在覆盖数据前先删除整个 `delivery_schema` 目录；后续按当前
Dockerfile 重建时还会校验唯一 CSV 文件名和内容 sha256，避免 Docker `COPY`
保留旧文件而形成混合快照。

这是 Edison 上的已验收候选镜像，还不是上方 2026-07-20 的压缩包交付物。
本次验收只覆盖最终 30 条 refused-bequest；依赖审计脚本通过只读挂载运行，
按当前仓库重建后才会被烤入镜像。

## 使用

```bash
# 1. 将本目录的 4 个 tar.gz 与 SHA256SUMS 放入仓库根目录的 images/ 下
# 2. 校验完整性
(cd images && sha256sum -c SHA256SUMS)
# 3. 载入镜像(以 Java 为例)
docker load -i images/smell-refactor-env-java.tar.gz
```

完整的端到端操作顺序见仓库根目录 `README.md` 第 11 节「交付使用流程」:
将本仓库只读挂载为 `/agent-src`,先跑 `self-check`,再按 dataset 样本运行。

## 说明

- 镜像提供 IDE/语言项目/离线依赖缓存/dataset;agent 源码以本仓库为准,
  更新只需 `git pull`,无需重建镜像。
- 交付目录(实验服务器):`D:\smell-refactor-delivery-20260720\images\`,
  内含 4 个压缩包与同内容 `SHA256SUMS`。

## 增量更新:feature_envy 与 mysterious_name(2026-07-22)

本批次镜像内 dataset 为 8 种通用异味。此后仓库新增了对 python/c/cpp 的
feature_envy 与 mysterious_name 支持,**无需更新镜像**,只需:

1. `git pull`(新检测器、guard、反投机逻辑全部随源码生效);
2. 运行这两种异味时,`--dataset` 指向仓库内随附的 CSV(已是容器路径
   格式,经 `/agent-src` 挂载直接可用):

```bash
docker run --rm \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  -e SMELL_OPENCODE_API_KEY \
  opencode-smell-python-refactor-env:0.1.1-amd64-delivery-20260720 \
  --dataset /agent-src/dataset/nonjava/python/feature_envy_30.csv \
  --sample-id 1 \
  --model minimax/MiniMax-M2.7 \
  --opencode-api-key-env SMELL_OPENCODE_API_KEY \
  --opencode-base-url "$SMELL_OPENCODE_BASE_URL" \
  --verification-mode project_full
```

- c/cpp 换镜像名与 CSV 路径即可;mysterious_name 换 CSV 文件名。
- 原有 8 种异味不受影：`/opt/dataset/...`（镜像内）与
  `/agent-src/dataset/nonjava/...`（仓库内）内容一致,均可使用。
- Java 侧无变化（镜像 dataset 本已覆盖全部 11 种异味)。
- 下次镜像交付批次会把 10 种异味 dataset 烤回镜像,届时两种路径完全
  等价,该说明可移除。

## Java 离线 Maven 仓库验收

构建 Java 镜像时，必须在最终镜像层执行：

```bash
python3 /opt/opencode-refactor/scripts/normalize_maven_offline_repo.py \
  --repository /opt/buildenv/offline-home/.m2/repository \
  --repository-id local-all
```

该命令会把 Maven Resolver 的 `_remote.repositories` 来源统一为
`maven-offline-settings.xml` 实际使用的 `local-all`，并删除
`.lastUpdated` 残留。构建时还必须把同一份离线 settings 安装到
`/opt/buildenv/maven-global-settings.xml` 和
`/opt/buildenv/offline-home/.m2/settings.xml`：前者覆盖显式使用 `-gs`
的构建脚本，后者覆盖只设置 `HOME` 的样本测试命令。镜像入口会先确认三份
settings 完全一致，再以 `--check` 模式验证 resolver 元数据；发现
`central`、`central-https` 等外部仓库来源时直接拒绝启动，不能依赖某次
批处理运行临时修复共享缓存。

交付验收必须从未运行过的全新容器开始，使用 `--network none` 执行至少：

- MyBatis + JDK 17（覆盖 Derby 10.16.1.1）；
- MyBatis + JDK 21（覆盖 Derby 10.17.1.0）；
- 一个新的容器重复 JDK 17，证明结果不依赖前一容器写入的缓存。

批处理应以非 root 用户运行，并将 `/opt/buildenv` 作为只读挂载或使用
Docker `--read-only`（仅给 `/tmp`、`/runs` 和样本 worktree 提供独立可写
挂载）。每个样本必须使用独立容器或独立构建缓存，禁止跨样本共享可写的
Maven 仓库。

### 一键依赖闭包审计

元数据检查只能证明离线仓库格式正确，不能证明当前 dataset、固定项目提交
和实际 build/test 命令所需的依赖已经齐全。候选镜像发布前还必须在无网络
的新容器中运行：

```bash
docker run --rm --network none \
  --mount type=bind,src="$PWD/runs",dst=/runs \
  "$IMAGE" dependency-audit \
  --jobs 1 \
  --output /runs/image-dependency-audit
```

工具不调用模型。它复用 `self_check_java_baselines.py`，先按照项目提交、tree、
build/test 命令、JDK 和环境生成权威 execution plans 并去重，再执行每个唯一
plan。最终报告位于 `report.json`，其中离线缺包会单列为
`OFFLINE_DEPENDENCY_MISSING`；不能确定是否为缓存缺失的解析错误列为
`DEPENDENCY_RESOLUTION_FAILED`，不会与业务测试失败混为一类。

发布验收保持 `--jobs 1`，以免同一 Maven/Gradle 缓存上的锁和元数据竞争影响
判断。只有每个 plan 使用独立只读缓存或独立容器时，才提高 `--jobs`。
若 Maven settings 或 resolver 元数据本身异常，delivery entrypoint 会在工具
启动前直接拒绝运行；此时以控制台的 `maven-offline-repo` 错误为准，不会生成
本次依赖闭包的 `report.json`。

定点排查可以添加 `--smell refused_bequest`、`--project arc` 或
`--sample-id 7`。先使用 `--list-only` 可以只查看去重后的计划数量。完整验收
应在第二个全新容器中再运行一次，并保证 `/opt/buildenv` 不可写。
