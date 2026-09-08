ARG GO_VERSION=1.25.7
ARG NODE_VERSION=20
ARG VERSION=""
ARG INSTALLER_REF=v2
ARG CHANNEL=""
ARG BUILD_FINGERPRINT=""
ARG TARGET_ARCHES="amd64 arm64 armv7 ppc64le s390x loong64 riscv64"

FROM node:${NODE_VERSION}-bookworm AS frontend-builder
ARG VERSION
ARG BUILD_FINGERPRINT
ENV BUILD_FINGERPRINT=${BUILD_FINGERPRINT}
ENV VERSION=${VERSION}
ENV NODE_OPTIONS=--max-old-space-size=8192

RUN set -ex \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN test -n "${VERSION}" || { echo "Pass --build-arg VERSION=<upstream tag or branch>" >&2; exit 1; }; \
    git clone -b "${VERSION}" --depth=1 https://github.com/1Panel-dev/1Panel /src

WORKDIR /src/frontend

COPY scripts/patch_frontend_xpack_compat.mjs /tmp/patch_frontend_xpack_compat.mjs
COPY scripts/patch_backend_xpack_compat.mjs /tmp/patch_backend_xpack_compat.mjs

RUN set -ex \
    && node /tmp/patch_backend_xpack_compat.mjs /src \
    && node /tmp/patch_frontend_xpack_compat.mjs /src/frontend \
    && npm install \
    && BUILD_SCRIPT="$(node -e 'const s=require("./package.json").scripts||{}; const name=["build:pro","build"].find(x=>s[x]); if(!name) { console.error("No frontend build script found"); process.exit(1); } process.stdout.write(name)')" \
    && npm run "$BUILD_SCRIPT" \
    && rm -rf node_modules ~/.npm

FROM golang:${GO_VERSION} AS builder
ARG VERSION
ARG INSTALLER_REF
ARG TARGET_ARCHES
ARG CHANNEL
ARG BUILD_FINGERPRINT
ENV CHANNEL=${CHANNEL}
ENV BUILD_FINGERPRINT=${BUILD_FINGERPRINT}
ENV VERSION=${VERSION}
ENV INSTALLER_REF=${INSTALLER_REF}
ENV TARGET_ARCHES=${TARGET_ARCHES}
ENV GOTOOLCHAIN=auto

WORKDIR /opt/1Panel

COPY --from=frontend-builder /src /opt/1Panel

RUN set -ex \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git curl python3 \
    && rm -rf /var/lib/apt/lists/*

# Keep packaging outside Docker syntax so CI and local builds exercise the same code.
COPY scripts/download_resources.sh scripts/configure_runtime.py scripts/build_packages.sh /tmp/build-scripts/

RUN set -ex \
    && bash /tmp/build-scripts/download_resources.sh \
    && python3 /tmp/build-scripts/configure_runtime.py /opt/1Panel "${VERSION}" --channel "${CHANNEL}" \
    && bash /tmp/build-scripts/build_packages.sh \
    && rm -rf /opt/1Panel/build

FROM debian:bookworm-slim

WORKDIR /opt/1Panel

COPY --from=builder /opt/1Panel/dist /opt/1Panel/dist

VOLUME /dist

CMD ["/bin/sh", "-c", "cp -rf dist/* /dist/"]
