"""
小红书自动采集调度模块
后台定时轮询已关注博主的最新作品，支持频率抖动、风控自动冷却与时间窗口控制
"""

import time
import math
import random
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

from backend.config import DATA_DIR, load_json, save_json, get_settings
from backend.xiaohongshu import (
    XhsClient, clean_filename, lognormal_sleep,
    write_note_text, write_note_html,
    upload_xhs_video_to_cos, write_note_data_json, post_xhs_note_to_server,
    XHS_DIR, XHS_ACCOUNTS_FILE, XHS_HISTORY_FILE
)

logger = logging.getLogger(__name__)

XHS_COLLECT_LOG_FILE = DATA_DIR / "xhs_collect_log.json"
MAX_COLLECT_LOG = 100


class XhsAutoCollector:
    """小红书自动采集调度器"""

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._abort_all_event = threading.Event()  # 手动停止全部采集任务信号
        self._cancelled_users: set[str] = set()    # 手动取消单博主采集集合
        self._lock = threading.Lock()           # 保护 accounts 读写
        self._history_lock = threading.Lock()   # 保护 history 读写
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xhs-collector")
        self._fetching: set[str] = set()        # 正在采集中的 user_id
        self._fetching_lock = threading.Lock()
        self._last_blogger_finish_time: float = 0.0  # 上一位博主采集完成的时间戳
        self._cooldown_until: float = 0.0       # 风控触发后的冷却截止时间戳
        self._last_error: str | None = None
        self._is_running = False

    def _sleep(self, seconds: float, user_id: str = None) -> bool:
        """可中断的休眠，如果触发停止全部或取消该博主，立即返回 False 退出休眠"""
        end_time = time.time() + max(0.0, float(seconds))
        while time.time() < end_time:
            if self._stop_event.is_set() or self._abort_all_event.is_set():
                return False
            if user_id and user_id in self._cancelled_users:
                return False
            time.sleep(min(0.2, max(0.01, end_time - time.time())))
        return True

    # ── 状态与时间计算 ────────────────────────────────────────

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value, default: float = 0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _normalize_interval_minutes(cls, value) -> int:
        return max(10, cls._safe_int(value, 120))

    @classmethod
    def _get_interval_range_minutes(cls, interval_minutes: int) -> tuple[int, int]:
        interval = cls._normalize_interval_minutes(interval_minutes)
        jitter = max(2, round(interval * 0.2))  # ±20% 抖动，防止固定频率特征
        return max(5, interval - jitter), interval + jitter

    def _schedule_next_fetch(self, account: dict, start_time: float | None = None) -> float:
        interval = self._safe_int(account.get("collect_interval_minutes", 120))
        min_m, max_m = self._get_interval_range_minutes(interval)
        next_time = (start_time or time.time()) + random.randint(min_m * 60, max_m * 60)
        account["next_collect_time"] = next_time
        return next_time

    def _ensure_next_fetch_time(self, account: dict, now: float) -> bool:
        if self._safe_float(account.get("next_collect_time"), 0) > 0:
            return False
        last_fetch = self._safe_float(account.get("last_collect_time"), 0)
        self._schedule_next_fetch(account, last_fetch or now)
        return True

    def _get_fetch_window_hours(self) -> tuple[int, int]:
        settings = get_settings()
        start_hour = max(0, min(23, self._safe_int(settings.get("xhs_collect_window_start_hour", 8), 8)))
        end_hour = max(0, min(24, self._safe_int(settings.get("xhs_collect_window_end_hour", 24), 24)))
        return start_hour, end_hour

    def is_in_fetch_window(self, now=None) -> bool:
        current = now or datetime.now()
        start_hour, end_hour = self._get_fetch_window_hours()
        current_hour = current.hour
        if start_hour <= end_hour:
            return start_hour <= current_hour < end_hour
        return current_hour >= start_hour or current_hour < end_hour

    def is_in_cooldown(self) -> tuple[bool, int]:
        now = time.time()
        if now < self._cooldown_until:
            remaining = int(self._cooldown_until - now)
            return True, remaining
        return False, 0

    def trigger_cooldown(self, minutes: int = 15, reason: str = "触发小红书风控限制"):
        self._cooldown_until = time.time() + minutes * 60
        self._last_error = f"{reason}，进入全局冷却 {minutes} 分钟 (至 {datetime.fromtimestamp(self._cooldown_until).strftime('%H:%M:%S')})"
        logger.warning("小红书自动采集: %s", self._last_error)

    # ── 已采集作品历史精准排重 ────────────────────────────────

    def _get_collected_note_ids(self) -> set[str]:
        """读取历史记录并结合本地磁盘目录，返回已成功采集的 note_id 集合，杜绝重复采集"""
        import json
        collected = set()
        with self._history_lock:
            history = load_json(XHS_HISTORY_FILE, [])
            for item in history:
                if isinstance(item, dict) and item.get("success") and item.get("note_id"):
                    collected.add(str(item["note_id"]).strip())

        # 结合本地已下载目录中的 data.json 进行双重排重，避免历史文件缺失导致重复下载
        if XHS_DIR.exists():
            try:
                for data_file in XHS_DIR.glob("*/*/data.json"):
                    try:
                        d = json.loads(data_file.read_text(encoding="utf-8", errors="replace"))
                        nid = d.get("note_id")
                        if nid:
                            collected.add(str(nid).strip())
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("扫描本地已采集作品目录异常: %s", e)

        return collected

    def _retry_unuploaded_notes_for_account(self, nickname: str, user_id: str, detail_records: list) -> int:
        """检查该博主以往下载成功但尚未成功推送到服务器的笔记，在本次采集时重新重试推送"""
        settings = get_settings()
        upload_enabled = settings.get("xhs_upload_enabled")
        if upload_enabled is None:
            upload_enabled = settings.get("rss_upload_enabled", False)
        if not upload_enabled:
            return 0

        retry_success_count = 0
        with self._history_lock:
            history = load_json(XHS_HISTORY_FILE, [])
            changed = False

            # 当前已在本次处理列表中的 note_id，避免重复推送
            current_note_ids = {r.get("note_id") for r in detail_records if r.get("note_id")}

            for item in history:
                nid = item.get("note_id")
                if not nid or nid in current_note_ids:
                    continue

                # 匹配属于该博主的记录
                author = item.get("author") or ""
                if author != nickname and author != user_id:
                    continue

                # 仅重试下载成功但未推送成功的记录 (uploaded 不是 True)
                if not item.get("success") or item.get("uploaded"):
                    continue

                path_str = item.get("path") or ""
                note_dir = Path(path_str) if path_str else None
                data_json = None

                if note_dir and note_dir.exists():
                    data_json_path = note_dir / "data.json"
                    if data_json_path.exists():
                        try:
                            data_json = json.loads(data_json_path.read_text(encoding="utf-8"))
                        except Exception:
                            data_json = None

                if not data_json:
                    data_json = {
                        "source": nickname,
                        "title": item.get("title") or nid,
                        "url": f"https://www.xiaohongshu.com/explore/{nid}",
                        "cover_url": "",
                        "publish_time": datetime.fromtimestamp(item.get("time", time.time())).strftime("%Y-%m-%d %H:%M:%S"),
                        "content": item.get("desc") or item.get("title") or "",
                        "desc": item.get("desc") or "",
                        "type": item.get("type", "图文"),
                        "tags": item.get("tags", []),
                        "images": [],
                        "video_url": item.get("cos_url") or "",
                        "cos_url": item.get("cos_url") or "",
                        "note_id": nid,
                    }

                uploaded_ok, upload_err = post_xhs_note_to_server(data_json)
                if uploaded_ok:
                    item["uploaded"] = True
                    item["upload_error"] = None
                    item["upload_time"] = time.time()
                    changed = True
                    retry_success_count += 1
                    logger.info("小红书自动采集 [%s] 历史未推送作品重试成功: %s", nickname, item.get("title"))

                    detail_records.append({
                        "note_id": nid,
                        "title": f"[重试补发] {item.get('title') or nid}",
                        "type": item.get("type", "图文"),
                        "download_success": True,
                        "cos_url": item.get("cos_url", ""),
                        "uploaded": True,
                        "upload_error": None,
                        "is_retry": True,
                        "error": None,
                        "path": path_str,
                        "time": time.time()
                    })
                else:
                    item["uploaded"] = False
                    item["upload_error"] = upload_err
                    changed = True
                    logger.warning("小红书自动采集 [%s] 历史未推送作品重试失败: %s - %s", nickname, item.get("title"), upload_err)

                    detail_records.append({
                        "note_id": nid,
                        "title": f"[重试失败] {item.get('title') or nid}",
                        "type": item.get("type", "图文"),
                        "download_success": True,
                        "cos_url": item.get("cos_url", ""),
                        "uploaded": False,
                        "upload_error": upload_err,
                        "is_retry": True,
                        "error": None,
                        "path": path_str,
                        "time": time.time()
                    })

            if changed:
                save_json(XHS_HISTORY_FILE, history)

        return retry_success_count

    # ── 账号配置管理 ──────────────────────────────────────────

    def get_accounts(self) -> list:
        return load_json(XHS_ACCOUNTS_FILE, [])

    def update_account_config(self, user_id: str, auto_collect: bool | None = None, interval_minutes: int | None = None) -> dict | None:
        with self._lock:
            accounts = self.get_accounts()
            target = None
            for acc in accounts:
                if acc.get("user_id") == user_id:
                    if auto_collect is not None:
                        acc["auto_collect_enabled"] = bool(auto_collect)
                    if interval_minutes is not None:
                        acc["collect_interval_minutes"] = self._normalize_interval_minutes(interval_minutes)
                        self._schedule_next_fetch(acc)
                    elif auto_collect and not self._safe_float(acc.get("next_collect_time"), 0):
                        self._schedule_next_fetch(acc)
                    target = acc
                    break
            if target:
                save_json(XHS_ACCOUNTS_FILE, accounts)
            return target

    # ── 采集审计日志 ──────────────────────────────────────────

    def _append_collect_log(self, record: dict):
        log = load_json(XHS_COLLECT_LOG_FILE, [])
        if not isinstance(log, list):
            log = []
        log.insert(0, record)
        save_json(XHS_COLLECT_LOG_FILE, log[:MAX_COLLECT_LOG])

    def get_collect_logs(self, limit: int = 50) -> list:
        log = load_json(XHS_COLLECT_LOG_FILE, [])
        if not isinstance(log, list):
            return []
        return log[:max(1, limit)]

    # ── 单博主采集执行 ────────────────────────────────────────

    def submit_fetch(self, account: dict) -> bool:
        user_id = account.get("user_id", "")
        if not user_id:
            return False
        with self._fetching_lock:
            if user_id in self._fetching:
                logger.debug("小红书采集跳过 [%s]: 已在队列中", account.get("nickname", user_id))
                return False
            self._fetching.add(user_id)
        self._executor.submit(self._fetch_wrapper, account)
        return True

    def _fetch_wrapper(self, account: dict):
        user_id = account.get("user_id", "")
        nickname = account.get("nickname", user_id)
        try:
            if self._stop_event.is_set() or self._abort_all_event.is_set() or user_id in self._cancelled_users:
                logger.info("小红书采集跳过已取消的博主任务: %s", nickname)
                return

            # 跨博主切换防风控：确保博主之间有 15~35 秒的人性化阅读/导航停顿
            if self._last_blogger_finish_time > 0:
                elapsed = time.time() - self._last_blogger_finish_time
                target_gap = random.uniform(15.0, 30.0)
                wait_needed = target_gap - elapsed
                if wait_needed > 0.5:
                    logger.info("小红书多博主切换安全缓冲等待: %.1f 秒...", wait_needed)
                    if not self._sleep(wait_needed, user_id=user_id):
                        logger.info("小红书博主切换等待被取消: %s", nickname)
                        return

            if not self._stop_event.is_set() and not self._abort_all_event.is_set() and user_id not in self._cancelled_users:
                self._fetch_for_account(account)
        except Exception as e:
            logger.error("小红书采集异常 [%s]: %s", nickname, e)
        finally:
            self._last_blogger_finish_time = time.time()
            with self._fetching_lock:
                self._fetching.discard(user_id)

    def _fetch_for_account(self, account: dict):
        user_id = account.get("user_id", "")
        nickname = account.get("nickname", user_id)
        url = account.get("url") or f"https://www.xiaohongshu.com/user/profile/{user_id}"

        if self._stop_event.is_set() or self._abort_all_event.is_set() or user_id in self._cancelled_users:
            logger.info("小红书博主采集已终止 [%s]", nickname)
            return

        # 检查风控冷却
        in_cd, remaining = self.is_in_cooldown()
        if in_cd:
            logger.warning("小红书采集跳过 [%s]: 处于风控冷却中，剩余 %d 秒", nickname, remaining)
            return

        settings = get_settings()
        max_per_account = self._safe_int(settings.get("xhs_collect_max_per_account", 20), 20)
        cooldown_min = self._safe_int(settings.get("xhs_collect_cooldown_minutes", 15), 15)

        client = XhsClient()
        last_error = None
        new_downloaded = 0
        skipped_count = 0
        failed_count = 0
        detail_records = []

        logger.info("小红书自动采集开始: [%s] (ID: %s)", nickname, user_id)

        try:
            # 1. 获取博主最新作品列表
            profile_data = client.get_user_profile(url)
            notes_list = profile_data.get("notes", [])

            if not notes_list:
                warning = profile_data.get("warning")
                if warning and "降级" in warning:
                    raise ValueError(f"无法获取有效笔记列表（{warning}）")
                logger.info("小红书博主 [%s] 暂无笔记", nickname)

            # 2. 读取下载历史与本地磁盘，获取全量已采集作品 ID
            downloaded_ids = self._get_collected_note_ids()
            last_note_time = self._safe_float(account.get("last_note_time"), 0)

            # 3. 智能增量筛选与时间线容错截断机制：
            # 解决单点假命中导致的漏采截断问题（例如中间某篇笔记此前被单条下载过或漏采，导致后面的新笔记被一刀切截断）。
            # 策略：
            # 1) 置顶笔记 (is_sticky)：已下载则跳过，不计入连续已采计数器；
            # 2) 连续命中阈值 (CONSECUTIVE_BREAK_COUNT = 3)：
            #    当在时间线中【连续遇到 3 篇】非置顶的已下载作品时，确认真正接轨历史老数据基线，才执行 break 终止扫描。
            #    若中间有单篇由于之前漏采/失败的笔记，可被继续检索并加回 pending_notes，不遗漏数据；
            # 3) 若作品自带有效 timestamp 且比上次采集到的最新作品还新 (note_ts > last_note_time)，确认为新增作品，安全收录。
            pending_notes = []
            consecutive_seen = 0
            max_new_ts = last_note_time
            CONSECUTIVE_BREAK_COUNT = 3

            for note in notes_list:
                nid = str(note.get("note_id", "")).strip()
                if not nid:
                    continue

                is_sticky = bool(note.get("sticky"))
                note_ts = self._safe_float(note.get("timestamp"), 0)

                if nid in downloaded_ids:
                    skipped_count += 1
                    if not is_sticky:
                        consecutive_seen += 1
                        # 连续命中 3 篇已采集的非置顶作品，确认已深达稳定历史区间，安全终止
                        if consecutive_seen >= CONSECUTIVE_BREAK_COUNT:
                            logger.info(
                                "小红书博主 [%s] 连续遇到 %d 篇已采集作品，确认接轨历史时间线，安全截断",
                                nickname, consecutive_seen
                            )
                            break
                else:
                    consecutive_seen = 0
                    pending_notes.append(note)
                    if not is_sticky and note_ts > max_new_ts:
                        max_new_ts = note_ts

            # 限制单次采集数量，避免突发大批量触发风控
            if max_per_account > 0:
                pending_notes = pending_notes[:max_per_account]

            if not pending_notes:
                logger.info("小红书博主 [%s]: 本次无新增未采集作品 (跳过 %d 篇历史已下载作品)", nickname, skipped_count)
            else:
                logger.info("小红书博主 [%s]: 发现 %d 篇未采集新作品待下载 (跳过 %d 篇历史作品)", nickname, len(pending_notes), skipped_count)

            # 3. 逐条解析并下载新笔记（仿真实人类浏览节奏）
            for idx, note_item in enumerate(pending_notes):
                if self._stop_event.is_set() or self._abort_all_event.is_set() or user_id in self._cancelled_users:
                    logger.info("小红书博主采集在第 %d 篇前被手动停止 [%s]", idx + 1, nickname)
                    break

                # 检查是否在采集途中触发了全局冷却
                in_cd_mid, _ = self.is_in_cooldown()
                if in_cd_mid:
                    logger.warning("小红书采集暂停 [%s]: 中途触发风控冷却", nickname)
                    break

                nid = note_item.get("note_id")
                token = note_item.get("xsec_token", "")
                note_url = f"https://www.xiaohongshu.com/explore/{nid}"
                if token:
                    note_url += f"?xsec_token={token}&xsec_source=pc_user"

                # 模拟真实人类进入单篇笔记浏览/阅读节奏延迟 (6~14 秒对数正态分布)
                view_delay = random.lognormvariate(math.log(8.0), 0.45)
                view_delay = max(4.0, min(16.0, view_delay))
                if not self._sleep(view_delay, user_id=user_id):
                    logger.info("小红书博主采集在阅读等待中被手动停止 [%s]", nickname)
                    break

                try:
                    detail = client.get_note_detail(note_url)
                    author_nick = clean_filename(detail["author"]["nickname"] or nickname)
                    pub_date = detail["publish_time"].split()[0]
                    clean_title = clean_filename(detail["title"]) or detail["note_id"]

                    note_dir = XHS_DIR / author_nick / f"{pub_date}_{clean_title}"
                    note_dir.mkdir(parents=True, exist_ok=True)

                    # 写入文案
                    text_size = write_note_text(note_dir, detail)
                    total_size = text_size
                    success = True
                    error_msg = None
                    video_file_name = None
                    image_items = []

                    img_fmt = settings.get("xhs_image_format", "png")
                    img_ext = img_fmt if img_fmt != "auto" else "jpg"
                    if img_ext == "jpeg":
                        img_ext = "jpg"

                    # 视频或图文下载
                    if detail["type"] == "视频" or detail.get("video") or detail.get("video_candidates"):
                        candidates = detail.get("video_candidates") or ([detail["video"]] if detail.get("video") else [])
                        if candidates:
                            save_file = note_dir / f"{clean_title}.mp4"
                            video_ok = False
                            last_v_err = None
                            for v_url in candidates:
                                try:
                                    size = client.download_file(v_url, save_file)
                                    if size > 0:
                                        total_size += size
                                        video_file_name = save_file.name
                                        video_ok = True
                                        break
                                except Exception as ve:
                                    last_v_err = ve
                                    continue
                            if video_ok:
                                # 视频下载完成后稍微停顿 1.2~2.5 秒
                                self._sleep(random.uniform(1.2, 2.5), user_id=user_id)
                                # 尝试将视频上传到腾讯云 COS (支持失败自动重试3次)
                                cos_url, cos_err = upload_xhs_video_to_cos(save_file, detail["note_id"], return_error=True)
                                if cos_url:
                                    detail["cos_url"] = cos_url
                                    detail["video_url"] = cos_url
                                elif cos_err:
                                    detail["cos_error"] = cos_err
                            else:
                                success = False
                                error_msg = f"视频下载失败: {last_v_err or '所有备选源均不可用'}"
                        else:
                            success = False
                            error_msg = "未找到可用视频链接"
                    else:
                        for img_idx, img_url in enumerate(detail.get("images", [])):
                            if self._stop_event.is_set() or self._abort_all_event.is_set() or user_id in self._cancelled_users:
                                success = False
                                error_msg = "采集被手动终止"
                                break

                            save_file = note_dir / f"{img_idx + 1:02d}.{img_ext}"
                            try:
                                size = client.download_file(img_url, save_file)
                                total_size += size
                            except Exception as ie:
                                success = False
                                error_msg = f"图片 {img_idx + 1} 下载失败: {ie}"
                                break

                            item = {"img": save_file.name, "live": None}
                            live_url = detail["lives"][img_idx] if img_idx < len(detail.get("lives", [])) else None
                            if live_url:
                                live_file = note_dir / f"{img_idx + 1:02d}.mp4"
                                try:
                                    self._sleep(random.uniform(0.5, 1.2), user_id=user_id)
                                    size = client.download_file(live_url, live_file)
                                    total_size += size
                                    item["live"] = live_file.name
                                except Exception:
                                    pass
                            image_items.append(item)
                            # 模拟人类翻看多图相册间隔 (0.8 ~ 1.8 秒)
                            self._sleep(random.uniform(0.8, 1.8), user_id=user_id)

                    # 写入离线 HTML
                    total_size += write_note_html(note_dir, detail, video_file_name, image_items)

                    # 生成与公众号对齐的 data.json
                    data_json = write_note_data_json(note_dir, detail, cos_url=detail.get("cos_url"), video_file=video_file_name, image_items=image_items)

                    # 如果开启了服务器上传，像公众号一样推送到远端服务器
                    uploaded_ok = False
                    upload_err = None
                    if success and not (self._stop_event.is_set() or self._abort_all_event.is_set() or user_id in self._cancelled_users):
                        uploaded_ok, upload_err = post_xhs_note_to_server(data_json)

                    # 自动采集且推送到服务器成功后：自动删除本地文件与目录，释放磁盘与空间，只保留采集与上传记录
                    local_cleaned = False
                    if success and uploaded_ok and note_dir and note_dir.exists():
                        try:
                            import shutil
                            shutil.rmtree(note_dir, ignore_errors=True)
                            local_cleaned = True
                            logger.info("小红书自动采集 [%s] 作品已成功上传服务器，自动清理本地文件: %s", nickname, note_dir.name)
                            # 若博主根目录已空则一并清理
                            parent_dir = note_dir.parent
                            if parent_dir.exists() and not any(parent_dir.iterdir()):
                                parent_dir.rmdir()
                        except Exception as rm_e:
                            logger.warning("清理本地笔记文件失败: %s", rm_e)

                    # 记录单篇详情到采集日志列表中
                    detail_records.append({
                        "note_id": detail["note_id"],
                        "title": detail["title"] or clean_title,
                        "type": detail["type"],
                        "download_success": success,
                        "cos_url": detail.get("cos_url", ""),
                        "cos_error": detail.get("cos_error", None),
                        "uploaded": uploaded_ok if success else False,
                        "upload_error": upload_err if (success and not uploaded_ok) else None,
                        "error": error_msg,
                        "path": str(note_dir) if (success and not local_cleaned) else "",
                        "local_cleaned": local_cleaned,
                        "time": time.time()
                    })

                    # 写入历史记录
                    with self._history_lock:
                        history = load_json(XHS_HISTORY_FILE, [])
                        history.append({
                            "title": detail["title"] or detail["note_id"],
                            "note_id": detail["note_id"],
                            "type": detail["type"],
                            "author": detail["author"]["nickname"] or nickname,
                            "path": str(note_dir) if not local_cleaned else "",
                            "size": total_size,
                            "time": time.time(),
                            "success": success,
                            "error": error_msg,
                            "trigger": "auto_collect",
                            "cos_url": detail.get("cos_url", ""),
                            "cos_error": detail.get("cos_error", None),
                            "uploaded": uploaded_ok if success else False,
                            "upload_error": upload_err if (success and not uploaded_ok) else None,
                            "upload_time": time.time() if (success and uploaded_ok) else None,
                            "local_cleaned": local_cleaned,
                        })
                        save_json(XHS_HISTORY_FILE, history)

                    if success:
                        new_downloaded += 1
                        if uploaded_ok:
                            logger.info("小红书自动采集 [%s] 成功下载并推送服务器 (本地文件已释放): %s (COS: %s)", nickname, clean_title, detail.get("cos_url") or "无")
                        elif upload_err:
                            logger.warning("小红书自动采集 [%s] 下载成功但推送服务器失败: %s - %s", nickname, clean_title, upload_err)
                        else:
                            logger.info("小红书自动采集 [%s] 成功下载: %s", nickname, clean_title)
                    else:
                        failed_count += 1
                        logger.warning("小红书自动采集 [%s] 失败: %s - %s", nickname, clean_title, error_msg)

                    # 单博主批量阅读间歇：每连续下载 3 篇作品，插入 20~35 秒模拟人类阅读休息
                    if (idx + 1) % 3 == 0 and (idx + 1) < len(pending_notes):
                        rest_time = random.uniform(20.0, 35.0)
                        logger.info("小红书博主 [%s] 已连续下载 %d 篇，模拟人类阅读休整 %.1f 秒...", nickname, idx + 1, rest_time)
                        if not self._sleep(rest_time, user_id=user_id):
                            logger.info("小红书博主休整等待被手动停止: %s", nickname)
                            break

                except Exception as ne:
                    err_text = str(ne)
                    failed_count += 1
                    logger.warning("小红书自动采集单篇异常 [%s - %s]: %s", nickname, nid, err_text)
                    detail_records.append({
                        "note_id": nid,
                        "title": f"作品_{nid}",
                        "type": "未知",
                        "download_success": False,
                        "cos_url": "",
                        "uploaded": False,
                        "upload_error": None,
                        "error": err_text,
                        "path": "",
                        "time": time.time()
                    })
                    if "461" in err_text or "406" in err_text or "风控" in err_text:
                        self.trigger_cooldown(minutes=cooldown_min, reason=f"采集作品触发风控 ({err_text})")
                        last_error = "触发风控限制"
                        break

            # 4. 无论本次有无新下载作品，均自动检查并重试推送该博主历史尚未推送成功的作品
            if not (self._stop_event.is_set() or self._abort_all_event.is_set() or user_id in self._cancelled_users):
                try:
                    self._retry_unuploaded_notes_for_account(nickname, user_id, detail_records)
                except Exception as re_err:
                    logger.warning("小红书自动采集 [%s] 历史未推送作品重试异常: %s", nickname, re_err)

        except Exception as e:
            err_str = str(e)
            last_error = err_str
            logger.warning("小红书博主采集失败 [%s]: %s", nickname, err_str)
            if "461" in err_str or "406" in err_str or "风控" in err_str:
                self.trigger_cooldown(minutes=cooldown_min, reason=f"获取博主主页触发风控 ({err_str})")

        now = time.time()
        # 4. 更新博主自身采集状态
        with self._lock:
            accounts = self.get_accounts()
            for acc in accounts:
                if acc.get("user_id") == user_id:
                    acc["last_collect_time"] = now
                    acc["last_collect_count"] = new_downloaded
                    acc["last_collect_error"] = last_error
                    if max_new_ts > self._safe_float(acc.get("last_note_time"), 0):
                        acc["last_note_time"] = max_new_ts
                    self._schedule_next_fetch(acc, now)
                    break
            save_json(XHS_ACCOUNTS_FILE, accounts)

        uploaded_count = sum(1 for it in detail_records if it.get("uploaded"))
        upload_failed_count = sum(1 for it in detail_records if it.get("download_success") and it.get("upload_error"))
        upload_skipped_count = sum(1 for it in detail_records if it.get("download_success") and not it.get("uploaded") and not it.get("upload_error"))

        # 5. 记录采集审计日志
        self._append_collect_log({
            "time": now,
            "user_id": user_id,
            "nickname": nickname,
            "new_count": new_downloaded,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "uploaded_count": uploaded_count,
            "upload_failed_count": upload_failed_count,
            "upload_skipped_count": upload_skipped_count,
            "success": last_error is None,
            "error": last_error,
            "items": detail_records
        })

        if uploaded_count > 0 or upload_failed_count > 0:
            logger.info("小红书自动采集完成 [%s]: 新下载 %d 篇 (推送服务器: 成功 %d 篇, 失败 %d 篇), 跳过 %d 篇, 失败 %d 篇",
                        nickname, new_downloaded, uploaded_count, upload_failed_count, skipped_count, failed_count)
        else:
            logger.info("小红书自动采集完成 [%s]: 新下载 %d 篇, 跳过 %d 篇, 失败 %d 篇", nickname, new_downloaded, skipped_count, failed_count)

    # ── 调度主循环 ──────────────────────────────────────────

    def _tick(self):
        settings = get_settings()
        if not settings.get("xhs_auto_collect_enabled", False):
            return

        if not self.is_in_fetch_window():
            return

        in_cd, _ = self.is_in_cooldown()
        if in_cd:
            return

        now = time.time()
        due_accounts = []

        with self._lock:
            accounts = self.get_accounts()
            changed = False

            for acc in accounts:
                if not acc.get("auto_collect_enabled"):
                    continue

                if self._ensure_next_fetch_time(acc, now):
                    changed = True

                next_time = self._safe_float(acc.get("next_collect_time"), 0)
                if now >= next_time:
                    due_accounts.append(dict(acc))

            if changed:
                save_json(XHS_ACCOUNTS_FILE, accounts)

        # 按最久未采集时间升序排序，实现多博主轮流公平采集
        due_accounts.sort(key=lambda a: self._safe_float(a.get("last_collect_time"), 0))

        for acc in due_accounts:
            self.submit_fetch(acc)

    def _run_loop(self):
        logger.info("小红书自动采集调度器已启动")
        self._is_running = True
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("小红书调度器 tick 异常: %s", e)
            self._stop_event.wait(10)
        self._is_running = False
        logger.info("小红书自动采集调度器已停止")

    # ── 控制接口 ────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="xhs-collector")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._abort_all_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._executor.shutdown(wait=False)

    def stop_all_collect(self) -> dict:
        """手动停止当前所有正在执行或排队中的小红书采集任务"""
        self._abort_all_event.set()
        with self._fetching_lock:
            cancelled_count = len(self._fetching)
            self._fetching.clear()
        logger.info("已手动请求停止所有小红书采集任务 (清理了 %d 个运行中任务)", cancelled_count)
        return {"message": "已成功请求停止所有采集任务", "stopped": True, "cancelled_count": cancelled_count}

    def stop_account_collect(self, user_id: str) -> dict:
        """手动停止指定博主的采集任务"""
        if not user_id:
            return {"message": "博主 ID 不能为空", "stopped": False}
        self._cancelled_users.add(user_id)
        with self._fetching_lock:
            was_running = user_id in self._fetching
            self._fetching.discard(user_id)
        logger.info("已手动请求停止小红书博主采集任务: %s", user_id)
        return {"message": "已成功请求停止该博主采集", "stopped": True, "was_running": was_running}

    def get_status(self) -> dict:
        settings = get_settings()
        in_cd, cd_remaining = self.is_in_cooldown()
        start_h, end_h = self._get_fetch_window_hours()
        accounts = self.get_accounts()

        enabled_count = sum(1 for a in accounts if a.get("auto_collect_enabled"))
        running_users = list(self._fetching)
        running_nicks = [
            next((a.get("nickname") for a in accounts if a.get("user_id") == uid), uid)
            for uid in running_users
        ]
        is_collecting = len(running_users) > 0

        return {
            "is_running": self._is_running,
            "is_collecting": is_collecting,
            "global_enabled": bool(settings.get("xhs_auto_collect_enabled", False)),
            "in_window": self.is_in_fetch_window(),
            "window_start_hour": start_h,
            "window_end_hour": end_h,
            "max_per_account": settings.get("xhs_collect_max_per_account", 20),
            "cooldown_minutes": settings.get("xhs_collect_cooldown_minutes", 15),
            "upload_enabled": bool(settings.get("xhs_upload_enabled", False)),
            "upload_url": settings.get("xhs_upload_url", "") or settings.get("rss_upload_url", ""),
            "device_id": settings.get("xhs_device_id", "小红书_caiji100"),
            "cos_prefix": settings.get("xhs_cos_prefix", "channels/"),
            "has_cos_config": bool(settings.get("cos_token_api_url") or (settings.get("cos_secret_id") and settings.get("cos_bucket"))),
            "in_cooldown": in_cd,
            "cooldown_remaining": cd_remaining,
            "total_accounts": len(accounts),
            "enabled_accounts_count": enabled_count,
            "running_users": running_users,
            "running_nicknames": running_nicks,
            "last_error": self._last_error,
            "accounts": accounts
        }

    def trigger_collect(self, user_id: str) -> dict:
        accounts = self.get_accounts()
        target = next((a for a in accounts if a.get("user_id") == user_id), None)
        if not target:
            raise ValueError("未找到该博主")

        # 重置单博主取消标记与全局中止标记
        self._abort_all_event.clear()
        self._cancelled_users.discard(user_id)

        submitted = self.submit_fetch(target)
        return {"message": "已提交采集任务" if submitted else "该博主已在采集队列中", "submitted": submitted}

    def trigger_all(self) -> dict:
        accounts = self.get_accounts()
        enabled = [a for a in accounts if a.get("auto_collect_enabled")]
        if not enabled:
            # 如果没有单独开启的博主，则对所有收藏博主执行一次
            enabled = accounts

        # 重置取消标记与全局中止标记
        self._abort_all_event.clear()
        self._cancelled_users.clear()

        # 按最久未采集时间升序排序，平滑有序入队
        enabled.sort(key=lambda a: self._safe_float(a.get("last_collect_time"), 0))

        submitted_count = 0
        for a in enabled:
            if self.submit_fetch(a):
                submitted_count += 1
        return {"message": f"已按安全防风控队列提交 {submitted_count} 位博主的采集任务", "count": submitted_count}


# 全局单例
xhs_collector = XhsAutoCollector()

