ARG GO_VERSION=1.24
ARG NODE_VERSION=20
ARG VERSION=v2.0.13
ARG INSTALLER_REF=""
ARG TARGET_ARCHES="amd64 arm64 armv7 ppc64le s390x loong64 riscv64"

FROM node:${NODE_VERSION}-bookworm AS frontend-builder
ARG VERSION
ENV VERSION=${VERSION}
ENV NODE_OPTIONS=--max-old-space-size=8192

RUN set -ex \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone -b ${VERSION} --depth=1 https://github.com/1Panel-dev/1Panel /src

WORKDIR /src/frontend

COPY scripts/patch_frontend_xpack_compat.mjs /tmp/patch_frontend_xpack_compat.mjs
COPY scripts/patch_backend_xpack_compat.mjs /tmp/patch_backend_xpack_compat.mjs

RUN set -ex \
    && node /tmp/patch_backend_xpack_compat.mjs /src \
    && node /tmp/patch_frontend_xpack_compat.mjs /src/frontend \
    && npm install \
    && npm run build:pro \
    && rm -rf node_modules ~/.npm

FROM golang:${GO_VERSION}-bookworm AS builder
ARG VERSION
ARG INSTALLER_REF
ARG TARGET_ARCHES
ENV VERSION=${VERSION}
ENV INSTALLER_REF=${INSTALLER_REF}
ENV TARGET_ARCHES=${TARGET_ARCHES}

WORKDIR /opt/1Panel

COPY --from=frontend-builder /src /opt/1Panel

RUN set -ex \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git wget \
    && rm -rf /var/lib/apt/lists/*

# Use custom download script instead of ci/script.sh for cross-version compatibility
COPY scripts/download_resources.sh /tmp/download_resources.sh

RUN set -ex \
    && chmod +x /tmp/download_resources.sh \
    && INSTALLER_REF="${INSTALLER_REF:-v2}" /tmp/download_resources.sh \
    && sed -i "s@^ORIGINAL_VERSION=.*@ORIGINAL_VERSION=${VERSION}@g" /opt/1Panel/1pctl

RUN set -ex \
    && mkdir -p build dist \
    # Verify required files exist before building
    && for f in 1pctl install.sh GeoIP.mmdb; do \
        if [ ! -f "/opt/1Panel/${f}" ]; then echo "ERROR: Missing ${f}"; exit 1; fi; \
    done \
    && if [ ! -d "/opt/1Panel/initscript" ]; then echo "ERROR: Missing initscript/"; exit 1; fi \
    && if [ ! -d "/opt/1Panel/lang" ]; then echo "ERROR: Missing lang/"; exit 1; fi \
    # Copy service files from initscript if root-level ones missing
    && if [ ! -f "/opt/1Panel/1panel-core.service" ] && [ -f "/opt/1Panel/initscript/1panel-core.service" ]; then \
        cp /opt/1Panel/initscript/1panel-core.service /opt/1Panel/1panel-core.service; \
    fi \
    && if [ ! -f "/opt/1Panel/1panel-agent.service" ] && [ -f "/opt/1Panel/initscript/1panel-agent.service" ]; then \
        cp /opt/1Panel/initscript/1panel-agent.service /opt/1Panel/1panel-agent.service; \
    fi \
    && for ARCH in ${TARGET_ARCHES}; do \
        echo "==> building ${ARCH}"; \
        GOARCH=${ARCH}; GOARM=""; APP_ARCH=${ARCH}; \
        if [ "${ARCH}" = "armv7" ]; then GOARCH=arm; GOARM=7; APP_ARCH=armv7; fi; \
        cd /opt/1Panel/core; \
        CGO_ENABLED=0 GOOS=linux GOARCH=${GOARCH} GOARM=${GOARM} go build -trimpath -ldflags '-s -w' -o ../build/1panel-core ./cmd/server/main.go || exit 1; \
        cd /opt/1Panel/agent; \
        CGO_ENABLED=0 GOOS=linux GOARCH=${GOARCH} GOARM=${GOARM} go build -trimpath -ldflags '-s -w' -o ../build/1panel-agent ./cmd/server/main.go || exit 1; \
        PACKAGE_NAME="1panel-${VERSION}-linux-${APP_ARCH}"; \
        mkdir -p "/opt/1Panel/${PACKAGE_NAME}"; \
        cp /opt/1Panel/build/1panel-core /opt/1Panel/build/1panel-agent "/opt/1Panel/${PACKAGE_NAME}/" || exit 1; \
        cp /opt/1Panel/1pctl /opt/1Panel/install.sh "/opt/1Panel/${PACKAGE_NAME}/" || exit 1; \
        if [ -f /opt/1Panel/1panel-core.service ]; then cp /opt/1Panel/1panel-core.service "/opt/1Panel/${PACKAGE_NAME}/"; fi; \
        if [ -f /opt/1Panel/1panel-agent.service ]; then cp /opt/1Panel/1panel-agent.service "/opt/1Panel/${PACKAGE_NAME}/"; fi; \
        if [ -f /opt/1Panel/GeoIP.mmdb ]; then cp /opt/1Panel/GeoIP.mmdb "/opt/1Panel/${PACKAGE_NAME}/"; fi; \
        if [ -f /opt/1Panel/LICENSE ]; then cp /opt/1Panel/LICENSE "/opt/1Panel/${PACKAGE_NAME}/"; fi; \
        if [ -f /opt/1Panel/README.md ]; then cp /opt/1Panel/README.md "/opt/1Panel/${PACKAGE_NAME}/"; fi; \
        cp -r /opt/1Panel/initscript /opt/1Panel/lang "/opt/1Panel/${PACKAGE_NAME}/" || exit 1; \
        tar -czf "/opt/1Panel/${PACKAGE_NAME}.tar.gz" -C /opt/1Panel "${PACKAGE_NAME}" || exit 1; \
        sha256sum "/opt/1Panel/${PACKAGE_NAME}.tar.gz" > "/opt/1Panel/dist/${PACKAGE_NAME}.tar.gz.sha256" || exit 1; \
        mv "/opt/1Panel/${PACKAGE_NAME}.tar.gz" /opt/1Panel/dist/ || exit 1; \
        rm -rf "/opt/1Panel/${PACKAGE_NAME}"; \
    done \
    && rm -rf build

FROM debian:bookworm-slim

WORKDIR /opt/1Panel

COPY --from=builder /opt/1Panel/dist /opt/1Panel/dist

VOLUME /dist

CMD ["/bin/sh", "-c", "cp -rf dist/* /dist/"]
