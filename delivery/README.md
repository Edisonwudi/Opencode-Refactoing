# 交付镜像清单（四种语言）

环境镜像以压缩包形式交付，不进入 Git 仓库。当前交付批次：2026-07-20。

| 语言 | 原始镜像 tag | 压缩包 | 大小 | sha256 |
|---|---|---|---|---|
| Java | `opencode-java-refactor-env:0.1.0-mounted-source-godclass-bounded-3a10c8ad` | `smell-refactor-env-java.tar.gz` | 6.4 GB | `c025b5d8c5695db683b4d140b87a9501bbfb45198dfe745024f8a3e5b4200cd0` |
| Python | `opencode-smell-python-refactor-env:0.1.1-amd64-delivery-20260720` | `smell-refactor-env-python.tar.gz` | 3.5 GB | `438520490b61a222e25eacc0a960ce2da2ca3799947e1439fe10a23915c67ebf` |
| C | `opencode-smell-c-refactor-env:0.1.1-amd64-delivery-20260720` | `smell-refactor-env-c.tar.gz` | 0.98 GB | `7dfc05fe4188d2894c70b4dd3e64767b97987244820f01715d5e613e6fc109dc` |
| C++ | `opencode-smell-cpp-refactor-env:0.1.1-amd64-delivery-20260720` | `smell-refactor-env-cpp.tar.gz` | 1.8 GB | `e02f716b70954cbda3320d3c9f9dc640ea81fcd3808583a7b2f2224ed3649a99` |

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
