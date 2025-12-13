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
- **📦 标准化产出**：生成的产物结构与官方发行版完全一致，可直接用于生产环境。

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
    git clone https://github.com/your-repo/1panel-diy.git
    cd 1panel-diy/diyv2
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
| **`VERSION`** | `v2.0.13` | 要构建的 1Panel Git 标签或分支。 |
| **`TARGET_ARCHES`** | *所有支持架构* | 空格分隔的目标架构列表 (例如: `"amd64 arm64"`)。 |
| **`INSTALLER_REF`** | `v2` | 用于脚本的 installer 仓库分支/标签。 |
| **`GO_VERSION`** | `1.24` | Golang 版本 (通常应匹配官方要求)。 |
| **`NODE_VERSION`** | `20` | 用于前端资源的 Node.js 版本。 |

> **架构说明**：默认列表为 `amd64 arm64 armv7 ppc64le s390x loong64 riscv64`。

## 📦 构建产物

生成器会产出与官方发行版完全一致的标准 tar.gz 包：

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
2.  **自动构建**：如果发现尚未构建的新官方版本，自动触发构建。
3.  **发布**：自动创建包含构件产物的 GitHub Release。

## 📄 许可证

本项目基于 **Apache License 2.0** 开源。
详情请参阅 `LICENSE` 文件。

---

<p align="center">Made with ❤️ by the Open Source Community</p>
