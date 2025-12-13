# 1Panel v2 Builder (DIY Edition)

<p align="center">
  <a href="README_zh.md"><img src="https://img.shields.io/badge/Lang-中文-red" alt="中文"></a>
  <a href="https://github.com/1Panel-dev/1Panel"><img src="https://img.shields.io/badge/Upstream-1Panel-blue?logo=github" alt="Upstream"></a>
  <a href="https://hub.docker.com/"><img src="https://img.shields.io/badge/Docker-Enabled-2496ED?logo=docker" alt="Docker"></a>
  <img src="https://img.shields.io/badge/License-Apache%202.0-green" alt="License">
</p>

A **community-maintained, pure Docker-based build system** for [1Panel v2](https://github.com/1Panel-dev/1Panel).

This project democratizes the build process of 1Panel, allowing developers and advanced users to compile the full 1Panel stack (Core + Agent + Frontend) from source without needing a complex local development environment.

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
- **📦 Standardized Output**: Produces artifacts identical in structure to official releases, ready for production use.

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
    git clone https://github.com/your-repo/1panel-diy.git
    cd 1panel-diy/diyv2
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
| **`VERSION`** | `v2.0.13` | The Git tag or branch of 1Panel to build. |
| **`TARGET_ARCHES`** | *All Supported* | Space-separated target architectures (e.g., `"amd64 arm64"`). |
| **`INSTALLER_REF`** | `v2` | The branch/tag of the installer repository to use for scripts. |
| **`GO_VERSION`** | `1.24` | Golang version (usually matches official requirement). |
| **`NODE_VERSION`** | `20` | Node.js version for frontend assets. |

> **Note on Architectures**: Default list is `amd64 arm64 armv7 ppc64le s390x loong64 riscv64`.

## 📦 Output Artifacts

The generator produces standard tarballs that look exactly like official releases:

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
2.  **Auto-Builds**: If a new official version is found that hasn't been built here, it triggers a build.
3.  **Releases**: Automatically creates a GitHub Release with the artifacts.

## 📄 License

This project is open-sourced under the **Apache License 2.0**.
See the `LICENSE` file for more details.

---

<p align="center">Made with ❤️ by the Open Source Community</p>
