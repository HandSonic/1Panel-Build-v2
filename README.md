# 1Panel v2 Builder (DIY Edition)

<p align="center">
  <a href="README_zh.md"><img src="https://img.shields.io/badge/Lang-中文-red" alt="中文"></a>
  <a href="https://github.com/1Panel-dev/1Panel"><img src="https://img.shields.io/badge/Upstream-1Panel-blue?logo=github" alt="Upstream"></a>
  <a href="https://hub.docker.com/"><img src="https://img.shields.io/badge/Docker-Enabled-2496ED?logo=docker" alt="Docker"></a>
  <img src="https://img.shields.io/badge/License-Apache%202.0-green" alt="License">
</p>

A **community-maintained, pure Docker-based build system** for [1Panel v2](https://github.com/1Panel-dev/1Panel).

This project democratizes the build process of 1Panel, allowing developers and advanced users to compile the full 1Panel stack (Core + Agent + Frontend) from source without needing a complex local development environment.

`e2e-build.yml` compiles the real upstream v2.2.5 frontend and all seven backend architectures, verifies exported packages, and retains Actions artifacts without publishing releases. The companion offline repository consumes these exact artifacts using `--custom_dist` for networkless Ubuntu amd64 installation and recovery checks. Production version discovery remains automatic; cross-compilation alone is not runtime validation of every architecture.

## 📖 Table of Contents

- [Why use this?](#-why-use-this)
- [Key Features](#--key-features)
- [Project Structure](#--project-structure)
- [Quick Start](#--quick-start)
- [Configuration](#️-configuration)
- [Output Artifacts](#-output-artifacts)
- [CI/CD Integration](#-cicd-integration)
- [License](#-license)

## ❓ Why use this?

The official 1Panel build process involves multiple languages (Go, Node.js) and tools (GoReleaser). This repository wraps all that complexity into a single Dockerfile.
Use this if you:
*   Want to **customize** 1Panel (modify source code, change assets).
*   Need to run 1Panel on **unsupported architectures** (e.g., specific RISC-V boards).
*   Want to **verify** the build process for security auditing.

## ✨ Key Features

- **🐳 Zero Local Dependencies**: No Go, Node.js, or complex toolchains required on your host machine.
- **🖥️ Multi-Architecture Ready**: Native cross-compilation for `amd64`, `arm64`, `armv7`, `ppc64le`, `s390x`, `loong64`, and `riscv64`.
- **🔄 Cross-Version Compatible**: Smart dependency handling ensures you can build both the latest `v2.x` and older versions.
- **📦 Standardized Output**: Produces standard installation tarballs with a machine-readable build manifest.

## 📂 Project Structure

```text
diyv2/
├── Dockerfile                  # Master build definition
├── scripts/
│   └── download_resources.sh   # Universal resource fetcher (keeps build robust)
└── README.md                   # Documentation
```

## 🚀 Quick Start

### Prerequisites
*   [Docker](https://docs.docker.com/get-docker/) installed and running.
*   [Git](https://git-scm.com/) (to clone this repo).

### Step-by-Step Build

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/HandSonic/1Panel-Build-v2.git
    cd 1Panel-Build-v2
    ```

2.  **Build the Builder Image**
    Replace `v2.0.13` with your desired version.
    ```bash
    docker build -f Dockerfile \
      --build-arg VERSION=v2.0.13 \
      -t 1panel-v2-builder .
    ```

3.  **Run Build & Export Artifacts**
    This command compiles the code inside a container and saves the results to `dist/`.
    ```bash
    docker run --rm -v "$(pwd)/dist:/dist" 1panel-v2-builder
    ```

4.  **Verify Output**
    Your packages are now ready in `dist/`:
    ```bash
    ls -lh dist/
    ```

## ⚙️ Configuration

Customize your build by passing `--build-arg` to the `docker build` command.

| Build Argument | Default | Description |
| :--- | :--- | :--- |
| **`VERSION`** | required | The Git tag or branch of 1Panel to build. |
| **`TARGET_ARCHES`** | *All Supported* | Space-separated target architectures (e.g., `"amd64 arm64"`). |
| **`INSTALLER_REF`** | `v2` | The branch/tag of the installer repository to use for scripts. |
| **`GO_VERSION`** | `auto in CI` | Golang version. CI resolves it from upstream `core/agent` `go.mod`; manual builds may still override it explicitly. |
| **`NODE_VERSION`** | `20` | Node.js version for frontend assets. |

> **Note on Architectures**: Default list is `amd64 arm64 armv7 ppc64le s390x loong64 riscv64`.

## 📦 Output Artifacts

The generator produces standard installation tarballs:

```text
dist/
├── 1panel-v2.0.13-linux-amd64.tar.gz  # The installation package
└── 1panel-v2.0.13-linux-amd64.tar.gz.sha256
```

**Inside the tarball:**
*   `/1panel-core`: Backend server binary.
*   `/1panel-agent`: Agent binary.
*   `/1pctl`: CLI management tool.
*   `/install.sh`: Standard install script.
*   And all necessary service files/language packs.

## 🤖 CI/CD Integration

This project is CI-ready. The included `.github/workflows/build.yml`:
1.  **Runs Daily**: Checks 1Panel official releases.
2.  **Auto-Builds**: Builds new versions, changed build inputs and incomplete architecture sets.
3.  **Releases**: Automatically creates a GitHub Release with the artifacts.

## Compatibility and recovery

Both CI providers call `scripts/ci_build.sh`. GitHub resolves the latest upstream version through the release API, then Git tags if the API is unavailable; it never silently builds an old hardcoded release. `INSTALLER_REF` defaults to `v2`. Resources are discovered from the installer repository, so added languages and init scripts do not require a local filename-list update. Valid cached resources and alternate download endpoints are tried before a required-resource failure. Optional init systems do not block packages for other systems.

Runtime configuration is found by its `base` section rather than an exact text block. Release tags such as `v2.2.5` use `stable`; beta/alpha/rc tags use `beta`; development branches use `dev`. Set `CHANNEL=stable|beta|dev` (Docker: `--build-arg CHANNEL=...`) to override. GitHub manual runs expose `channel` and `installer_ref`; repository variables `GO_VERSION`, `NODE_VERSION`, `CHANNEL` and `INSTALLER_REF` also override defaults without editing workflows. The stock online updater continues to use upstream update sources; this builder does not run a custom update server. Use the companion offline installer with its custom package source to retain custom builds when upgrading. GoReleaser uses the same resource and runtime-configuration helpers, but its own multi-platform build semantics still apply.

Architecture compilation failures are isolated. Usable packages are published with `build-manifest.json`, which lists `built`/`skipped` and `complete`; a partial release is labelled accordingly and retried on the next run. No usable package is a build failure. Required installer entry points, base language/service resources and GeoIP must be usable: packaging empty placeholders would produce a broken installer.

GitHub skips a version only when its build-input fingerprint and expected artifacts match. Local script/toolchain changes and resolved upstream/installer commits invalidate that fingerprint. New releases remain drafts until their built attachments and manifest are verified; interrupted uploads and missing architectures are retried. CNB uses the same build inputs, never treats a Git tag as proof of completion, and currently rebuilds each invocation to recover missing uploads without depending on provider-specific attachment APIs. Tags are never force-moved. For mutable branch builds outside CI, use `--no-cache` or pass a fresh `BUILD_FINGERPRINT` so Docker does not reuse an old source checkout.

Checksums contain only attachment filenames: download the `.tar.gz` and `.sha256` together and run `sha256sum -c 1panel-<version>-linux-<arch>.tar.gz.sha256` in that directory. Download failures retry; an upstream redesign that removes the runtime configuration or installer entry points requires an explicit error instead of silently claiming success. This reduces routine maintenance but cannot promise compatibility with every future upstream breaking change.

Offline regression checks (no Go build or network required):

```bash
python3 scripts/test_build.py
bash scripts/test_resources.sh
python3 scripts/test_release.py
```

## 📄 License

This project is open-sourced under the **Apache License 2.0**.
See the `LICENSE` file for more details.

---

<p align="center">Made with ❤️ by the Open Source Community</p>
