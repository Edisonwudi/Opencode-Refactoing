# 维护与审计脚本

本目录中的 `self_check_*` 由 `npm run check` 执行；下面三份脚本不属于日常
自检，而是数据集或交付物维护入口。它们保留生成与审计溯源，统一通过根目录
`package.json` 调用，避免依赖个人工作目录或隐含调用者。

## Java finding-contract 审计

归属：Java dataset 与产品 detector contract 维护。默认读取仓库内
`dataset/java/delivery_schema`，并生成
`docs/java-finding-contract-audit-current.{json,md}`。项目 checkout 根和产品项目
配置必须显式提供：

```bash
npm run audit:java-finding-contract -- \
  --projects-root /path/to/Java_Project \
  --projects-config /path/to/projects.java.local.yaml
```

`--projects-root` 下应按 dataset 的 `project_name` 放置已固定版本的 checkout；
`--projects-config` 必须描述同一批 checkout 及其只读 build symbol roots。

## 非 Java feature-envy / mysterious-name dataset 构建

归属：非 Java dataset 维护。脚本会扫描固定项目集合、复核 detector 命中并写入
`dataset/nonjava/<lang>/`。每个选中语言都必须显式声明本地 checkout 根：

```bash
npm run dataset:build-envy-name -- \
  --project-root c=/path/to/C_Project \
  --project-root cpp=/path/to/CPP_Project \
  --project-root python=/path/to/Python_Project
```

每个根目录直接包含脚本列出的项目 checkout。默认 cache 位于
`runs/build_envy_name_dataset/`；读取 cache 时，候选路径会绑定到本次命令声明的
根目录，不会复用生成机器的绝对路径。用 `--languages` 或 `--smells` 缩小范围，
用 `--no-write` 只做复核。

## Java 离线依赖静态完整性审计

归属：Java delivery image 维护。此脚本检查 Maven/Gradle cache、settings、
toolchains、JDK 和 wrapper 的静态完整性，不替代会真实执行 build/test plan 的
`dependency-audit`。默认路径对应交付镜像内 `/opt/buildenv` 与 `/opt/projects`：

```bash
npm run audit:offline-dependency-integrity -- \
  --report /runs/offline-dependency-integrity.json \
  --verify-archives
```

在宿主机执行时，应通过 `--maven-repository`、`--gradle-home`、
`--projects-root`、`--jdk-root`、`--maven-toolchains` 和重复的
`--maven-settings` 显式覆盖镜像路径。
