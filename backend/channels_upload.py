# -*- coding: utf-8 -*-
"""
视频号上传编排：下载→传腾讯云COS→组数据结构→传服务器
"""

import threading
import time
import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from qcloud_cos import CosConfig, CosS3Client

from backend.config import get_settings, load_json, save_json, DATA_DIR, get_proxies_dict
from backend.channels import CHANNELS_FEEDS_FILE, CHANNELS_FAVORITES_FILE, decrypt_channels_data, add_channels_history_item

logger = logging.getLogger(__name__)

CHANNELS_UPLOAD_LOG_FILE = DATA_DIR / "channels_upload_log.jsonl"

_upload_lock = threading.Lock()
_upload_running = False


def _log_event(event: dict):
    """追加一行审计日志到 channels_upload_log.jsonl，供事后确认整个上传流程"""
    event["ts"] = int(time.time() * 1000)
    try:
        with open(CHANNELS_UPLOAD_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[视频号上传] 写审计日志失败: {e}")

def process_pending_uploads():
    """扫描未上传项→批量下载+COS上传→按批POST服务器"""
    global _upload_running

    with _upload_lock:
        if _upload_running:
            logger.info("[视频号上传] 跳过：已有上传任务运行中")
            return {"skipped": True, "reason": "already_running"}
        _upload_running = True

    try:
        logger.info("[视频号上传] ========== 开始上传流程 ==========")
        settings = get_settings()
        if not settings.get("channels_upload_enabled"):
            logger.info("[视频号上传] 跳过：功能未启用")
            return {"skipped": True, "reason": "disabled"}

        server_url = (settings.get("channels_upload_url") or "").strip()
        if not server_url:
            logger.error("[视频号上传] 错误：未配置 channels_upload_url")
            return {"error": "channels_upload_url not configured"}

        cos_cfg = {
            "secret_id": settings.get("cos_secret_id", "").strip(),
            "secret_key": settings.get("cos_secret_key", "").strip(),
            "region": settings.get("cos_region", "").strip(),
            "bucket": settings.get("cos_bucket", "").strip(),
            "prefix": settings.get("cos_prefix", "channels/").strip(),
            "cds_domain": settings.get("cos_cds_domain", "").strip(),
        }
        if not all([cos_cfg["secret_id"], cos_cfg["secret_key"], cos_cfg["region"], cos_cfg["bucket"]]):
            logger.error("[视频号上传] 错误：COS凭证不完整")
            return {"error": "COS credentials incomplete"}

        device_id = settings.get("channels_device_id", "视频号_caiji2").strip() or "视频号_caiji2"
        logger.info(f"[视频号上传] 配置加载完成 - 目标服务器: {server_url}, 设备ID: {device_id}, COS区域: {cos_cfg['region']}")

        feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
        favs = load_json(CHANNELS_FAVORITES_FILE, [])
        nick_map = {f.get("username"): f.get("nickname") for f in favs if isinstance(f, dict) and f.get("username")}

        pending = []
        for username, items in feeds_db.items():
            for item in items:
                # 仅处理本次新同步的作品（needs_upload）；历史积压不自动上传
                if item.get("needs_upload") and not item.get("uploaded") and item.get("video_url"):
                    pending.append((username, item))

        if not pending:
            logger.info("[视频号上传] 跳过：无本次新同步的待上传内容")
            return {"skipped": True, "reason": "no_pending"}

        logger.info(f"[视频号上传] 发现 {len(pending)} 个待上传视频，分 {(len(pending) + 4) // 5} 批处理")
        _log_event({"event": "start", "pending": len(pending)})
        batches = [pending[i:i+5] for i in range(0, len(pending), 5)]
        total_success = 0

        for batch_idx, batch in enumerate(batches, 1):
            logger.info(f"[视频号上传] --- 第 {batch_idx}/{len(batches)} 批，包含 {len(batch)} 个视频 ---")
            cos_results = _batch_cos_upload(batch, cos_cfg)
            cos_fail_ids = [str(item.get("id", "")) for (u, item), url in cos_results if not url]
            server_records = []
            for (username, item), cos_url in cos_results:
                if cos_url:
                    item["cos_url"] = cos_url
                    server_records.append({
                        "feedId": str(item.get("id", "")),
                        "description": item.get("description", ""),
                        "nickName": nick_map.get(username) or item.get("nickname") or username,
                        "url": cos_url,
                        "publishTime": int(item.get("createtime") or 0),
                        "insertTime": int(time.time()),
                        "id": 0,
                        "commentCount": item.get("comment_count", 0),
                        "favCount": item.get("fav_count", 0),
                        "forwardCount": item.get("forward_count", 0),
                        "likeCount": item.get("like_count", 0),
                    })

            save_json(CHANNELS_FEEDS_FILE, feeds_db)

            if server_records:
                logger.info(f"[视频号上传] 准备POST到服务器，包含 {len(server_records)} 条记录")
                ok = _post_to_server(server_url, server_records, device_id)
                if ok:
                    logger.info(f"[视频号上传] ✓ 服务器接受成功，标记 {len(server_records)} 个为已上传")
                    for (username, item), _ in cos_results:
                        if item.get("cos_url"):
                            item["uploaded"] = True
                            item["upload_time"] = int(time.time())
                            item.pop("needs_upload", None)
                            # 写入视频号下载历史，让自动上传的内容也出现在历史记录中
                            try:
                                add_channels_history_item(
                                    item.get("description") or item.get("id"),
                                    "视频(自动上传)",
                                    item["cos_url"],
                                    item.get("upload_size", 0),
                                )
                            except Exception as eh:
                                logger.warning(f"[视频号上传] 写历史记录失败: {eh}")
                            total_success += 1
                    save_json(CHANNELS_FEEDS_FILE, feeds_db)
                else:
                    logger.warning(f"[视频号上传] ✗ 服务器POST失败，本批 {len(server_records)} 个视频未标记为已上传")
                _log_event({"event": "batch", "idx": batch_idx, "total_batches": len(batches),
                            "cos_ok": len(server_records), "cos_fail": cos_fail_ids, "server_ok": ok})
            else:
                logger.warning(f"[视频号上传] 本批无成功上传到COS的视频，跳过POST")
                _log_event({"event": "batch", "idx": batch_idx, "total_batches": len(batches),
                            "cos_ok": 0, "cos_fail": cos_fail_ids, "server_ok": None})

        logger.info(f"[视频号上传] ========== 上传完成：处理 {len(pending)} 个，成功 {total_success} 个 ==========")
        _log_event({"event": "done", "processed": len(pending), "uploaded": total_success})
        return {"success": True, "processed": len(pending), "uploaded": total_success}
    finally:
        with _upload_lock:
            _upload_running = False

def _batch_cos_upload(batch, cos_cfg):
    """5个并发：下载+解密+COS上传，返回[(username,item,cos_url)]"""
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_download_and_cos, username, item, cos_cfg): (username, item) for username, item in batch}
        for future in as_completed(futures):
            username, item = futures[future]
            feed_id = item.get('id', 'unknown')
            try:
                cos_url = future.result()
                logger.info(f"[视频号上传] ✓ feedId={feed_id} COS上传成功: {cos_url}")
                results.append(((username, item), cos_url))
            except Exception as e:
                logger.error(f"[视频号上传] ✗ feedId={feed_id} COS上传失败: {e}")
                results.append(((username, item), None))
    return results

def _download_and_cos(username, item, cos_cfg):
    """下载CDN→解密→上传COS→返回公网URL"""
    feed_id = item.get("id", "unknown")

    if item.get("cos_url"):
        logger.debug(f"[视频号上传] feedId={feed_id} 已有COS URL，跳过")
        return item["cos_url"]

    video_url = item.get("video_url_h265") or item.get("video_url_h264") or item.get("video_url")
    if not video_url:
        raise ValueError("No video_url")

    logger.debug(f"[视频号上传] feedId={feed_id} 开始下载: {video_url[:80]}...")
    resp = requests.get(video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    resp.raise_for_status()
    data = bytearray(resp.content)
    item["upload_size"] = len(data)
    logger.debug(f"[视频号上传] feedId={feed_id} 下载完成，大小: {len(data)} bytes")

    decode_key = item.get("decode_key", "")
    if decode_key:
        try:
            decrypt_channels_data(data, int(decode_key))
            logger.debug(f"[视频号上传] feedId={feed_id} 解密完成")
        except Exception as e:
            logger.warning(f"[视频号上传] feedId={feed_id} 解密失败: {e}")

    config = CosConfig(Region=cos_cfg["region"], SecretId=cos_cfg["secret_id"], SecretKey=cos_cfg["secret_key"])
    client = CosS3Client(config)

    filename = f"{item.get('id', int(time.time()))}.mp4"
    key = cos_cfg["prefix"] + filename

    logger.debug(f"[视频号上传] feedId={feed_id} 开始上传到COS: {key}")
    client.put_object(Bucket=cos_cfg["bucket"], Body=bytes(data), Key=key)

    cds = cos_cfg.get("cds_domain")
    if cds:
        cos_url = cds.rstrip("/") + "/" + key
    else:
        cos_url = f"https://{cos_cfg['bucket']}.cos.{cos_cfg['region']}.myqcloud.com/{key}"

    return cos_url

def _post_to_server(url, records, device_id):
    """POST {data, deviceId} 到服务器，返回bool"""
    try:
        payload = {"data": records, "deviceId": device_id}
        logger.info(f"[视频号上传] POST到服务器 {url}，包含 {len(records)} 条记录，deviceId={device_id}")
        logger.debug(f"[视频号上传] 请求体示例（首条）: {records[0] if records else 'empty'}")

        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, proxies=get_proxies_dict(), timeout=30)
        logger.info(f"[视频号上传] 服务器响应状态码: {resp.status_code}")

        resp.raise_for_status()
        data = resp.json()
        logger.info(f"[视频号上传] 服务器响应: {data}")

        if isinstance(data, dict) and data.get("success") is False:
            logger.error(f"[视频号上传] 服务器返回失败: {data}")
            return False

        logger.info(f"[视频号上传] ✓ 服务器接受成功")
        return True
    except Exception as e:
        logger.error(f"[视频号上传] ✗ 服务器请求异常: {e}", exc_info=True)
        return False
