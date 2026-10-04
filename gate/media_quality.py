#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media Automation v2.0R — 统一媒体质量规则引擎 (Media Quality Engine Correctness Hardened)

核心设计准则:
1. Canonical AVID: 权威提取并规范番号（前缀白名单剥离、特殊复合格式转换、后缀清理）。
2. Canonical BTIH: 40位小写十六进制，支持 32位 Base32 自动解算标准化。
3. Quality Profile: 提取分辨率等级 (支持6K/4K/1080P/720P/SD)、无码流出属性、中文字幕属性、视频编码 (HEVC/H264)。
4. Multi-Profile Decision Matrix (彻底废除虚构合并超级版本):
   - 真实多版本列表匹配: 任一真实版本完全覆盖候选即 SKIP；
   - 绝不以“文件体积更大”盲目判定画质更高；
   - 保护现有 HEVC 资产，同等规格下默认拒绝冗余 H264；
   - 强升级信号（像素升维 4K/6K、无码流出、中文字幕）明确放行；
   - UNKNOWN 分辨率前置拦截，绝不因 Rank 误判；
   - 判定结果严格收敛为: NEW, UPGRADE, SKIP, REVIEW 四类。
"""

import re
import base64
from typing import Optional, Dict, Tuple, Any, List

KNOWN_RELEASE_PREFIXES = {
    "420", "546", "702", "360", "326", "328", "858", "865",
    "857", "393", "348", "458", "476", "107", "112"
}
AVID_PATTERN = re.compile(r"^[A-Z0-9]+-\d+$", re.IGNORECASE)

RESOLUTION_RANKS = {
    "SD": 0,
    "720P": 1,
    "1080P": 2,
    "4K": 3,
    "6K": 4,
    "UNKNOWN": -1  # 设为 -1，绝对低于任何已知分辨率 Rank (SD=0)
}

def normalize_btih(raw_btih: str) -> str:
    """
    统一将 40位十六进制或 32位 Base32 BTIH 标准化为 40位小写 Hex
    杜绝同 Torrent 因编码表示不同绕过 HASH_DUP 查重
    """
    if not raw_btih:
        return ""
    clean = raw_btih.strip()
    if len(clean) == 40:
        try:
            int(clean, 16)
            return clean.lower()
        except ValueError:
            return ""
    elif len(clean) == 32:
        try:
            raw_bytes = base64.b32decode(clean.upper())
            return raw_bytes.hex().lower()
        except Exception:
            return ""
    return ""

def extract_canonical_avid(raw_str: str) -> str:
    """
    严格从文件名、目录名或磁力 dn 字符串中提取规范番号 (Canonical AVID):
    1. 剥离版本与画质后缀: -U, -UC, -C, _UNC, -4K, -CD1, -HD, -FHD
    2. 前置处理特殊复合前缀标准化 (如 CPZ69-015 -> CPZ-69015, 420ERK-111 -> ERK-111)
    3. 校验白名单前缀剥离: 仅当以 KNOWN_RELEASE_PREFIXES 开头且剥离后仍符合标准番号结构才剥离
    4. 统一大写与中划线
    """
    if not raw_str:
        return ""
    clean = raw_str.strip().upper().replace("_", "-")
    clean = re.sub(r"([A-Z0-9]+)-\s+(\d+)", r"\1-\2", clean)
    clean = re.sub(r"-(U|UC|C|4K|4KS|6K|CD\d+|HD|FHD|UNC)$", "", clean)
    clean = re.sub(r"-[U|C]+$", "", clean)

    # 前置特殊复合前缀标准化 (防止进入常规 [A-Z0-9]+-\d+ 导致漏匹配)
    clean = re.sub(r"CPZ69-(\d+)", r"CPZ-69\1", clean)
    clean = re.sub(r"^420([A-Z]+-\d+)", r"\1", clean)

    m = re.search(r"([A-Z0-9]+-\d+)", clean)
    if not m:
        return ""
    cand = m.group(1)
    for pfx in KNOWN_RELEASE_PREFIXES:
        if cand.startswith(pfx):
            sub_cand = cand[len(pfx):]
            if AVID_PATTERN.match(sub_cand):
                return sub_cand
    return cand

def parse_quality_tags(filename: str, width: int = 0, height: int = 0, codec: str = "") -> Dict[str, Any]:
    """
    解析媒体文件的质量特征。
    支持结合真实 ffprobe 探测参数 (width, height, codec) 与文件名文本特征。
    支持 6K / 4K / 1080P / 720P / SD 阶梯。
    """
    t = filename.lower()
    
    # 1. 分辨率判决 (支持真实 6K ffprobe 参数)
    res = "UNKNOWN"
    if width > 0 or height > 0:
        if width >= 5000 or height >= 2800:
            res = "6K"
        elif width >= 3800 or height >= 2100:
            res = "4K"
        elif width >= 1800 or height >= 1000:
            res = "1080P"
        elif width >= 1200 or height >= 700:
            res = "720P"
        else:
            res = "SD"
    else:
        # 基于文件名文本推断
        if any(k in t for k in ["6k"]):
            res = "6K"
        elif any(k in t for k in ["4k", "4ks", "2160p"]):
            res = "4K"
        elif any(k in t for k in ["1080p", "fhd"]):
            res = "1080P"
        elif any(k in t for k in ["720p", "hd"]):
            res = "720P"
        else:
            res = "UNKNOWN"

    # 2. 无码 / 流出特征
    is_unc = any(k in t for k in [
        "uncensored", "leaked", "無修正", "无码", "流出", "破解",
        "-u", "-uc", "_uc", "_unc", "unc"
    ])

    # 3. 中文字幕特征
    is_sub = any(k in t for k in [
        "-c", "字幕", "中字", "中文", "_ch", "ch.", "-ch"
    ])

    # 4. 视频编码
    codec_name = "H264"
    c_lower = codec.lower() if codec else ""
    if "hevc" in c_lower or "h265" in c_lower or "x265" in c_lower or any(k in t for k in ["hevc", "h265", "x265"]):
        codec_name = "HEVC"
    elif "av1" in c_lower:
        codec_name = "AV1"

    # 5. 组合变体标签 (Variant)
    var_parts = []
    if is_unc:
        var_parts.append("UNC")
    if res in ("4K", "6K"):
        var_parts.append(res)
    if is_sub:
        var_parts.append("SUB")
    variant = "+".join(var_parts) if var_parts else "NORMAL"

    return {
        "resolution": res,
        "rank": RESOLUTION_RANKS.get(res, 1),
        "is_unc": is_unc,
        "is_sub": is_sub,
        "codec": codec_name,
        "variant": variant
    }

def evaluate_quality_decision(local_profile: Optional[Dict[str, Any]], candidate_profile: Dict[str, Any]) -> Tuple[str, str]:
    """
    单对单质量仲裁门禁 (已修正 UNKNOWN 漏洞):
    返回 (DECISION, REASON)
    DECISION 严格限定为: NEW, UPGRADE, SKIP, REVIEW
    """
    if not local_profile:
        return "NEW", "本地未拥有该番号，放行全新下载"

    cand_res = candidate_profile.get("resolution", "UNKNOWN")
    local_res = local_profile.get("resolution", "1080P")
    cand_rank = RESOLUTION_RANKS.get(cand_res, 1)
    local_rank = RESOLUTION_RANKS.get(local_res, 2)

    cand_unc = candidate_profile.get("is_unc", False)
    local_unc = local_profile.get("is_unc", False)

    cand_sub = candidate_profile.get("is_sub", False)
    local_sub = local_profile.get("is_sub", False)

    cand_codec = candidate_profile.get("codec", "H264")
    local_codec = local_profile.get("codec", "H264")

    # 0. 强升级信号检查: 无码超越或字幕补充 (即使分辨率未知也是升级)
    if cand_unc and not local_unc:
        return "UPGRADE", "内容重大升级: 候选版本为无码/流出版本 (本地仅为有码)"
    if cand_sub and not local_sub:
        return "UPGRADE", "中文字幕升级: 候选版本包含中文字幕 (本地为生肉无字幕)"

    # 1. 【P0 修复】: 候选分辨率未知 (UNKNOWN) 必须前置拦截，绝不能因为 Rank 判定为 UPGRADE
    if cand_res == "UNKNOWN" or not candidate_profile:
        return "REVIEW", "信息不足: 缺乏明确提升凭证 (4K/无码/字幕)，默认不下载"

    # 2. 强升级信号: 像素升维 (分辨率超越本地，例如 1080P -> 4K/6K, 720P -> 1080P)
    if cand_rank > local_rank:
        return "UPGRADE", f"分辨率升维: 候选版本 ({cand_res}) 优于本地 ({local_res})"

    # 3. 分辨率明显倒退
    if cand_rank < local_rank:
        return "SKIP", f"画质明显倒退: 候选版本 ({cand_res}) 低于本地现存 ({local_res})"

    # 4. 【核心 HEVC 资产保护法】: 同分辨率同内容下，保护现有 HEVC
    # 本地已有 HEVC 且各项内容属性不输候选，候选即使文件体积再大，也是 H264 编码效率问题，直接 SKIP
    if local_codec == "HEVC" and cand_codec != "HEVC":
        if (cand_unc == local_unc) and (cand_sub == local_sub):
            return "SKIP", "保护现有 HEVC 资产: 本地已有同分辨率 HEVC 高效编码版本，候选体积大属 H264 特性，非画质升级"

    # 5. 同质完全冗余: 分辨率、无码性、字幕完全一致或本地更优
    if (cand_rank <= local_rank) and (not cand_unc or local_unc) and (not cand_sub or local_sub):
        return "SKIP", f"同质冗余: 本地已有同等或更高规格版本 (本地: {local_profile.get('variant', 'NORMAL')} {local_codec})"

    return "REVIEW", "规格相当或存在取舍冲突，默认不发起下载"

def evaluate_quality_decision_multi(local_profiles: Optional[List[Dict[str, Any]]], candidate_profile: Dict[str, Any]) -> Tuple[str, str]:
    """
    多版本真实列表仲裁 (彻底废除虚构合并超级 Profile):
    1. 若本地无任何记录 -> NEW
    2. 遍历所有真实存在的旧版本:
       - 只要本地存在任一版本判定该候选为 SKIP (即该真实版本已完全覆盖候选且不输于候选) -> 返回 SKIP!
    3. 若没有任一单文件覆盖候选:
       - 检查候选是否对集合带来未被拥有的能力:
         * 候选分辨率高于所有本地版本 -> UPGRADE (分辨率升维)
         * 候选为无码且本地所有版本皆为有码 -> UPGRADE (无码升级)
         * 候选为中字且本地所有版本皆无字幕 -> UPGRADE (字幕补充)
         * 候选为多属性更优组合 (例如本地有4K有码和1080P无码，候选为4K无码) -> UPGRADE
       - 否则 -> REVIEW (属性取舍冲突或信息不足)
    """
    if not local_profiles:
        return "NEW", "本地未拥有该番号，放行全新下载"

    valid_locals = [p for p in local_profiles if isinstance(p, dict) and p]
    if not valid_locals:
        return "NEW", "本地未拥有该番号，放行全新下载"

    # 门槛 1: 只要任一本地真实文件完全覆盖候选 -> SKIP (杜绝重复下载已有同等内容)
    for loc in valid_locals:
        dec, rsn = evaluate_quality_decision(loc, candidate_profile)
        if dec == "SKIP":
            return "SKIP", f"同质冗余: 本地已有完全覆盖此规格的真实版本 (本地文件特性: {loc.get('variant', 'NORMAL')} {loc.get('codec', '')}, 原因: {rsn})"

    # 门槛 2: 检查是否有针对集合的真实升维
    max_local_rank = max(loc.get("rank", 1) for loc in valid_locals)
    cand_res = candidate_profile.get("resolution", "UNKNOWN")
    cand_rank = RESOLUTION_RANKS.get(cand_res, 1)

    has_any_local_unc = any(loc.get("is_unc", False) for loc in valid_locals)
    has_any_local_sub = any(loc.get("is_sub", False) for loc in valid_locals)

    cand_unc = candidate_profile.get("is_unc", False)
    cand_sub = candidate_profile.get("is_sub", False)

    if cand_res != "UNKNOWN" and cand_rank > max_local_rank:
        return "UPGRADE", f"分辨率升维: 候选版本 ({cand_res}) 超越本地所有存量版本 (本地最高: {max_local_rank})"

    if cand_unc and not has_any_local_unc:
        return "UPGRADE", "内容重大升级: 候选版本为无码/流出版本 (本地现存所有版本皆为有码)"

    if cand_sub and not has_any_local_sub:
        return "UPGRADE", "中文字幕升级: 候选版本包含中文字幕 (本地现存所有版本皆无字幕)"

    # 检查复合升级 (例如本地最高无码仅为 1080P，而候选为 4K+UNC)
    if cand_unc:
        unc_locals = [loc for loc in valid_locals if loc.get("is_unc")]
        if unc_locals:
            max_unc_rank = max(loc.get("rank", 1) for loc in unc_locals)
            if cand_res != "UNKNOWN" and cand_rank > max_unc_rank:
                return "UPGRADE", f"无码版本分辨率升级: 候选无码为 {cand_res} (本地无码最高仅为 {max_unc_rank})"

    if cand_res == "UNKNOWN":
        return "REVIEW", "信息不足: 缺乏明确画质或版本提升凭证，默认不下载"

    return "REVIEW", "多版本存在属性取舍冲突或证据不充分，默认不发起下载"
