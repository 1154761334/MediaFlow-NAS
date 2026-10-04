# MediaFlow-NAS

> **专为家庭 NAS 设计的全自动影视流水线**  
> 以迅雷 NAS 为下载引擎，具备自适应动态水位调度、画质升级智能门禁、冷热存储分层流转与 Emby 零负载同步。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Platform: Synology / Linux](https://img.shields.io/badge/Platform-Synology%20%7C%20Linux-orange.svg)]()

---

## 为什么写这个项目？

很多搭建家庭 NAS 影音库的朋友都有类似的体验：
* 欧美影视剧有成熟的 `Sonarr` / `Radarr` / `qBittorrent` 全家桶，但到了东亚番号影视（特异性番号前缀、多版本流出、无码版、中文字幕升级版），主流工具几乎完全失效。
* 下载冷门、死种资源时，真正能拉得动速度的往往只有**迅雷 NAS 版**（依赖离线服务器与 P2SP 会员加速）。然而迅雷官方客户端**完全缺乏自动化调度生态**：
  1. **任务容易堵死**：一口气塞进几百上千个磁力链接，客户端直接卡死崩溃；
  2. **死种霸占通道**：大量 0 速度的任务永久霸占宝贵的下载槽位，后面的新种子排队排到地老天荒；
  3. **SSD 容易撑爆**：高速 SSD 下载缓冲盘动不动就报警爆满，导致系统服务假死；
  4. **重复与劣质下载**：媒体库里明明已有 1080P HEVC，又下载了 6GB 的 H.264 重复版本；或者来了真正的 4K/无码流出版本，却无法自动识别升级；
  5. **人工搬运繁琐**：下载完后还要手动翻找正片、删广告样片、复制到机械盘、手动触发刮削；
  6. **Emby 扫库风暴**：刮削完通知 Emby，常常触发全库深度扫描，导致装载近 2 万部影片的机械盘疯狂寻道、I/O 假死。

**MediaFlow-NAS 就是为了彻底终结这些折磨而生的。**  
它将“磁力准入 ➔ 迅雷调度 ➔ 分层清洗 ➔ AI 刮削 ➔ 媒体库同步”串联成一条**7×24 小时无人值守闭环流水线**。

---

## 整体架构与数据流

```text
               [ 磁力任务清单 (TXT / 剪贴板) ]
                              │
                              ▼
        ┌───────────────────────────────────────────┐
        │       Pre-download Media Gate (质量门禁)    │
        │  • 40位 BTIH 哈希查重                     │
        │  • 1.8W+ 媒体资产毫秒级对账 (0 机械盘寻道) │
        │  • HEVC 同档保护法则 (体积大 ≠ 画质好)     │
        │  • 4K / 无码流出 / 中文字幕 升维放行      │
        └───────────────────────────────────────────┘
                              │
                 NEW / UPGRADE 写入 queue.db
                              ▼
        ┌───────────────────────────────────────────┐
        │    Dynamic Watermark Scheduler (调度引擎) │
        │  • 动态维持 10~15 个高活跃下载槽位        │
        │  • 0 速度死种 3 小时让位退避 (6h/24h/72h) │
        │  • 新任务 Tier-Fresh 优先准入             │
        │  • SSD 缓冲保护熔断 (低于 120G 停 / 180G 复)│
        └───────────────────────────────────────────┘
                              │
                    提交到本地 迅雷 NAS 引擎
                              ▼
            Tier 1: SSD 高速下载缓冲盘 (/volume1/迅雷/下载)
                              │
                              ├─▶ clean_ghost_dirs (8 重门槛安全修剪空目录)
                              ▼
                 完成文件自动检测与样本清洗
                 • 自动剔除 <150MB 垃圾样片与广告
                 • 过滤 .xltd / .tmp / .cfg 临时分块
                              ▼
            Tier 2: HDD 机械盘暂存队列 (/volume2/.../#重刮队列)
                              │
                              ▼ (攒够 3 部或超时 30 分钟)
        ┌───────────────────────────────────────────┐
        │          AI Metadata & Scraper 刮削生态   │
        │  • 自动唤醒 JavSP 刮削容器                │
        │  • 智能翻译双路由: 云端 Gemini + 本地 Hy-MT2│
        │  • 0% 敏感词内容审查拦截                  │
        └───────────────────────────────────────────┘
                              │
                              ▼ 刮削完成移动到归档库
            Tier 2: HDD 正式归档影视库 (/volume2/.../#整理完成)
                              │
                              ▼ (POST /emby/Library/Media/Updated)
        ┌───────────────────────────────────────────┐
        │             Emby 零负载单片叶子更新       │
        │  • 仅推送精准叶子路径 (单片耗时 0.009s)   │
        │  • 彻底告别大库全盘扫描与机械盘 I/O 假死  │
        └───────────────────────────────────────────┘
```

---

## 核心特性

### 1. 🎯 下载前智能质量门禁 (Media Gate)
* **规范化 40 位 BTIH 查重**：自动支持 Hex 与 RFC 4648 Base32 编码互转，杜绝同一任务变着花样重复提交。
* **本地资产毫秒级对账**：利用可快速重建的 SQLite 资产库比对本地 18,000+ 部存量视频，**日常调度绝不盲扫机械盘目录**。
* **独创 HEVC 保护法则**：**Codec 属于存储效率属性，而不是画质等级**。本地已收录 2.5GB 1080P HEVC，绝不因为新磁力是 6GB 1080P H.264 就误判“升级”重新下载，杜绝宝贵带宽与存储浪费。
* **精准升维放行**：识别真正的提升版本——分辨率升级（1080P ➔ 4K/6K）、内容升级（普通有码 ➔ 无码流出 UNC）、字幕升级（无字幕 ➔ 中文字幕 -C）。
* **五分流模型与 Dry-Run**：
  * `HASH_DUP`：队列已存在，直接过滤；
  * `SKIP`：本地已收录同档次或更好版本，拦截下载；
  * `REVIEW`：信息不足（如仅标注“高清”），默认暂缓不下载；
  * `NEW`：纯新片，放行；
  * `UPGRADE`：高价值版本，放行。
  * 默认执行只读评估，只有显式添加 `--commit` 才会实际写入下载池。

### 2. ⚡ 自适应动态水位调度 (Dynamic Watermark)
* **恒定活跃水位**：不把 1,000 个任务全塞进迅雷，而是维持在 10~15 个高活跃任务，**吃掉一个，补充一个**。
* **死种梯级退避**：持续 3 小时 0 速度或元数据解析超时的任务，自动移出活跃槽位，按照 **6小时 ➔ 24小时 ➔ 72小时** 梯级退避重试，绝不霸占通道。
* **新任务最高优先级**：新导入任务初始标记为 `retry_count=0`，在调度中拥有最高优先级（Tier-Fresh），秒速投入下载。
* **SSD 双阈值防爆熔断**：SSD 剩余空间低于 120GB 自动停投，待搬运释放回 180GB 以上自动复苏（迟滞区间，避免临界值反复震荡）。

### 3. 🧹 冷热分层流转与安全清洗 (Storage Tiering)
* **工业级五重搬运判定**：进程文件描述符扫描、临时后缀黑名单（`.xltd`, `.cfg` 等）、文件 mtime/size 稳定静止期校验，确保只搬运已完成的完整正片。
* **广告与垃圾清理**：自动过滤并剔除体积小于 150MB 的样片、宣传片与广告。
* **8 重门槛空目录安全修剪**：30 分钟静止期 + 进程无打开句柄 + 迅雷活跃任务排除 + 数据库活跃任务排除 + 0-entry 真空目录检测，绝不误删。

### 4. 🤖 AI 智能刮削与大模型双路由
* **自适应批处理**：当暂存队列累积满 3 部或等待超过 30 分钟时，自动启动 JavSP 容器执行刮削。
* **翻译双引擎保底**：集成大模型翻译路由：
  * **主通道**：Google Gemini 云端极速翻译；
  * **本地保底**：本地部署的腾讯 Hy-MT2 1.8B 模型，0% 敏感词内容审查拦截，离线断网亦能完美汉化。

### 5. 📺 Emby 零负载单片叶子更新
* 归档成功后，调用 Emby 专有的 `POST /Library/Media/Updated` 接口，**仅传递本次新增影片的具体叶子目录**。
* 响应时间低至 0.009 秒，即刻出海报并更新媒体库，彻底消除传统“全库重扫”带来的机械盘震颤与 I/O 假死。

### 6. 🩺 全栈一键健康巡检 (Health Diagnostics)
* 单命令快速排查：迅雷 API、任务数据库、SSD 空间、JavSP 容器、本地大模型服务、翻译路由、Emby 直连与 Systemd 守护器。
* 严格遵循标准 Unix 退出码（0: 正常, 1: 告警, 2: 严重错误）。

---

## 快速上手 (Quick Start)

### 1. 环境准备
* **系统环境**：Linux / 群晖 Synology DSM 7.0+ / Unraid / 威联通 QNAP
* **依赖工具**：Python 3.10+、`ffprobe`（用于媒体编码分析）
* **下载引擎**：迅雷 NAS 版（或已部署的 Docker 迅雷客户端）

### 2. 克隆项目与安装依赖
```bash
git clone https://github.com/1154761334/MediaFlow-NAS.git
cd MediaFlow-NAS

# 安装依赖 (仅依赖 PyYAML，其余皆为 Python 原生标准库，极简轻量)
pip install -r requirements.txt
```

### 3. 配置参数
复制配置模板并修改路径：
```bash
cp config.example.yaml config.yaml
vim config.yaml
```
主要填写内容：
* `paths.ssd_download`：你的 SSD 下载盘路径（如 `/volume1/迅雷/下载`）
* `paths.hdd_staging`：机械盘暂存就绪目录（如 `/volume2/video/.../#重刮队列`）
* `paths.hdd_archive`：最终归档媒体库路径（如 `/volume2/video/.../#整理完成`）
* `xunlei.pan_auth_token`：迅雷 NAS 引擎认证 Token
* `emby.api_key`：你的 Emby API 密钥

### 4. 运行自动化测试
在正式投产前，运行内置的单元测试与回归套件：
```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```
输出显示 `Ran 13 tests in 0.008s OK` 即表示本地规则环境健全。

### 5. 导入第一批磁力链接
准备一个包含磁力链接的文件 `magnets.txt`：
```bash
# 步骤 1: 试运行评估 (Dry-Run，默认只读，不修改任何文件和数据库)
python3 gate/media_ingest.py magnets.txt

# 步骤 2: 确认放行与拦截策略符合预期后，正式提交入库
python3 gate/media_ingest.py magnets.txt --commit
```

### 6. 启动调度器
```bash
# 手动触发单轮自适应水位调度
python3 core/orchestrator.py --cycle

# 查看全景控制台仪表盘
python3 core/orchestrator.py --status

# 执行一键全系统健康巡检
python3 diagnostics/check_health.py
```

### 7. 配置 Systemd 定时守护 (推荐生产配置)
让 NAS 每 2 分钟自主巡检并消费任务：
```bash
sudo cp deploy/systemd/mediaflow.service /etc/systemd/system/
sudo cp deploy/systemd/mediaflow.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mediaflow.timer
```

---

## 常见疑问 (FAQ)

<details>
<summary><b>Q1: 为什么不直接用 qBittorrent / Transmission？</b></summary>
对于公网上的冷门影视或历史老种，传统的 BT/PT 客户端在没有 Peer 连接的情况下速度基本为 0。在国内网络环境下，迅雷 NAS 版是唯一能够利用庞大的 P2SP 缓存节点和离线加速跑满带宽的下载工具。本项目正是为了将迅雷 NAS 强大的下载能力与现代化自动媒体库无缝连接。
</details>

<details>
<summary><b>Q2: 为什么把“大体积”重新下载判定为错误？</b></summary>
很多旧时代的自动化脚本简单地用 <code>新文件大小 > 旧文件大小 * 1.3</code> 来判定画质升级。但在 HEVC/H.265 普及的今天，一部高质量压制的 1080P HEVC 可能只有 2.5GB，而源盘直接转出的 H.264 却有 6GB。两者画质肉眼几无差别甚至 HEVC 更纯净。如果仅以大小论画质，不仅白白消耗数倍带宽和硬盘，还会造成严重的同质重复。因此 MediaFlow-NAS 将编码视为存储效率属性，严格基于分辨率（1080P ➔ 4K）和内容版本（有码 ➔ 无码/中字）判定升级。
</details>

<details>
<summary><b>Q3: 死种被移出后，以后还能下载吗？</b></summary>
能。移出的任务只是暂时休眠并释放槽位给排队中的新任务。它会严格遵循 6小时 ➔ 24小时 ➔ 72小时 ➔ 每7天的节奏周期性复查，一旦网络中出现新做种者，调度器会立刻将其重新激活直至完成。
</details>

---

## 目录结构说明

```text
MediaFlow-NAS/
├── core/                # 核心调度、分层流转、迅雷 API 与空目录修剪
├── gate/                # 质量门禁判定、资产库 ffprobe 索引、磁力批量筛选
├── diagnostics/         # 全栈 8 维度健康巡检与自愈诊断
├── deploy/              # 生产部署脚本与 Systemd 守护器
├── docs/                # 深度架构设计与配置详解
├── tests/               # 自动化回归测试套件
├── config.example.yaml  # 详尽中文注释的全局配置模板
└── requirements.txt     # Python 依赖定义
```

---

## 开源许可

本项目遵循 [MIT License](LICENSE) 许可协议。欢迎提交 Issue 与 Pull Request 共同优化！
