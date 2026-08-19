# 修复旧 Java 镜像的 Maven 离线仓库

## Bug 原因

旧 Java 镜像中的 Maven `_remote.repositories` 仍记录 `central` 等旧仓库 ID，
而新版源码将镜像内离线仓库统一暴露为 `local-all`。两者不一致时，入口检查会
拒绝启动：

```text
maven-offline-repo mode=check ... repository_id=local-all ...
invalid_entries=3636 changed_files=0
```

这不是缺少依赖。`invalid_entries` 表示仓库来源 ID 不匹配；`changed_files=0`
表示入口只检查，没有修改旧镜像。

## 修复流程

在已经载入旧镜像的机器上进入最新版源码根目录，派生一个本地修复 tag：

```bash
OLD_IMAGE='opencode-java-refactor-env:0.1.0-mounted-source-godclass-bounded-3a10c8ad'
REPAIRED_IMAGE="${OLD_IMAGE}-localall"

docker build -f - \
  --build-arg BASE_IMAGE="$OLD_IMAGE" \
  -t "$REPAIRED_IMAGE" \
  . <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY scripts/normalize_maven_offline_repo.py /tmp/normalize_maven_offline_repo.py

RUN python3 /tmp/normalize_maven_offline_repo.py \
      --repository /opt/buildenv/offline-home/.m2/repository \
      --repository-id local-all \
 && python3 /tmp/normalize_maven_offline_repo.py \
      --check \
      --repository /opt/buildenv/offline-home/.m2/repository \
      --repository-id local-all \
 && rm /tmp/normalize_maven_offline_repo.py
DOCKERFILE
```

验证修复结果：

```bash
docker run --rm \
  --entrypoint python3 \
  --mount type=bind,src="$PWD",dst=/agent-src,readonly \
  "$REPAIRED_IMAGE" \
  /agent-src/scripts/normalize_maven_offline_repo.py \
  --check \
  --repository /opt/buildenv/offline-home/.m2/repository \
  --repository-id local-all
```

成功输出应包含：

```text
invalid_entries=0 changed_files=0 last_updated_files=0
```

最后把原运行命令中的镜像名替换为：

```text
opencode-java-refactor-env:0.1.0-mounted-source-godclass-bounded-3a10c8ad-localall
```
