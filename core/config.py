import os
import yaml
from pathlib import Path
from typing import Optional, Dict, Any

DEFAULT_CONFIG: Dict[str, Any] = {
    "paths": {
        "db_path": "./data/queue.db",
        "inventory_db": "./data/media_inventory.db",
        "ssd_download": "/volume1/迅雷/下载",
        "hdd_staging": "/volume2/video/avnook/#重刮队列/avok",
        "hdd_archive": "/volume2/video/avnook/#整理完成",
        "log_file": "./mover.log",
    },
    "scheduler": {
        "interval_seconds": 120,
        "target_active": 32,
        "low_watermark": 22,
        "max_physical_active": 45,
        "add_per_cycle": 8,
        "feed_delay_seconds": 2.5,
        "bandwidth_target_mb": 50.0,
        "slow_speed_threshold_kb": 50,
    },
    "stall": {
        "metadata_timeout_minutes": 15,
        "no_progress_hours": 0.8,
        "retry_hours": [6, 24, 72],
        "cold_retry_days": 7,
    },
    "storage": {
        "stop_free_gb": 120.0,
        "resume_free_gb": 180.0,
    },
    "xunlei": {
        "info_file": "/volume1/@appstore/pan-xunlei-com/bin/bin/info.file",
        "sock_path": "/volume1/@appstore/pan-xunlei-com/var/pan-xunlei-com.sock",
        "pan_auth_token": "",
        "runner_count": 24,
    },
    "scrape": {
        "docker_container": "javsp-avnook",
        "batch_count_trigger": 3,
        "max_wait_minutes": 30,
    },
    "emby": {
        "url": "http://127.0.0.1:8096",
        "api_key": "",
    },
    "translation": {
        "router_url": "http://127.0.0.1:18088",
    },
}

def find_config_path(custom_path: Optional[str] = None) -> Path:
    """按优先级搜寻配置文件"""
    if custom_path:
        p = Path(custom_path).resolve()
        if p.is_file():
            return p

    env_path = os.environ.get("MEDIAFLOW_CONFIG")
    if env_path:
        p = Path(env_path).resolve()
        if p.is_file():
            return p

    search_dirs = [
        Path.cwd(),
        Path(__file__).resolve().parent.parent,
        Path("/volume1/docker/MediaFlow-NAS"),
        Path("/volume1/docker/xunlei"),
        Path("/etc/mediaflow"),
    ]

    for d in search_dirs:
        candidate = d / "config.yaml"
        if candidate.is_file():
            return candidate.resolve()

    for d in search_dirs:
        candidate = d / "config.example.yaml"
        if candidate.is_file():
            return candidate.resolve()

    return Path(__file__).resolve().parent.parent / "config.yaml"

def load_config(custom_path: Optional[str] = None) -> Dict[str, Any]:
    """加载配置并用默认值填充缺失项"""
    cfg_file = find_config_path(custom_path)
    if not cfg_file.is_file():
        return DEFAULT_CONFIG.copy()

    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
    except Exception:
        return DEFAULT_CONFIG.copy()

    # 递归合并默认值
    merged = DEFAULT_CONFIG.copy()
    for section, values in user_cfg.items():
        if isinstance(values, dict) and section in merged and isinstance(merged[section], dict):
            merged[section] = {**merged[section], **values}
        else:
            merged[section] = values

    return merged
