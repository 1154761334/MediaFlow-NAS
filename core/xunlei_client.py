#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迅雷 NAS 本地接口适配器 (Xunlei NAS API Adapter)
支持通过 Unix Domain Socket 直接与底层引擎通信
覆盖: list_tasks / add_magnet / pause_task / resume_task / delete_task
"""

import os
import sys
import json
import socket
import http.client
import urllib.request
import re
import yaml
from pathlib import Path
from urllib.parse import unquote, quote

PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://tracker.dler.org:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce"
]

class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)

class XunleiAdapter:
    def __init__(self, config=None):
        self.config = config or {}
        self.sock_path = self.config.get("sock_path", "/volume1/@appstore/pan-xunlei-com/var/pan-xunlei-com.sock")
        self.info_file = self.config.get("info_file", "/volume1/@appstore/pan-xunlei-com/bin/bin/info.file")
        self.token = self.config.get("pan_auth_token", "")
        self.device_id = self._load_device_id()
        if not self.token:
            self.auto_refresh_token()

    def _load_device_id(self):
        try:
            if os.path.exists(self.info_file):
                with open(self.info_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("device_id", "")
        except Exception as e:
            print(f"警告: 无法读取 device_info: {e}")
        return ""

    def auto_refresh_token(self):
        """
        从 NAS 本地 127.0.0.1:25010 自动获取最新的 72 小时 pan-auth Token
        完全无需人工打开浏览器，支持开箱即用与自动续期
        """
        url = "http://127.0.0.1:25010/webman/3rdparty/pan-xunlei-com/index.cgi/"
        proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)
        try:
            with opener.open(url, timeout=5) as resp:
                html = resp.read().decode("utf-8", "ignore")
                m = re.search(r'function\s+uiauth\s*\(\s*value\s*\)\s*\{\s*return\s*\"([^\"]+)\"', html)
                if m:
                    new_token = m.group(1).strip()
                    self.token = new_token
                    print(f"[XunleiAdapter] 成功从本地 127.0.0.1:25010 自动获取/刷新 Token (前缀: {new_token[:25]}...)")
                    # 同步持久化到 config.yaml
                    try:
                        from core.config import find_config_path
                        cfg_file = find_config_path()
                    except Exception:
                        cfg_file = Path("config.yaml")
                    if cfg_file.exists():
                        try:
                            with open(cfg_file, "r", encoding="utf-8") as f:
                                cfg = yaml.safe_load(f) or {}
                            if "xunlei" not in cfg:
                                cfg["xunlei"] = {}
                            cfg["xunlei"]["pan_auth_token"] = new_token
                            with open(cfg_file, "w", encoding="utf-8") as f:
                                yaml.safe_dump(cfg, f, allow_unicode=True)
                        except Exception as ce:
                            print(f"[XunleiAdapter] 写入 config.yaml 异常 (不影响内存生效): {ce}")
                    return True
        except Exception as e:
            print(f"[XunleiAdapter] 本地自动获取 Token 失败: {e}")
        return False

    def _request(self, method, path, body=None, params=None, retry_auth=True):
        if not self.token:
            self.auto_refresh_token()

        if params:
            query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
            path = f"{path}?{query}" if "?" not in path else f"{path}&{query}"

        conn = UnixSocketHTTPConnection(self.sock_path)
        headers = {
            "Content-Type": "application/json",
            "Pan-Auth": self.token or ""
        }
        b_data = json.dumps(body) if body else None
        conn.request(method, path, body=b_data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw.decode("utf-8", "ignore")}

        # 检查是否为 Token 过期或鉴权失败，若是则自动续期并重试一次
        is_auth_error = (resp.status == 403) or (
            isinstance(data, dict) and any(err_k in str(data).lower() for err_k in ("checkauth failed", "token is expired", "invalid number of segments"))
        )
        if is_auth_error and retry_auth:
            print("[XunleiAdapter] 检测到 Token 过期或鉴权失效，正在从 NAS 本地回环自动续期并重试...")
            if self.auto_refresh_token():
                return self._request(method, path, body=body, params=params, retry_auth=False)

        return resp.status, data

    def set_token(self, token):
        self.token = token.strip()

    def list_tasks(self, limit=100):
        """
        获取当前迅雷所有任务 (运行中、排队中、暂停、已完成)
        """
        path = f"/drive/v1/tasks?space=device_id%23{self.device_id}&limit={limit}"
        status, data = self._request("GET", path)
        if status == 200:
            tasks_raw = data.get("tasks", [])
            parsed = []
            for t in tasks_raw:
                params = t.get("params", {}) or {}
                parsed.append({
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "phase": t.get("phase"),  # PHASE_TYPE_RUNNING, PHASE_TYPE_PENDING, PHASE_TYPE_PAUSED, PHASE_TYPE_COMPLETE
                    "progress": int(t.get("progress", 0) or 0),
                    "file_size": int(t.get("file_size", 0) or 0),
                    "speed": int(params.get("speed", 0) or 0),
                    "real_path": params.get("real_path", ""),
                    "url": params.get("url", "")
                })
            return True, parsed
        err = data.get("error_description") or data.get("error") or str(data)
        return False, err

    def add_magnet(self, magnet_url, name=None):
        """
        向迅雷提交一个磁力链接任务
        自动追加公网顶级 Trackers 列表以大幅加速做种节点发现
        """
        if not name:
            m = re.search(r'dn=([^&]+)', magnet_url)
            name = unquote(m.group(1)) if m else "unnamed_task"

        final_magnet = magnet_url
        if "&tr=" not in final_magnet and PUBLIC_TRACKERS:
            tr_params = "".join(f"&tr={quote(tr)}" for tr in PUBLIC_TRACKERS)
            final_magnet = f"{final_magnet}{tr_params}"

        payload = {
            "type": "user#download-url",
            "name": name,
            "file_name": name,
            "file_size": "0",
            "space": f"device_id#{self.device_id}",
            "params": {
                "target": f"device_id#{self.device_id}",
                "url": final_magnet,
                "total_file_count": "0",
                "parent_folder_id": "",
                "mime_type": ""
            }
        }
        status, data = self._request("POST", "/drive/v1/task", body=payload)
        if status == 200 and data.get("HttpStatus") == 0:
            task_info = data.get("task", {})
            return True, task_info.get("id"), task_info.get("name")
        err = data.get("error_description") or data.get("error") or str(data)
        return False, None, err

    def pause_task(self, task_id):
        """
        暂停指定任务
        """
        payload = {
            "space": f"device_id#{self.device_id}",
            "type": "user#download-url",
            "id": task_id,
            "set_params": {
                "spec": "{\"phase\":\"PHASE_TYPE_PAUSED\"}"
            }
        }
        status, data = self._request("POST", "/method/patch/drive/v1/task", body=payload)
        if status == 200 and data.get("HttpStatus") == 0:
            return True, ""
        err = data.get("error_description") or data.get("error") or str(data)
        return False, err

    def resume_task(self, task_id):
        """
        恢复指定任务下载
        """
        payload = {
            "space": f"device_id#{self.device_id}",
            "type": "user#download-url",
            "id": task_id,
            "set_params": {
                "spec": "{\"phase\":\"PHASE_TYPE_RUNNING\"}"
            }
        }
        status, data = self._request("POST", "/method/patch/drive/v1/task", body=payload)
        if status == 200 and data.get("HttpStatus") == 0:
            return True, ""
        err = data.get("error_description") or data.get("error") or str(data)
        return False, err

    def delete_task(self, task_ids):
        """
        从迅雷任务列表移除已完成或失效的任务 (不删本地文件)
        支持单个 task_id 或 task_ids 列表
        """
        if isinstance(task_ids, (list, set, tuple)):
            ids_str = ",".join(str(i) for i in task_ids if i)
        else:
            ids_str = str(task_ids)

        if not ids_str:
            return True, ""

        path = f"/method/delete/drive/v1/tasks?space=device_id%23{self.device_id}&task_ids={ids_str}"
        status, data = self._request("POST", path)
        if status == 200:
            return True, ""
        err = data.get("error_description") or data.get("error") or str(data)
        return False, err

    def set_runner_count(self, count: int):
        """
        设置迅雷最大并发下载任务数
        """
        status, data = self._request("POST", "/device/config", body={"runner_count": int(count)})
        if status == 200:
            return True, data.get("runner_count", count)
        err = data.get("error_description") or data.get("error") or str(data)
        return False, err

if __name__ == "__main__":
    adapter = XunleiAdapter()
    print(f"Device ID: {adapter.device_id}")
    ok, res = adapter.list_tasks()
    if ok:
        print(f"成功获取迅雷任务列表 ({len(res)} 个任务)")
        for t in res[:5]:
            print(f" - [{t['phase']}] {t['name']} ({t['progress']}%, {t['speed']/1024:.1f} KB/s)")
    else:
        print(f"获取任务失败: {res}")
