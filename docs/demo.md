# MediaFlow-NAS 离线交互演示指南 (Quick Demo Guide)

无需准备群晖 NAS 硬件，无需配置迅雷或部署 Docker。  
任何开发者只需克隆本项目，即可在本地 1 秒钟体验 MediaFlow-NAS 核心门禁判定与容量预算体系：

```bash
git clone https://github.com/1154761334/MediaFlow-NAS.git
cd MediaFlow-NAS

# 运行离线演示
python3 gate/demo_run.py
```

---

## 演示覆盖的核心现实场景

| 场景 | 候选磁力输入 | 本地存量状态 | 裁定结果 | 核心设计哲学 |
| :--- | :--- | :--- | :---: | :--- |
| **纯新片放行** | `WAAA-696-C` | 本地未收录 | **`NEW`** | 纯新片放行下载 |
| **4K 分辨率升维** | `URE-131.[4K]` | 本地仅存 720P | **`UPGRADE`** | 像素维度显著翻倍，放行下载 |
| **无码流出内容升级** | `IPX-037-UC` | 本地仅有普通有码版 | **`UPGRADE`** | 内容重大升级，放行下载 |
| **HEVC 资产保护** | `JUQ-123-C (6.2GB H264)` | 本地已有 2.5GB 1080P HEVC | **`SKIP`** | **坚决拦截**！体积大属 H.264 编码特性，绝不以体积论画质，杜绝低效重复下载 |
| **同质冗余拦截** | `STAR-999-4K` | 本地已有 4K UNC SUB HEVC | **`SKIP`** | 本地已有全覆盖优质版本，拦截冗余 |
| **证据不足暂缓** | `JUQ-123 高清` | 本地已有版本 | **`REVIEW`** | 仅标模糊“高清”且无明确升级凭证，默认不下载，防止盲目下载 |

---

## 试运行真实磁力文件 (Dry-Run & Audit)

如果你手头有真实的磁力清单文件（例如 `my_magnets.txt`），可直接运行只读审计模式：

```bash
# 1. 深度容量预算与风险审计 (默认只读，绝不修改任何数据库)
python3 gate/media_ingest.py my_magnets.txt --audit

# 2. 限制单批次配额导入 (例如限制前 100 部最高价值任务)
python3 gate/media_ingest.py my_magnets.txt --commit --quota 100
```
