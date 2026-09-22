# WeChat & Media Tools (一站式多平台媒体下载与管理工具)

[![Release](https://img.shields.io/github/v/release/x554960766/wechat-mp-tools?style=flat-square)](https://github.com/x554960766/wechat-mp-tools/releases)
[![License](https://img.shields.io/github/license/x554960766/wechat-mp-tools?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-blue?style=flat-square)](#)

本地运行的多平台内容离线下载与归档工具，支持 **微信公众号、视频号、抖音、快手、小红书、哔哩哔哩** 资源批量下载、RSS 订阅及音视频转码。支持原生桌面窗口（`pywebview`）与浏览器 Web 模式。

---

## 🚀 快速下载与安装

> [!TIP]
> **开箱即用，无需配置本地 Python 环境！**
> 本项目已通过 GitHub Actions 自动构建双平台客户端，可直接前往 [👉 GitHub Releases 下载最新版 (v2.0.8)](https://github.com/x554960766/wechat-mp-tools/releases)。

### 客户端选择指南

| 系统平台 | 产物推荐 | 说明 |
| :--- | :--- | :--- |
| **macOS (Apple Silicon)** | `WeChat_MP_Tools_macOS_Lite.zip` | 适用于 M1/M2/M3/M4 芯片 Mac（原生 ARM64 极速运行） |
| **macOS (Intel)** | `WeChat_MP_Tools_macOS_Lite.zip` | 适用于 Intel 芯片 Mac |
| **Windows** | `WeChat_MP_Tools_Windows_Lite.zip` | 适用于 Win10 / Win11（内置自动检测与引导安装 WebView2） |

---

## 🌟 支持平台与核心特性

| 平台 | 核心功能 | 导出与高级能力 |
| :--- | :--- | :--- |
| **📱 微信公众号** | 单篇/历史批量采集、自动增量监控 | 原生 HTML / Markdown / 矢量 PDF 归档，标准 RSS 2.0 订阅源输出 |
| **🎥 微信视频号** | 单视频解析、博主主页采集、关注列表同步 | 无水印原画下载、断点续传、本地历史库智能映射 |
| **🎵 抖音** | 单视频/图集解析、博主主页/喜欢列表/收藏夹采集 | 评论批量抓取、无水印原视频直链下载、防风控会话自愈 |
| **⚡ 快手** | 单视频/图集解析、博主主页作品获取 | 流式分片下载与自动重试 |
| **📕 小红书** | 图文/视频笔记解析、Live 实况图、博主作品深度采集 | H.264 高清优选、文案离线保存、评论与画廊导出 |
| **📺 哔哩哔哩** | 单视频/多P分P解析、UP主空间投稿采集 | 高画质音视频自动合并为 MP4，弹幕转 ASS，CC 字幕导出 |
| **🎬 媒体转码** | MP4 / MKV / MOV / WebM / AVI / FLV 等多格式互转 | 支持 H.264/H.265 硬件加速、批量体积压缩、一键提取 MP3 |

### 微信自动化亮点
- **静默后台凭证捕获**：内置轻量级 MITM 代理与独立子进程拦截，毫秒级获取 `key`、`pass_ticket`、`appmsg_token`。
- **跨平台无感刷新**：
  - **Windows**：基于底层 Win32 API / UIA 异步消息机制，**完全不抢鼠标、不夺取焦点**，兼容微信 3.x/4.x。
  - **macOS**：原生 AppleScript + Vision OCR 字符定位，全自动完成窗口流转与凭证续期。

---

## 📖 微信公众号使用指引

1. **凭证初始化**：进入「设置」页面，点击 **安装证书** 并开启 **本地抓包代理**，保持电脑端微信登录。
   - 方式 A：点击「公众号」或「账号设置」中的 **刷新凭证**，程序自动完成后台静默续期。
   - 方式 B：在微信中随意点开任意一篇公众号文章，代理将自动完成截获。
2. **添加公众号**：复制任意公众号文章链接，在软件内粘贴并点击 **添加公众号** 即可建档。
3. **下载与订阅**：
   - 支持勾选 **HTML / Markdown / PDF** 格式一键下载单篇或历史批量文章。
   - 在详情页点击 **RSS** 即可复制标准订阅源，无缝接入 NetNewsWire、Follow、Inoreader 等主流阅读器。

---

## 💻 本地开发与源码运行

如需自行编译或进行二次开发：

```bash
# 1. 克隆代码
git clone https://github.com/x554960766/wechat-mp-tools.git
cd wechat-mp-tools

# 2. 安装运行依赖
pip install -r requirements.txt
playwright install chromium

# 3. 启动应用
python3 main.py   # 桌面原生窗口模式（基于 pywebview）
# 或
python3 app.py    # 网页服务模式（访问 http://localhost:5200）
```

---

## 📁 数据存储目录

所有登录状态、Cookie 缓存、收藏及下载资源均保存在用户独立数据目录：
- **macOS**：`~/Library/Application Support/WeChat MP Tools/data/`
- **Windows**：程序所在目录下的 `data/`

---

## ⚠️ 免责声明

1. 本项目仅用于个人学习研究、技术交流与本地数据容灾备份，严禁用于任何商业牟利或侵权行为。
2. 使用本工具时请严格遵守各平台用户协议与相关法律法规，因使用不当造成的风险由使用者自行承担。
3. 本项目为开源软件，按“现状”提供，不承担任何形式的担保与连带责任。
