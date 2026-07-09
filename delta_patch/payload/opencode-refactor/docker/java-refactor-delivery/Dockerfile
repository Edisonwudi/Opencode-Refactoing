ARG OPENCODE_IMAGE=opencode-smell-opencode:0.1.0-amd64
ARG IDEA_IMAGE=opencode-smell-idea:0.1.0-amd64
ARG JAVA_PROJECTS_IMAGE=opencode-smell-java-projects:0.1.0-amd64
ARG DATASET_IMAGE=opencode-java-refactor-delivery:0.1.0-amd64
ARG NODE_VERSION=22.22.2
ARG NODE_DIST_BASE=https://nodejs.org/dist

FROM ${OPENCODE_IMAGE} AS opencode
FROM ${IDEA_IMAGE} AS idea
FROM ${DATASET_IMAGE} AS dataset
FROM ${JAVA_PROJECTS_IMAGE}

ARG NODE_VERSION=22.22.2
ARG NODE_DIST_BASE=https://nodejs.org/dist

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/opt/buildenv/offline-home \
    MODEL_EGRESS_ONLY=1 \
    IDEA_SYSTEM=/tmp/idea-system \
    IDEA_CONFIG=/tmp/idea-config \
    IDEA_LOG=/tmp/idea-log \
    XDG_DATA_HOME=/tmp/idea-data \
    IDEA_REFACTOR_HOST=127.0.0.1 \
    IDEA_REFACTOR_JETBRAINS_CONFIG_DIRS=/tmp/idea-config \
    IDEA_REFACTOR_CLI=/opt/idea-refactoring/bin/idea-refactor \
    IDEA_REFACTOR_OPEN_COMMAND=/usr/local/bin/idea-refactor-open \
    SMELL_CONFIG=/opt/opencode-refactor/runtime/python/smell_core/defaults/refactor.yaml \
    SMELL_PROJECTS=/opt/opencode-refactor/runtime/python/smell_core/defaults/projects.java.docker.yaml \
    SMELL_ARTIFACT_ROOT=/runs/artifacts \
    PYTHONPATH=/opt/opencode-refactor/runtime/python \
    PATH=/opt/idea-refactoring/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/opencode-refactor

COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=opencode /usr/local/bin/rg /usr/local/bin/rg
COPY --from=opencode /usr/local/lib/python3.12/dist-packages /usr/local/lib/python3.12/dist-packages
COPY --from=idea /opt/idea /opt/idea
COPY --from=idea /opt/idea-refactoring /opt/idea-refactoring
COPY --from=idea /tmp/idea-config/plugins /tmp/idea-config/plugins
COPY --from=idea /usr/local/bin/idea-refactor-open /usr/local/bin/idea-refactor-open
COPY --from=dataset /opt/dataset/java /opt/dataset/java
COPY --from=dataset /opt/manifests /opt/manifests

COPY . /opt/opencode-refactor
COPY docker/java-refactor-delivery/entrypoint.sh /usr/local/bin/run-java-refactor-delivery

RUN NODE_VERSION="${NODE_VERSION}" NODE_DIST_BASE="${NODE_DIST_BASE}" python3 -c 'import os, tarfile, urllib.request; from pathlib import Path; version = os.environ["NODE_VERSION"].lstrip("v"); tag = f"v{version}"; base = os.environ["NODE_DIST_BASE"].rstrip("/"); archive = f"node-{tag}-linux-x64.tar.xz"; target = Path(f"/opt/node-{tag}-linux-x64"); url = f"{base}/{tag}/{archive}"; archive_path = Path("/tmp") / archive; target.exists() or (urllib.request.urlretrieve(url, archive_path), (lambda handle: (handle.extractall("/opt"), handle.close()))(tarfile.open(archive_path, "r:xz")), archive_path.unlink())' \
    && ln -sf "/opt/node-v${NODE_VERSION}-linux-x64/bin/node" /usr/local/bin/node \
    && ln -sf "/opt/node-v${NODE_VERSION}-linux-x64/bin/npm" /usr/local/bin/npm \
    && ln -sf "/opt/node-v${NODE_VERSION}-linux-x64/bin/npx" /usr/local/bin/npx \
    && find /opt/opencode-refactor -name '._*' -delete \
    && find /opt/opencode-refactor -name '.DS_Store' -delete \
    && find /opt/opencode-refactor -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/opencode-refactor -name '*.pyc' -delete \
    && cd /opt/opencode-refactor \
    && npm ci \
    && cd /opt/opencode-refactor/.opencode \
    && npm ci \
    && cd /opt/opencode-refactor \
    && chmod +x /usr/local/bin/opencode \
    && chmod +x /usr/local/bin/rg \
    && node --version | grep -E '^v22\.' \
    && node -e "const p=require('/opt/opencode-refactor/node_modules/@opencode-ai/plugin/package.json'); if (p.version !== '1.17.13') process.exit(1)" \
    && node -e "const p=require('/opt/opencode-refactor/.opencode/node_modules/@opencode-ai/plugin/package.json'); if (p.version !== '1.17.13') process.exit(1)" \
    && chmod +x /usr/local/bin/idea-refactor-open \
    && chmod +x /opt/idea-refactoring/bin/idea-refactor \
    && chmod +x /usr/local/bin/run-java-refactor-delivery \
    && chmod +x /opt/opencode-refactor/scripts/run_smell_dataset.py \
    && chmod +x /opt/opencode-refactor/scripts/self_check_smell_verify.mjs \
    && cp /opt/java-projects-config/projects.java.docker.yaml /opt/opencode-refactor/runtime/python/smell_core/defaults/projects.java.docker.yaml \
    && python3 -m py_compile /opt/opencode-refactor/runtime/python/bridge/smell_bridge.py /opt/opencode-refactor/scripts/run_smell_dataset.py \
    && node /opt/opencode-refactor/node_modules/typescript/bin/tsc --noEmit --skipLibCheck --module NodeNext --moduleResolution NodeNext --target ES2022 --types node /opt/opencode-refactor/.opencode/plugins/smell.ts \
    && python3 /opt/opencode-refactor/runtime/python/bridge/smell_bridge.py verify --help >/dev/null \
    && node /opt/opencode-refactor/scripts/self_check_smell_verify.mjs --require-dataset >/tmp/smell-verify-self-check.json \
    && find /opt/opencode-refactor -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/opencode-refactor -name '*.pyc' -delete \
    && opencode --version \
    && rg --version \
    && mkdir -p /runs/artifacts "$IDEA_SYSTEM" "$IDEA_CONFIG/plugins" "$IDEA_LOG" "$XDG_DATA_HOME" \
    && install -d -o smell -g smell \
        /opt/buildenv/offline-home/.local/share/opencode \
        /opt/buildenv/offline-home/.local/state/opencode \
        /opt/buildenv/offline-home/.config/opencode \
        /opt/buildenv/offline-home/.cache/opencode \
    && chown -R smell:smell /runs /tmp/idea-system /tmp/idea-config /tmp/idea-log /tmp/idea-data

VOLUME ["/runs"]
ENTRYPOINT ["/usr/local/bin/run-java-refactor-delivery"]
