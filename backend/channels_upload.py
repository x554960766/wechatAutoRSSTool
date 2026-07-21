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
from backend.channels import (
    CHANNELS_FEEDS_FILE, CHANNELS_FAVORITES_FILE, FEEDS_LOCK,
    decrypt_channels_data, add_channels_history_item,
)

logger = logging.getLogger(__name__)

CHANNELS_UPLOAD_LOG_FILE = DATA_DIR / "channels_upload_log.jsonl"

_upload_lock = threading.Lock()
_upload_running = False

# 下载+COS+POST服务器 统一批大小:每批≤5,走完「下载→COS→推服务器」再下一批。
# 服务器批次跟随 COS 批(而非攒到 30 再推),数据量小、每批容错独立、失败面更小。
UPLOAD_BATCH_SIZE = 5
# 单条累计失败达此次数 → 放弃:去掉 needs_upload、标 upload_failed,不再无限重试。
# 视频号 CDN URL 带时效 token,过期后每轮都白下载,必须有放弃阈值。
MAX_UPLOAD_ATTEMPTS = 5

# 缓存上一次成功获取的临时凭证,API 请求失败时用作兜底
_last_cos_token: dict | None = None


def _fetch_cos_token(token_api_url: str) -> dict:
    """调用远程接口获取 COS 临时凭证(STS),失败时回退到上次缓存的凭证。

    返回 dict 包含: secret_id, secret_key, token, region, bucket, prefix
    """
    global _last_cos_token

    try:
        logger.info(f"[视频号上传] 请求临时凭证: {token_api_url}")
        resp = requests.get(token_api_url, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        logger.info(f"[视频号上传] 临时凭证接口响应: {json.dumps(body, ensure_ascii=False)[:500]}")
        # 兼容接口格式: 直接返回 data 或嵌套在 data 字段中
        data = body.get("data", body) if isinstance(body, dict) else body
        token_info = {
            "secret_id": data["tmpSecretId"],
            "secret_key": data["tmpSecretKey"],
            "token": data["sessionToken"],
            "region": data["region"],
            "bucket": data["bucket"],
            "prefix": "channels/",
        }
        _last_cos_token = token_info
        logger.info(f"[视频号上传] 获取临时凭证成功, region={token_info['region']}, bucket={token_info['bucket']}")
        return token_info
    except Exception as e:
        logger.warning(f"[视频号上传] 获取临时凭证失败: {e}", exc_info=True)
        if _last_cos_token:
            logger.info("[视频号上传] 使用上次缓存的临时凭证")
            return _last_cos_token
        raise RuntimeError(f"获取COS临时凭证失败且无缓存可用: {e}") from e


def _update_feed_item(username, feed_id, changes, pop_keys=()):
    """在 FEEDS_LOCK 内 load → 按 (username, feed_id) 定点改 → save。

    上传流程不再长时间持有 feeds_db 整体快照回写(会覆盖并发采集的写入),
    而是每次状态变更都基于磁盘最新内容做定点更新,规避跨线程读改写竞态。
    """
    with FEEDS_LOCK:
        feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
        items = feeds_db.get(username) or []
        for it in items:
            if str(it.get("id", "")) == str(feed_id):
                it.update(changes)
                for k in pop_keys:
                    it.pop(k, None)
                save_json(CHANNELS_FEEDS_FILE, feeds_db)
                return


def _bump_attempt(snap, err):
    """失败计数 +1;达 MAX_UPLOAD_ATTEMPTS 则放弃(去 needs_upload、标 upload_failed)。"""
    attempts = snap.get("attempts", 0) + 1
    snap["attempts"] = attempts
    if attempts >= MAX_UPLOAD_ATTEMPTS:
        _update_feed_item(snap["username"], snap["feed_id"],
                          {"upload_attempts": attempts, "upload_failed": True,
                           "last_error": str(err)[:200]},
                          pop_keys=("needs_upload",))
        logger.warning(f"[视频号上传] feedId={snap['feed_id']} 连续失败 {attempts} 次,放弃重试")
    else:
        _update_feed_item(snap["username"], snap["feed_id"],
                          {"upload_attempts": attempts, "last_error": str(err)[:200]})


def _log_event(event: dict):
    """追加一行审计日志到 channels_upload_log.jsonl，供事后确认整个上传流程"""
    event["ts"] = int(time.time() * 1000)
    try:
        with open(CHANNELS_UPLOAD_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[视频号上传] 写审计日志失败: {e}")

def process_pending_uploads():
    """扫描未上传项 → 每批≤5:下载+解密+传COS,本批COS成功的立即POST服务器,再下一批。"""
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

        token_api_url = (settings.get("cos_token_api_url") or "").strip()
        if not token_api_url:
            logger.error("[视频号上传] 错误：未配置 cos_token_api_url")
            return {"error": "cos_token_api_url not configured"}

        try:
            cos_cfg = _fetch_cos_token(token_api_url)
        except RuntimeError as e:
            logger.error(f"[视频号上传] 错误：{e}")
            return {"error": str(e)}

        device_id = str(settings.get("channels_device_id") if settings.get("channels_device_id") is not None else "视频号_caiji2").strip() or "视频号_caiji2"
        logger.info(f"[视频号上传] 配置加载完成 - 目标服务器: {server_url}, 设备ID: {device_id}, COS区域: {cos_cfg['region']}")

        # ---- 锁内快照本次待上传项:拷贝最小字段,不再长时间持有共享引用整体回写 ----
        with FEEDS_LOCK:
            feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
            favs = load_json(CHANNELS_FAVORITES_FILE, [])
            nick_map = {f.get("username"): f.get("nickname") for f in favs if isinstance(f, dict) and f.get("username")}
            pending = []
            for username, items in feeds_db.items():
                for item in items:
                    # 仅处理本次新同步(needs_upload)、未成功、未放弃且有链接的作品
                    if (item.get("needs_upload") and not item.get("uploaded")
                            and not item.get("upload_failed") and item.get("video_url")):
                        pending.append({
                            "username": username,
                            "feed_id": str(item.get("id", "")),
                            "video_url": item.get("video_url_h265") or item.get("video_url_h264") or item.get("video_url"),
                            "decode_key": item.get("decode_key", ""),
                            "description": item.get("description", ""),
                            "createtime": item.get("createtime") or 0,
                            "cos_url": item.get("cos_url"),  # 上次已传成功则复用,重试不重复下载/传COS
                            "nickname": nick_map.get(username) or item.get("nickname") or username,
                            "comment_count": item.get("comment_count", 0),
                            "fav_count": item.get("fav_count", 0),
                            "forward_count": item.get("forward_count", 0),
                            "like_count": item.get("like_count", 0),
                            "attempts": int(item.get("upload_attempts", 0) or 0),
                        })

        if not pending:
            logger.info("[视频号上传] 跳过：无本次新同步的待上传内容")
            return {"skipped": True, "reason": "no_pending"}

        batches = [pending[i:i+UPLOAD_BATCH_SIZE] for i in range(0, len(pending), UPLOAD_BATCH_SIZE)]
        logger.info(f"[视频号上传] 发现 {len(pending)} 个待上传视频，分 {len(batches)} 批(每批≤{UPLOAD_BATCH_SIZE}:COS→服务器)")
        _log_event({"event": "start", "pending": len(pending)})

        total_success = 0
        cos_fail_total = 0

        for batch_idx, batch in enumerate(batches, 1):
            logger.info(f"[视频号上传] --- 第 {batch_idx}/{len(batches)} 批，{len(batch)} 个 ---")
            # 阶段1：本批并发下载+解密+传COS(单个失败跳过,不阻塞其余)
            cos_results = _batch_cos_upload(batch, cos_cfg)  # [(snap, cos_url|None, err|None)]

            server_snaps = []
            for snap, cos_url, err in cos_results:
                feed_id = snap["feed_id"]
                if cos_url:
                    snap["cos_url"] = cos_url
                    _update_feed_item(snap["username"], feed_id,
                                      {"cos_url": cos_url, "upload_size": snap.get("upload_size", 0)})
                    server_snaps.append(snap)
                else:
                    cos_fail_total += 1
                    _bump_attempt(snap, err)  # 失败计数,超阈值放弃
                # 每个视频(无论成败)都写下载历史,带云端上传标识/地址/失败原因
                try:
                    add_channels_history_item(
                        snap.get("description") or feed_id,
                        "视频(自动上传)",
                        cos_url or "",
                        snap.get("upload_size", 0),
                        feed_id=feed_id,
                        uploaded=bool(cos_url),
                        cos_url=cos_url,
                        upload_error=err,
                    )
                except Exception as eh:
                    logger.warning(f"[视频号上传] 写历史记录失败: {eh}")
                _log_event({"event": "item", "feedId": feed_id,
                            "title": (snap.get("description") or "")[:60],
                            "cos_ok": bool(cos_url), "cos_url": cos_url, "error": err})

            # 阶段2：本批 COS 成功的(≤5 条)立即 POST 服务器
            if not server_snaps:
                logger.warning(f"[视频号上传] 第 {batch_idx} 批无COS成功项，跳过POST")
                _log_event({"event": "server_post", "batch": batch_idx, "total_batches": len(batches),
                            "records": 0, "ok": None, "error": "no_cos_success"})
                continue

            records = [{
                "feedId": s["feed_id"],
                "description": s["description"],
                "nickName": s["nickname"],
                "url": s["cos_url"],
                "publishTime": int(s["createtime"] or 0),
                "insertTime": int(time.time()),
                "id": 0,
                "commentCount": s["comment_count"],
                "favCount": s["fav_count"],
                "forwardCount": s["forward_count"],
                "likeCount": s["like_count"],
            } for s in server_snaps]

            logger.info(f"[视频号上传] 服务器POST第 {batch_idx}/{len(batches)} 批，{len(records)} 条")
            ok, server_err = _post_to_server(server_url, records, device_id)
            _log_event({"event": "server_post", "batch": batch_idx, "total_batches": len(batches),
                        "records": len(records), "ok": ok, "error": server_err})
            if ok:
                for s in server_snaps:
                    _update_feed_item(s["username"], s["feed_id"],
                                      {"uploaded": True, "upload_time": int(time.time())},
                                      pop_keys=("needs_upload", "upload_attempts", "last_error"))
                    total_success += 1
                logger.info(f"[视频号上传] ✓ 第 {batch_idx} 批服务器接受成功，标记 {len(records)} 个为已上传")
            else:
                # 本批不标记 uploaded；cos_url 已缓存,失败计数 +1,超阈值放弃,否则下轮重试(不重复下载)
                for s in server_snaps:
                    _bump_attempt(s, f"服务器: {server_err}")
                logger.warning(f"[视频号上传] ✗ 第 {batch_idx} 批服务器POST失败，{len(records)} 个保留待下次重试")

        logger.info(f"[视频号上传] ========== 上传完成：处理 {len(pending)} 个，成功 {total_success} 个，COS失败 {cos_fail_total} 个 ==========")
        _log_event({"event": "done", "processed": len(pending), "uploaded": total_success,
                    "cos_fail": cos_fail_total})
        return {"success": True, "processed": len(pending), "uploaded": total_success}
    finally:
        with _upload_lock:
            _upload_running = False

def _batch_cos_upload(batch, cos_cfg):
    """本批并发：下载+解密+COS上传，返回 [(snap, cos_url_or_None, error_or_None)]"""
    results = []
    with ThreadPoolExecutor(max_workers=UPLOAD_BATCH_SIZE) as executor:
        futures = {executor.submit(_download_and_cos, snap, cos_cfg): snap for snap in batch}
        for future in as_completed(futures):
            snap = futures[future]
            feed_id = snap["feed_id"]
            try:
                cos_url = future.result()
                logger.info(f"[视频号上传] ✓ feedId={feed_id} COS上传成功: {cos_url}")
                results.append((snap, cos_url, None))
            except Exception as e:
                logger.error(f"[视频号上传] ✗ feedId={feed_id} COS上传失败: {e}")
                results.append((snap, None, str(e)))
    return results

def _download_and_cos(snap, cos_cfg):
    """下载CDN→解密(前128KB)→上传COS→返回公网URL。snap 为本地快照,可安全就地写 upload_size。"""
    feed_id = snap["feed_id"]

    if snap.get("cos_url"):
        logger.debug(f"[视频号上传] feedId={feed_id} 已有COS URL，跳过下载")
        return snap["cos_url"]

    video_url = snap.get("video_url")
    if not video_url:
        raise ValueError("No video_url")

    logger.debug(f"[视频号上传] feedId={feed_id} 开始下载: {video_url[:80]}...")
    resp = requests.get(video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    resp.raise_for_status()
    data = bytearray(resp.content)
    snap["upload_size"] = len(data)
    logger.debug(f"[视频号上传] feedId={feed_id} 下载完成，大小: {len(data)} bytes")

    decode_key = snap.get("decode_key", "")
    if decode_key:
        try:
            key_val = int(decode_key)
            if key_val > 0:  # 与本地下载一致:key<=0(未加密)不解密,避免 XOR 损坏前128KB
                decrypt_channels_data(data, key_val)
                logger.debug(f"[视频号上传] feedId={feed_id} 解密完成")
        except Exception as e:
            logger.warning(f"[视频号上传] feedId={feed_id} 解密失败: {e}")

    cos_config_kwargs = {
        "Region": cos_cfg["region"],
        "SecretId": cos_cfg["secret_id"],
        "SecretKey": cos_cfg["secret_key"],
    }
    if cos_cfg.get("token"):
        cos_config_kwargs["Token"] = cos_cfg["token"]
    config = CosConfig(**cos_config_kwargs)
    client = CosS3Client(config)

    filename = f"{feed_id or int(time.time())}.mp4"
    key = cos_cfg["prefix"] + filename

    logger.debug(f"[视频号上传] feedId={feed_id} 开始上传到COS: {key}")
    client.put_object(Bucket=cos_cfg["bucket"], Body=bytes(data), Key=key)

    cos_url = f"https://{cos_cfg['bucket']}.cos.{cos_cfg['region']}.myqcloud.com/{key}"

    return cos_url

def _post_to_server(url, records, device_id):
    """POST {data, deviceId} 到服务器，返回 (ok: bool, error: str|None)。

    error 尽量具体：区分超时/连接失败/HTTP状态码+响应体/业务失败，方便在上传日志里定位问题。
    """
    payload = {"data": records, "deviceId": device_id}
    logger.info(f"[视频号上传] POST到服务器 {url}，包含 {len(records)} 条记录，deviceId={device_id}")
    logger.debug(f"[视频号上传] 请求体示例（首条）: {records[0] if records else 'empty'}")

    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, proxies=get_proxies_dict(), timeout=30)
    except requests.exceptions.Timeout:
        logger.error(f"[视频号上传] ✗ 服务器请求超时(30秒): {url}")
        return False, f"请求超时(30秒未响应): {url}"
    except requests.exceptions.ConnectionError as e:
        logger.error(f"[视频号上传] ✗ 无法连接服务器: {e}")
        return False, f"无法连接服务器(地址错误/服务未启动/网络不通): {str(e)[:150]}"
    except Exception as e:
        logger.error(f"[视频号上传] ✗ 服务器请求异常: {e}", exc_info=True)
        return False, f"{type(e).__name__}: {str(e)[:180]}"

    body_snippet = (resp.text or "")[:300].strip()
    logger.info(f"[视频号上传] 服务器响应状态码: {resp.status_code}, 响应体: {body_snippet}")

    if resp.status_code != 200:
        logger.error(f"[视频号上传] ✗ 服务器返回HTTP {resp.status_code}: {body_snippet}")
        return False, f"HTTP {resp.status_code} {resp.reason or ''}，响应体: {body_snippet or '(空)'}"

    try:
        data = resp.json()
    except Exception:
        logger.error(f"[视频号上传] ✗ 服务器响应不是合法JSON: {body_snippet}")
        return False, f"响应不是合法JSON(HTTP 200)，响应体: {body_snippet or '(空)'}"

    logger.info(f"[视频号上传] 服务器响应: {data}")
    if isinstance(data, dict) and data.get("success") is False:
        # 优先提取常见的业务错误字段，其次整体返回内容
        msg = data.get("message") or data.get("msg") or data.get("error") or json.dumps(data, ensure_ascii=False)
        logger.error(f"[视频号上传] 服务器返回业务失败: {data}")
        return False, f"服务器返回失败: {str(msg)[:250]}"

    logger.info(f"[视频号上传] ✓ 服务器接受成功")
    return True, None
