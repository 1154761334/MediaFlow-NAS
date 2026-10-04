# MediaFlow-NAS 配置参数完全手册 (Configuration Guide)

本文档详细解释 `config.yaml` 中所有配置参数的含义、默认值与生产调优建议。

---

## 1. 存储与路径设置 (`paths`)

```yaml
paths:
  db_path: ./data/queue.db
  inventory_db: ./data/media_inventory.db
  ssd_download: /volume1/迅雷/下载
  hdd_staging: /volume2/video/avnook/#重刮队列/avok
  hdd_archive: /volume2/video/avnook/#整理完成
  log_file: ./mover.log
```

* **`db_path`**: 调度任务状态数据库（SQLite）。记录所有待下载、下载中、完成与重试的任务。
* **`inventory_db`**: 媒体资产库索引数据库（SQLite）。记录存量影片的分辨率、Codec、变体等信息。
* **`ssd_download`**: 高速下载缓冲路径。必须与迅雷 NAS 引擎中的默认下载路径一致。
* **`hdd_staging`**: 机械盘暂存就绪目录。已完成下载的影片经过样片清洗后，会被搬运到此目录等待刮削。
* **`hdd_archive`**: 最终归档正片影视库根目录。刮削成功后文件存入此处，并向 Emby 上报。
* **`log_file`**: 调度与搬运运行日志的输出文件路径。

---

## 2. 调度引擎参数 (`scheduler`)

```yaml
scheduler:
  interval_seconds: 120
  target_active: 32
  low_watermark: 22
  max_physical_active: 45
  add_per_cycle: 8
  feed_delay_seconds: 2.5
  bandwidth_target_mb: 50.0
  slow_speed_threshold_kb: 50
```

* **`interval_seconds`**: 每次调度周期执行间隔（默认 120 秒）。
* **`target_active`**: 目标维持的活跃下载任务数量（建议 15~35 个）。
* **`low_watermark`**: 最低水位。当迅雷中活跃任务数低于此值时，触发自动向队列申请新任务。
* **`max_physical_active`**: 迅雷客户端物理槽位上限。防止注入过多任务造成迅雷 NAS 套件内存溢出或假死。
* **`add_per_cycle`**: 单轮调度最多允许新注入的任务数量（防止突发脉冲式冲击引擎）。
* **`feed_delay_seconds`**: 向迅雷 API 投递每个磁力之间的休眠间隔（秒）。
* **`bandwidth_target_mb`**: 目标下行带宽阈值（MB/s）。当总下载速度超过此值时，暂停新任务注入，保护宽带。
* **`slow_speed_threshold_kb`**: 慢速任务阈值（KB/s）。低于此速度的任务被计入慢速观察期。

---

## 3. 死种让位与退避策略 (`stall`)

```yaml
stall:
  metadata_timeout_minutes: 15
  no_progress_hours: 0.8
  retry_hours:
    - 6
    - 24
    - 72
  cold_retry_days: 7
```

* **`metadata_timeout_minutes`**: 磁力链接解析种子元数据（Metadata）超时让位时间（默认 15 分钟）。
* **`no_progress_hours`**: 持续 0 速度判定死种时间（小时）。超过此时间任务自动暂停并移出活跃槽位，状态变更为 `retry`。
* **`retry_hours`**: 梯级退避重试时间表。
  * 第 1 次重试：让位 6 小时后；
  * 第 2 次重试：让位 24 小时后；
  * 第 3 次重试：让位 72 小时后。
* **`cold_retry_days`**: 长期冷门死种排查周期（默认每 7 天复活一次）。

---

## 4. 存储熔断保护 (`storage`)

```yaml
storage:
  stop_free_gb: 120.0
  resume_free_gb: 180.0
```

* **`stop_free_gb`**: 停止投递线。当 SSD 剩余可用空间低于该值（如 120GB）时，调度器自动熔断，暂停所有新任务注入。
* **`resume_free_gb`**: 恢复投递线。随着后台 Mover 将已完成影片持续搬迁至 HDD，当 SSD 剩余空间回升至该值（如 180GB）以上时，自动恢复投递。

---

## 5. 迅雷 NAS 引擎集成 (`xunlei`)

```yaml
xunlei:
  info_file: /volume1/@appstore/pan-xunlei-com/bin/bin/info.file
  sock_path: /volume1/@appstore/pan-xunlei-com/var/pan-xunlei-com.sock
  pan_auth_token: "YOUR_TOKEN"
  runner_count: 24
```

* **`info_file`**: 群晖迅雷套件的内部网络与端口映射文件。
* **`sock_path`**: 迅雷的 Unix Domain Socket 路径。优先通过本地域套接字直接通信，绕过 HTTP 代理干扰。
* **`pan_auth_token`**: 迅雷 NAS 本地 API 调用的鉴权 Token。
* **`runner_count`**: 迅雷内部并发 Runner 线程数。

---

## 6. 刮削容器与 Emby 集成 (`scrape` & `emby`)

```yaml
scrape:
  docker_container: javsp-avnook
  batch_count_trigger: 3
  max_wait_minutes: 30

emby:
  url: "http://127.0.0.1:8096"
  api_key: "YOUR_EMBY_API_KEY"
```

* **`docker_container`**: JavSP 刮削容器的容器名称。
* **`batch_count_trigger`**: 批处理阈值。暂存区累积待刮片达到此数量时，自动 `docker start` 启动刮削容器。
* **`max_wait_minutes`**: 超时强制启动时间（分钟）。暂存区即便不足 3 部，只要有未刮削影片等待超过 30 分钟，亦触发启动。
* **`emby.url`**: Emby 服务的本地内网直连地址（严禁走外部翻墙代理）。
* **`emby.api_key`**: Emby 用户的 API 密钥。
