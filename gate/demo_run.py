#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MediaFlow-NAS 离线独立交互演示 (Standalone Offline Demo)

无需群晖硬件、无需 NAS 环境、无需迅雷引擎。
任何开发者 clone 本项目后，单命令体验核心价值：
1. 本地媒体资产多版本真实 Profile 比对
2. HEVC 保护原则 (体积大 != 画质升级)
3. 升维判定 (4K/无码流出/中文字幕)
4. 容量预算预估与分批配额建议
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gate.media_quality import (
    extract_canonical_avid,
    parse_quality_tags,
    evaluate_quality_decision_multi,
    normalize_btih
)

# 模拟本地影视库中已有的真实影片资产 (覆盖单版本、多版本、HEVC高效压制等场景)
MOCK_LOCAL_INVENTORY = {
    "JUQ-123": [
        {"resolution": "1080P", "rank": 2, "is_unc": False, "is_sub": True, "codec": "HEVC", "variant": "SUB", "size_bytes": 2684354560}
    ],
    "IPX-037": [
        {"resolution": "1080P", "rank": 2, "is_unc": False, "is_sub": False, "codec": "H264", "variant": "NORMAL", "size_bytes": 5368709120}
    ],
    "URE-131": [
        {"resolution": "720P", "rank": 1, "is_unc": False, "is_sub": False, "codec": "H264", "variant": "NORMAL", "size_bytes": 1610612736}
    ],
    "STAR-999": [
        {"resolution": "4K", "rank": 3, "is_unc": True, "is_sub": True, "codec": "HEVC", "variant": "UNC+SUB+4K", "size_bytes": 8589934592}
    ]
}

# 模拟用户输入的一组典型磁力链接候选
MOCK_CANDIDATES = [
    {
        "name": "【典型 1: 纯新影片放行】",
        "dn": "WAAA-696-C デカパイ女教師リモバイ調教 高清 字幕",
        "btih": "13909334754d2785302d47404c3acc47e82c67ec"
    },
    {
        "name": "【典型 2: 4K 分辨率重大升维】",
        "dn": "URE-131.[4K]@R90s 高清",
        "btih": "76230124e271150de3038194a128b91030403f91"
    },
    {
        "name": "【典型 3: 无码流出 (UNC) 内容升级】",
        "dn": "IPX-037-UC 无码流出 高清",
        "btih": "7a179d360a59053fcfba2527fc09dbb7ac6eb0e3"
    },
    {
        "name": "【典型 4: 触发 HEVC 保护法则 (坚决拦截低效同质大文件)】",
        "dn": "JUQ-123-C 1080P 6.2GB H264 字幕",
        "btih": "bc6ba752bfb9b598e3d40b5d4a8ac6a081515005"
    },
    {
        "name": "【典型 5: 本地已有全覆盖版本 (拦截冗余)】",
        "dn": "STAR-999-4K 高清 字幕",
        "btih": "60dcf59e748f088ca3d984018006d98796636460"
    },
    {
        "name": "【典型 6: 信息不足暂缓下载】",
        "dn": "JUQ-123 高清",
        "btih": "4d73aa09cc9a76aeefb624637eb292eb818b2c9c"
    }
]

def run_demo():
    print("=" * 75)
    print("        MediaFlow-NAS 质量门禁与决策引擎离线演示 (Demo)")
    print("=" * 75)
    print(f"已模拟加载本地媒体库番号: {len(MOCK_LOCAL_INVENTORY)} 部")
    print("-" * 75)

    stats = {"NEW": 0, "UPGRADE": 0, "SKIP": 0, "REVIEW": 0}

    for idx, c in enumerate(MOCK_CANDIDATES, 1):
        dn = c["dn"]
        avid = extract_canonical_avid(dn)
        cand_prof = parse_quality_tags(dn)

        print(f"\n{c['name']}")
        print(f"  磁力标题: {dn}")
        print(f"  识别番号: [{avid}] | 候选属性: {cand_prof['resolution']} | 变体: {cand_prof['variant']}")

        if not avid:
            dec, reason = "REVIEW", "无法从标题中提取规范番号"
        elif avid not in MOCK_LOCAL_INVENTORY:
            dec, reason = "NEW", "本地媒体库未收录，放行作为纯新片"
        else:
            local_profs = MOCK_LOCAL_INVENTORY[avid]
            dec, reason = evaluate_quality_decision_multi(local_profs, cand_prof)

        stats[dec] += 1
        color_map = {
            "NEW": "\033[32m★ 放行下载 (NEW)\033[0m",
            "UPGRADE": "\033[35m★ 升维下载 (UPGRADE)\033[0m",
            "SKIP": "\033[33m○ 智能拦截 (SKIP)\033[0m",
            "REVIEW": "\033[34m▲ 暂缓待查 (REVIEW)\033[0m"
        }
        print(f"  门禁裁决: {color_map[dec]}")
        print(f"  决策理由: {reason}")

    print("\n" + "=" * 75)
    print("                        全批次汇总与容量审计")
    print("=" * 75)
    print(f"  • 纯新影片准入 (NEW):       {stats['NEW']} 部")
    print(f"  • 规格升级准入 (UPGRADE):   {stats['UPGRADE']} 部")
    print(f"  • 同质冗余拦截 (SKIP):      {stats['SKIP']} 部  (捍卫带宽与硬盘)")
    print(f"  • 凭据不足暂缓 (REVIEW):    {stats['REVIEW']} 部  (避免盲目下载)")
    print("-" * 75)
    will_import = stats["NEW"] + stats["UPGRADE"]
    print(f"  ★ 最终准入率: {will_import}/{len(MOCK_CANDIDATES)} 部 | 避免了 {stats['SKIP'] + stats['REVIEW']} 部无效下载！")
    print(f"  • 预计新增存储: ~{will_import * 4.5:.1f} GB (基于均值 4.5GB/部)")
    print("=" * 75)

if __name__ == "__main__":
    run_demo()
