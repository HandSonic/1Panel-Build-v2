# 1Panel v2 构建器 (DIY 版)

<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/Lang-English-blue" alt="English"></a>
  <a href="https://github.com/1Panel-dev/1Panel"><img src="https://img.shields.io/badge/Upstream-1Panel-blue?logo=github" alt="Upstream"></a>
  <a href="https://hub.docker.com/"><img src="https://img.shields.io/badge/Docker-Enabled-2496ED?logo=docker" alt="Docker"></a>
  <img src="https://img.shields.io/badge/License-Apache%202.0-green" alt="License">
</p>

这是一个**由社区维护、纯 Docker 驱动**的 [1Panel v2](https://github.com/1Panel-dev/1Panel) 构建系统。

本项目旨在降低 1Panel 的构建门槛，允许开发者和高级用户在无需配置复杂本地开发环境（如 Go、Node.js）的情况下，从源码编译完整的 1Panel 技术栈（核心 + Agent + 前端）。

## 📖 目录

- [为什么使用此工具？](#-为什么使用此工具)
- [核心特性](#-核心特性)
- [项目结构](#-项目结构)
- [快速开始](#-快速开始)
- [配置说明](#-配置说明)
- [构建产物](#-构建产物)
- [CI/CD 集成](#-cicd-集成)
- [许可证](#-许可证)

## ❓ 为什么使用此工具？

官方的 1Panel 构建流程涉及多种语言（Go, Node.js）和工具（GoReleaser）。本仓库将所有复杂性封装在一个 Dockerfile 中。
适用场景：
*   想要**定制** 1Panel（修改源码、更换素材）。
*   需要在**非官方支持架构**（如特定的 RISC-V 开发板）上运行 1Panel。
*   出于安全审计目的，需要**验证**构建过程。

## ✨ 核心特性

- **🐳 零本地依赖**：宿主机仅需 Docker 和 Git，无需 Go 或 Node.js 环境。
- **🖥️ 多架构就绪**：原生支持 `amd64`, `arm64`, `armv7`, `ppc64le`, `s390x`, `loong64`, 和 `riscv64` 交叉编译。
- **🔄 跨版本兼容**：智能依赖处理，确保既能构建最新的 `v2.x`，也能兼容旧版本。
- **📦 标准化产出**：生成标准安装压缩包，并附带可机器读取的构建清单。

## 📂 项目结构

```text
diyv2/
├── Dockerfile                  # 主构建定义文件
├── scripts/
│   └── download_resources.sh   # 通用资源下载脚本（保障构建稳健性）
└── README.md                   # 文档
```

## 🚀 快速开始

### 前置要求
*   已安装并运行 [Docker](https://docs.docker.com/get-docker/)。
*   安装 [Git](https://git-scm.com/)（用于克隆本仓库）。

### 逐步构建指南

1.  **克隆仓库**
    ```bash
    git clone https://github.com/HandSonic/1Panel-Build-v2.git
    cd 1Panel-Build-v2
    ```

2.  **构建镜像**
    将 `v2.0.13` 替换为您想要构建的版本。
    ```bash
    docker build -f Dockerfile \
      --build-arg VERSION=v2.0.13 \
      -t 1panel-v2-builder .
    ```

3.  **运行构建并导出产物**
    此命令会在容器内编译代码并将结果保存到 `dist/` 目录。
    ```bash
    docker run --rm -v "$(pwd)/dist:/dist" 1panel-v2-builder
    ```

4.  **验证输出**
    您的安装包现在已准备就绪，位于 `dist/` 目录：
    ```bash
    ls -lh dist/
    ```

## ⚙️ 配置说明

您可以通过传递 `--build-arg` 参数给 `docker build` 命令来定制构建过程。

| 构建参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| **`VERSION`** | 必填 | 要构建的 1Panel Git 标签或分支。 |
| **`TARGET_ARCHES`** | *所有支持架构* | 空格分隔的目标架构列表 (例如: `"amd64 arm64"`)。 |
| **`INSTALLER_REF`** | `v2` | 用于脚本的 installer 仓库分支/标签。 |
| **`GO_VERSION`** | `CI 自动解析` | Golang 版本。CI 会从上游 `core/agent` 的 `go.mod` 中解析；手动构建仍可显式覆盖。 |
| **`NODE_VERSION`** | `20` | 用于前端资源的 Node.js 版本。 |

> **架构说明**：默认列表为 `amd64 arm64 armv7 ppc64le s390x loong64 riscv64`。

## 📦 构建产物

生成器会产出标准安装 tar.gz 包：

```text
dist/
├── 1panel-v2.0.13-linux-amd64.tar.gz  # 安装包
└── 1panel-v2.0.13-linux-amd64.tar.gz.sha256
```

**压缩包内容：**
*   `/1panel-core`: 后端服务二进制。
*   `/1panel-agent`: Agent 代理二进制。
*   `/1pctl`: CLI 管理工具。
*   `/install.sh`: 标准安装脚本。
*   以及所有必要的服务文件和语言包。

## 🤖 CI/CD 集成

本项目已就绪 CI。包含的 `.github/workflows/build.yml`：
1.  **每日运行**：检查 1Panel 官方发布。
2.  **自动构建**：发现新版本、构建输入变化或架构产物不齐时，自动构建或补齐。
3.  **发布**：自动创建包含构件产物的 GitHub Release。

## 自适应与失败恢复

GitHub Actions 和 CNB 共用 `scripts/ci_build.sh`。最新版本先查询上游 Release API，失败后查询 Git 标签，不会静默退回某个写死的旧版本。`INSTALLER_REF` 默认 `v2`。资源从 installer 仓库动态发现，新增语言、初始化脚本无需维护本地文件名清单；先复用有效缓存，再尝试备用下载入口。可选初始化系统文件缺失不会拖垮其他系统的安装包。

后端配置按 `base` 配置节发现，不依赖完整文本块。`v2.2.5` 等稳定标签使用 `stable`，beta/alpha/rc 预发布标签使用 `beta`，开发分支使用 `dev`。可通过 `CHANNEL=stable|beta|dev`（Docker：`--build-arg CHANNEL=...`）覆盖。GitHub 手动运行提供 `channel`、`installer_ref` 输入，也可用仓库变量 `GO_VERSION`、`NODE_VERSION`、`CHANNEL`、`INSTALLER_REF` 调整默认值，无需修改工作流。内置在线更新仍使用上游更新源，本仓库不提供自定义更新服务器；要在升级后保留自编译产物，请使用配套离线安装仓库的 custom 包源。GoReleaser 共用资源和运行配置处理脚本，但仍遵循其自身的多架构构建行为。

架构编译失败相互隔离。可用的包可以发布，`build-manifest.json` 明确记录各架构的 `built`/`skipped` 与整体 `complete`，部分发布会注明并在下次重试；所有架构都失败才终止。安装入口、基础语言与服务资源、GeoIP 等真正必要的内容必须有效，避免把空文件打成“成功”的安装包。

GitHub 只有在构建输入指纹一致、预期附件齐全时才跳过。本地脚本、工具链和解析到的上游/installer 提交变化会更新指纹。新 Release 先保存草稿，上传并核对已构建附件及清单后才发布；中断上传或缺少架构会再次重试。CNB 使用相同构建输入，不再用 Git 标签判断构建完成；目前每次触发都重建，以恢复缺失附件，不依赖不确定的 CNB 附件查询接口。已有标签不会强制移动。CI 外手动构建可变分支时，请使用 `--no-cache` 或传入新的 `BUILD_FINGERPRINT`，避免 Docker 复用旧源码缓存。

校验文件仅包含附件文件名；把 `.tar.gz` 和 `.sha256` 下载到同一目录后执行 `sha256sum -c 1panel-<版本>-linux-<架构>.tar.gz.sha256` 即可。网络波动会重试，但上游彻底移除配置或安装入口时仍会明确报错。此策略减少常规改动带来的维护，不能保证自动兼容所有未来的上游破坏性变更。

隔离回归检查（无需实际 Go 编译或访问网络）：

```bash
python3 scripts/test_build.py
bash scripts/test_resources.sh
python3 scripts/test_release.py
```

## 📄 许可证

本项目基于 **Apache License 2.0** 开源。
详情请参阅 `LICENSE` 文件。

---

<p align="center">Made with ❤️ by the Open Source Community</p>

