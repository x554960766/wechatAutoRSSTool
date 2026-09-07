"""macOS 微信 PC 端公众号批量“即产即销”流水线调度器 (Batch Pipeline Runner)。
支持数百个公众号列表的自动化队列处理：
1. 自动从微信搜一搜/历史卡片打开目标公众号文章；
2. 毫秒级捕获该号专属凭证 (appmsg_token, key, pass_ticket)；
3. 立即调用微信原生接口 (/mp/profile_ext) 同步该号文章列表并入库；
4. 关闭微信临时 Web 窗口，执行随机安全休眠 (2.5~4.5s)；
5. 每处理 30 个号自动深度冷却 2 分钟，防止频控。
"""
from __future__ import annotations

import os
import sys

def ensure_virtualenv():
    """检测当前是否运行在虚拟环境 venv312 中。
    如果不是，并且检测到本地存在 venv312，则自动使用 venv312 的 python 解释器重载当前脚本！
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    if getattr(sys, 'frozen', False):
        return

    if sys.platform == 'win32':
        venv_python = os.path.join(project_root, 'venv312', 'Scripts', 'python.exe')
    else:
        venv_python = os.path.join(project_root, 'venv312', 'bin', 'python')
    if os.path.exists(venv_python):
        current_exe = os.path.abspath(sys.executable)
        target_exe = os.path.abspath(venv_python)
        if current_exe != target_exe:
            env = dict(os.environ)
            old_pypath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{project_root}:{old_pypath}" if old_pypath else project_root
            args = [venv_python, "-m", "mac.batch_runner"] + sys.argv[1:]
            os.execve(venv_python, args, env)




import argparse
import logging
import random
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

from mac.mac_win import (
    find_portal_window, find_main_window, activate_wechat, raise_window,
    capture_window, list_wechat_windows, MacWindow
)
from mac.scale import CoordSpace
from mac.mac_input import move_and_click, human_sleep, scroll, press, type_text_via_clipboard
from mac.mac_ocr import ocr
from mac.steps.step4_cleanup import close_native_account_windows, cleanup_mac_wechat_windows, is_portal_window

from backend.config import DATA_DIR, load_json, save_json, DOWNLOAD_HISTORY_FILE
from backend.account_pool import account_pool, AccountPool
from backend.articles import _fetch_articles_page
from backend.mitm_proxy import ProxyManager

logger = logging.getLogger("wechat_auto_mac.batch")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_sh)
logger.propagate = False


class WeChatBatchRunner:
    """批量公众号流水线同步执行引擎（聚合页 OCR 驱动，零搜索，保留聚合页并关闭原生名片）"""

    def __init__(
        self,
        articles_per_account: int = 10,
        jitter_range: tuple[float, float] = (1.5, 3.0),
        batch_rest_every: int = 30,
        batch_rest_duration: float = 120.0,
        auto_cleanup: bool = True
    ):
        self.articles_per_account = articles_per_account
        self.jitter_range = jitter_range
        self.batch_rest_every = batch_rest_every
        self.batch_rest_duration = batch_rest_duration
        self.auto_cleanup = auto_cleanup

        self.total_processed = 0
        self.success_count = 0
        self.failed_list: list[dict] = []
        self.total_articles_synced = 0

    def _save_articles_to_history(self, articles: list[dict], account_name: str, fakeid: str) -> int:
        """保存/追加抓取到的文章到本地历史数据库"""
        if not articles:
            return 0
        history = load_json(DOWNLOAD_HISTORY_FILE, [])
        existing_titles = {item.get("title") for item in history if item.get("title")}
        new_added = 0
        for art in articles:
            t = art.get("title", "")
            if t and t not in existing_titles:
                history.append({
                    "title": t,
                    "link": art.get("link", ""),
                    "cover": art.get("cover", ""),
                    "digest": art.get("digest", ""),
                    "author": art.get("author") or account_name,
                    "account_name": account_name,
                    "fakeid": fakeid,
                    "publish_time": art.get("update_time", int(time.time())),
                    "download_time": int(time.time()),
                    "status": "synced",
                })
                existing_titles.add(t)
                new_added += 1

        if new_added > 0:
            save_json(DOWNLOAD_HISTORY_FILE, history)
            logger.info("💾 [数据入库] 公众号【%s】新增 %d 篇文章至本地历史库 (总拉取: %d)", account_name, new_added, len(articles))
        return new_added

    def _ensure_portal_window(self) -> MacWindow:
        """确保屏幕上存在批量授权聚合页窗口，如果不存在则通过微信文件传输助手自动唤起。"""
        # 确保代理助手处于运行状态
        import socket
        proxy_already_open = False
        try:
            with socket.create_connection(('127.0.0.1', 5202), timeout=0.5):
                proxy_already_open = True
        except Exception:
            pass

        if not proxy_already_open:
            try:
                mgr = ProxyManager.get_instance()
                mgr.start()
            except Exception:
                pass

        activate_wechat()
        human_sleep(0.3, 0.5)

    def _is_portal_content(self, win: MacWindow) -> bool:
        """OCR 快速校验指定窗口当前是否确实展示批量授权聚合页内容（严格检查正文，过滤顶部标签栏与错误页）"""
        try:
            img = capture_window(win)
            boxes = ocr(img)
            # 过滤顶部标签栏（top < 60），针对正文内容区
            body_boxes = [b for b in boxes if b.top > 60]
            if not body_boxes:
                return False
            # 校验是否为断网或错误提示页
            if any(k in b.text for b in body_boxes for k in ('ERR_', '未连接到互联网', '代理服务器出现问题', '无法访问此网站', 'ERR_PROXY')):
                return False
            portal_keywords = ('公众号清单', '会话已就绪', '点击授权', '刷新当前就绪状态', '全部公众号', '全部（', '待授权（')
            return any(k in b.text for b in body_boxes for k in portal_keywords)
        except Exception:
            return False

    def _ensure_portal_window(self) -> MacWindow:
        """确保屏幕上有且仅有处于前台的可用批量授权聚合页窗口。
        容错规则：
        1. 若已存在聚合页窗口且内容正确，直接置顶复用，绝不重复打开；
        2. 若窗口存在但被其他页面/新标签覆盖，尝试按 Cmd+W 恢复，恢复成功则继续复用；
        3. 若仍不是聚合页或完全无聚合页窗口，则通过文件传输助手重新拉起聚合页；
        4. 拉起后轮询直至窗口出现且 OCR 确认聚合页内容就绪。
        """
        # 1. 优先检查已存在的候选窗口
        portal = find_portal_window()
        if portal:
            raise_window(portal)
            human_sleep(0.3, 0.5)
            if self._is_portal_content(portal):
                logger.info("🎯 检测到聚合页窗口已存在且内容完好，无需重新打开，直接复用 (Window ID: %d)", portal.window_id)
                return portal
            else:
                logger.warning("⚠️ 检测到窗口 (ID: %d) 当前不是聚合页内容或被覆盖，尝试按 Cmd+W 恢复...", portal.window_id)
                press('command', 'w')
                human_sleep(0.5, 0.8)
                if self._is_portal_content(portal):
                    logger.info("✅ 成功恢复聚合页窗口，继续复用！")
                    return portal

        # 2. 如果不存在或未恢复，通过微信主界面（文件传输助手）重新打开聚合页
        logger.info("🚀 当前页面不是聚合页，正在通过文件传输助手重新打开聚合页...")
        activate_wechat()
        human_sleep(0.3, 0.5)

        main = find_main_window(timeout=3.0)
        if not main:
            raise RuntimeError("未检测到微信客户端窗口，请先打开微信 Mac 版。")

        raise_window(main)
        human_sleep(0.3, 0.5)

        cs = CoordSpace(main.window_id, {'x': main.x, 'y': main.y, 'w': main.w, 'h': main.h})
        img = capture_window(main)
        boxes = ocr(img)

        in_filehelper = any('文件传输助手' in b.text and b.top < 80 for b in boxes)
        if not in_filehelper:
            for b in boxes:
                if b.left < 250 and '文件传输助手' in b.text:
                    sx, sy = cs.img_to_screen(b.center[0], b.center[1])
                    move_and_click(sx, sy)
                    human_sleep(0.5, 0.8)
                    break

        img2 = capture_window(main)
        boxes2 = ocr(img2)
        link_box = next((b for b in reversed(boxes2) if b.left > 300 and any(k in b.text for k in ('5200', 'mp-batch', 'portal'))), None)
        if link_box:
            sx, sy = cs.img_to_screen(link_box.center[0], link_box.center[1])
            move_and_click(sx, sy)
        else:
            portal_url = "http://127.0.0.1:5200/api/auth/mp-batch-portal"
            input_x = main.x + int(main.w * 0.6)
            input_y = main.y + main.h - 80
            move_and_click(input_x, input_y)
            human_sleep(0.2, 0.3)
            type_text_via_clipboard(portal_url, clear_first=False)
            human_sleep(0.2, 0.3)
            press("return")
            human_sleep(0.8, 1.2)
            img3 = capture_window(main)
            boxes3 = ocr(img3)
            link_box = next((b for b in reversed(boxes3) if b.left > 300 and any(k in b.text for k in ('5200', 'mp-batch', 'portal'))), None)
            if link_box:
                sx, sy = cs.img_to_screen(link_box.center[0], link_box.center[1])
                move_and_click(sx, sy)

        # 3. 轮询等待聚合页窗口出现并确认内容
        deadline = time.time() + 8.0
        while time.time() < deadline:
            time.sleep(0.5)
            portal = find_portal_window()
            if portal:
                raise_window(portal)
                if self._is_portal_content(portal):
                    logger.info("🎉 聚合页窗口已成功重新打开并校验就绪 (Window ID: %d)！", portal.window_id)
                    return portal

        if not portal:
            raise RuntimeError("未能唤起批量授权聚合页窗口，请手动在微信文件传输助手中点击聚合页链接！")
        return portal

    def _sync_via_portal_ocr(self, portal_win: MacWindow, account_name: str, target_fakeid: str) -> bool:
        """纯 OCR 驱动：在聚合页中识别指定公众号卡片并点击触发抓包，关闭原生名片弹窗，保留聚合页。"""
        # 0. 前置容错：先关闭任何上次可能残留的原生公众号名片窗口
        close_native_account_windows()

        # 1. 确保聚合页置顶在最前，并校验当前内容
        raise_window(portal_win)
        human_sleep(0.3, 0.5)
        if not self._is_portal_content(portal_win):
            portal_win = self._ensure_portal_window()

        cs = CoordSpace(portal_win.window_id, {'x': portal_win.x, 'y': portal_win.y, 'w': portal_win.w, 'h': portal_win.h})

        name_clean = account_name.replace(" ", "")
        tokens = [t for t in account_name.split() if len(t) >= 2]

        def matches_acc(txt_clean: str) -> bool:
            if name_clean in txt_clean or txt_clean in name_clean:
                return True
            if any(t in txt_clean for t in tokens):
                return True
            if len(name_clean) >= 3 and name_clean[:3] in txt_clean:
                return True
            return False

        # 2. 多轮 OCR 扫描寻找该公众号卡片（支持列表滚动容错）
        target_box = None
        for attempt in range(4):
            img = capture_window(portal_win)
            boxes = ocr(img)

            for b in boxes:
                if b.top > 150:
                    txt = b.text.replace(" ", "")
                    if matches_acc(txt):
                        target_box = b
                        break

            if target_box:
                break

            # 若未在当前可视区域发现，在列表区域向下滑动
            if attempt < 3:
                move_and_click(portal_win.x + portal_win.w // 2, portal_win.y + portal_win.h // 2)
                human_sleep(0.2, 0.3)
                scroll(-6)
                human_sleep(0.5, 0.8)

        if not target_box:
            # 若向下滑动未找到，尝试滚回顶部再扫描一次（防止前面处理项滚到底部）
            move_and_click(portal_win.x + portal_win.w // 2, portal_win.y + portal_win.h // 2)
            press('command', 'up')
            scroll(30)
            human_sleep(0.5, 0.8)
            img = capture_window(portal_win)
            boxes = ocr(img)
            for b in boxes:
                if b.top > 120:
                    txt = b.text.replace(" ", "")
                    if matches_acc(txt):
                        target_box = b
                        break

        if not target_box:
            logger.warning("❌ [Portal OCR] 未在聚合页中找到公众号【%s】的卡片", account_name)
            return False

        logger.info("✅ [Portal OCR] 定位到公众号【%s】卡片: [%s] @ (%d, %d)", account_name, target_box.text, target_box.left, target_box.top)

        # 3. 点击卡片触发原生主页与抓包
        sx, sy = cs.img_to_screen(target_box.center[0], target_box.center[1])
        logger.info("👉 点击公众号【%s】卡片，屏幕坐标 (%d, %d)...", account_name, sx, sy)

        old_updated_at = 0
        if target_fakeid:
            acc = account_pool.acquire()
            cred = AccountPool.get_biz_credential(acc, target_fakeid) if acc else {}
            old_updated_at = cred.get('updated_at', 0)

        move_and_click(sx, sy)

        # 4. 轮询等待 MITM 抓包截获与原生公众号名片窗口出现并关闭（支持一次点击重试容错）
        deadline = time.time() + 6.0
        captured = False
        native_closed = False
        retried_click = False

        while time.time() < deadline:
            time.sleep(0.3)
            # 检查凭据库是否已更新
            if target_fakeid:
                acc = account_pool.acquire()
                cred = AccountPool.get_biz_credential(acc, target_fakeid) if acc else {}
                if cred.get('key') and cred.get('updated_at', 0) > old_updated_at:
                    if not captured:
                        captured = True
                        logger.info("🎉 成功捕获到公众号【%s】全新凭证!", account_name)

            # 若检测到原生公众号窗口打开，立即安全关闭
            if close_native_account_windows() > 0:
                native_closed = True

            if captured and native_closed:
                break

            # 容错重试：若等待 3 秒后依然没有任何响应，再次点击一次
            if not captured and not native_closed and not retried_click and (deadline - time.time() < 3.0):
                retried_click = True
                logger.info("🔄 [容错重试] 距初次点击无响应，重新置顶聚合页并重试点击卡片 (%d, %d)...", sx, sy)
                raise_window(portal_win)
                move_and_click(sx, sy)

        # 5. 坚决关闭可能延迟弹出的原生名片窗口（最多等 2.5 秒直至关闭）
        if not native_closed:
            t_close_deadline = time.time() + 2.5
            while time.time() < t_close_deadline:
                time.sleep(0.25)
                if close_native_account_windows() > 0:
                    break

        # 6. 如果在聚合页窗口内弹出了新标签页，关闭新标签页返回聚合页
        try:
            if not self._is_portal_content(portal_win):
                logger.info("检测到聚合页被新标签覆盖，正在关闭标签返回聚合页...")
                press('command', 'w')
                human_sleep(0.3, 0.5)
        except Exception:
            pass

        return captured

    def sync_single_account(self, account_name: str, fakeid: Optional[str] = None) -> dict:
        """从聚合页逐个打开公众号主页 -> 截获凭据 -> 立即关闭原生窗口 -> 同步文章入库"""
        t0 = time.time()
        logger.info("👉 [Batch] 开始处理公众号: 【%s】 (指定 fakeid: %s)", account_name, fakeid or "待捕获")
        
        target_fakeid = fakeid
        if not target_fakeid:
            try:
                from backend.accounts import _load_accounts
                for a in _load_accounts():
                    if (a.get("nickname") or a.get("name")) == account_name:
                        target_fakeid = a.get("fakeid") or a.get("alias")
                        break
            except Exception:
                pass

        # ⚡ 纯聚合页流转流程：确保聚合页在最前，逐个点击打开并同步
        portal_win = self._ensure_portal_window()
        ocr_ok = self._sync_via_portal_ocr(portal_win, account_name, target_fakeid)

        # 再次尝试提取最新捕获的 fakeid (biz) 与凭证
        if not target_fakeid:
            acc = account_pool.acquire()
            if acc and acc.get("biz_tokens"):
                target_fakeid = list(acc["biz_tokens"].keys())[-1]

        if not target_fakeid:
            raise RuntimeError(f"未能获取到公众号【{account_name}】的 fakeid (__biz)")

        if not ocr_ok:
            acc = account_pool.acquire()
            cred = AccountPool.get_biz_credential(acc, target_fakeid) if acc else {}
            if not cred.get("key"):
                raise RuntimeError(f"未能通过聚合页 OCR 激活公众号【{account_name}】并捕获有效凭证")

        # 立即调用 API 拉取历史文章
        articles, total_count, can_continue = _fetch_articles_page(
            fakeid=target_fakeid,
            begin=0,
            count=self.articles_per_account,
            keyword="",
            account_name=account_name
        )

        if articles:
            self._save_articles_to_history(articles, account_name, target_fakeid)

        elapsed = time.time() - t0
        logger.info("✅ [Batch] 公众号【%s】同步成功! 耗时: %.2fs, 文章数: %d", account_name, elapsed, len(articles))

        return {
            "success": True,
            "account_name": account_name,
            "fakeid": target_fakeid,
            "articles_count": len(articles),
            "total_count": total_count,
            "elapsed": elapsed
        }

    def run_queue(
        self,
        account_list: list[str | dict],
        progress_callback: Optional[Callable[[int, int, str, bool, str], None]] = None
    ) -> dict:
        """执行整个批量队列"""
        total_accounts = len(account_list)
        logger.info("🚀 [Batch Runner] 启动批量流水线同步任务，总待处理目标数: %d", total_accounts)

        t_task_start = time.time()

        for idx, item in enumerate(account_list, start=1):
            if isinstance(item, dict):
                acc_name = item.get("name") or item.get("nickname") or item.get("keyword", "")
                fakeid = item.get("fakeid") or item.get("biz")
            else:
                acc_name = str(item).strip()
                fakeid = None

            if not acc_name:
                continue

            logger.info("═══════════════════════════════════════════════════")
            logger.info("⏳ 进度 [%d/%d] 正在处理: 【%s】", idx, total_accounts, acc_name)
            logger.info("═══════════════════════════════════════════════════")

            success = False
            err_msg = ""
            articles_count = 0

            try:
                res = self.sync_single_account(acc_name, fakeid)
                success = True
                articles_count = res.get("articles_count", 0)
                self.success_count += 1
                self.total_articles_synced += articles_count
            except Exception as e:
                err_msg = str(e)
                logger.error("❌ [%d/%d] 公众号【%s】同步失败: %s", idx, total_accounts, acc_name, err_msg)
                self.failed_list.append({
                    "index": idx,
                    "account_name": acc_name,
                    "fakeid": fakeid,
                    "error": err_msg
                })
                # 出现异常时强制清理多余窗口
                if self.auto_cleanup:
                    try:
                        cleanup_mac_wechat_windows()
                    except Exception:
                        pass
            finally:
                self.total_processed += 1
                try:
                    close_native_account_windows()
                except Exception:
                    pass
                if progress_callback:
                    try:
                        progress_callback(idx, total_accounts, acc_name, success, err_msg)
                    except Exception:
                        pass

            # 若还有后续任务，执行安全避让休眠
            if idx < total_accounts:
                # 检查是否达到批次深度休息阈值
                if idx % self.batch_rest_every == 0:
                    logger.info("☕ [批次冷却] 已连续处理 %d 个公众号，深度休息 %.0f 秒以重置微信频控计数器...", idx, self.batch_rest_duration)
                    time.sleep(self.batch_rest_duration)
                else:
                    sleep_sec = random.uniform(*self.jitter_range)
                    logger.info("😴 [随机避让] 安全休眠 %.2f 秒...", sleep_sec)
                    time.sleep(sleep_sec)

        total_elapsed = time.time() - t_task_start
        logger.info("🎉 [Batch Runner] 批量任务全部完成!")
        logger.info("📊 总处理: %d | 成功: %d | 失败: %d | 同步文章总数: %d | 总耗时: %.2fs",
                    self.total_processed, self.success_count, len(self.failed_list),
                    self.total_articles_synced, total_elapsed)

        return {
            "total": self.total_processed,
            "success": self.success_count,
            "failed_count": len(self.failed_list),
            "failed_list": self.failed_list,
            "total_articles_synced": self.total_articles_synced,
            "total_elapsed_seconds": total_elapsed
        }


def run_batch_pipeline(
    accounts: list[str | dict],
    articles_per_account: int = 10,
    batch_rest_every: int = 30
) -> dict:
    """批量流水线快捷入口"""
    runner = WeChatBatchRunner(
        articles_per_account=articles_per_account,
        batch_rest_every=batch_rest_every
    )
    return runner.run_queue(accounts)
