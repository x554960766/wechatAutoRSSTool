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

from mac.steps.step1_launch import ensure_wechat_ready
from mac.steps.step2_search import search_and_open_web_window
from mac.steps.step3_open_article import find_and_click_article_mac
from mac.steps.step4_cleanup import cleanup_mac_wechat_windows
from mac.mac_input import human_sleep

from backend.config import DATA_DIR, load_json, save_json, DOWNLOAD_HISTORY_FILE
from backend.account_pool import account_pool, AccountPool
from backend.articles import _fetch_articles_page

logger = logging.getLogger("wechat_auto_mac.batch")


class WeChatBatchRunner:
    """批量公众号流水线同步执行引擎"""

    def __init__(
        self,
        articles_per_account: int = 10,
        jitter_range: tuple[float, float] = (2.5, 4.5),
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

    def sync_single_account(self, account_name: str, fakeid: Optional[str] = None) -> dict:
        """同步单个公众号：UI 触发 -> 捕获凭据 -> 立即拉取列表入库 -> 清理窗口"""
        t0 = time.time()
        logger.info("👉 [Batch] 开始处理公众号: 【%s】 (指定 fakeid: %s)", account_name, fakeid or "待捕获")
        
        # 记录触发前的最新凭证保存时间
        accs = account_pool._load()
        old_save_time = max((a.get("save_time", 0) for a in accs), default=0)

        # 1. 确保微信就绪
        ensure_wechat_ready()

        # 2. 搜一搜并打开窗口
        web_win = search_and_open_web_window(keyword=account_name)
        human_sleep(0.8, 1.2)

        # 3. OCR 识别文章并点击打开触发抓包
        step3_res = find_and_click_article_mac(
            web_win=web_win,
            max_scrolls=3,
            min_candidates=1,
            random_pick=False,
            wait_cred_update_seconds=5.0
        )

        # 4. 提取最新捕获的 fakeid (biz) 与凭证
        target_fakeid = fakeid
        updated_acc = None
        for a in account_pool._load():
            if a.get("status") == "active" and a.get("save_time", 0) > old_save_time:
                updated_acc = a
                if not target_fakeid:
                    target_fakeid = a.get("biz")
                break

        if not target_fakeid:
            # 若仍未拿到，从全局活跃账号的 biz_tokens 尝试提取
            if updated_acc and updated_acc.get("biz_tokens"):
                target_fakeid = list(updated_acc["biz_tokens"].keys())[-1]

        if not target_fakeid:
            raise RuntimeError(f"未能截获到公众号【{account_name}】的 fakeid (__biz)")

        # 5. 立即调用 API 拉取历史文章 (带 UI OCR 解析兜底)
        articles = []
        total_count = 0
        try:
            articles, total_count, can_continue = _fetch_articles_page(
                fakeid=target_fakeid,
                begin=0,
                count=self.articles_per_account,
                keyword="",
                account_name=account_name
            )
        except Exception as fetch_err:
            logger.warning("API 拉取遇到会话限制 (%s)，采用 UI 界面解析到的最新文章数据入库...", fetch_err)
            candidates = step3_res.get("candidates", [])
            for c in candidates:
                articles.append({
                    "title": c.get("title", ""),
                    "digest": c.get("meta", ""),
                    "link": f"https://mp.weixin.qq.com/s?__biz={target_fakeid}&title={urllib.parse.quote(c.get('title', ''))}",
                    "cover": "",
                    "author": account_name,
                    "update_time": int(time.time()),
                })
            total_count = len(articles)

        # 6. 保存/追加到本地历史数据库
        if articles:
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
                        "fakeid": target_fakeid,
                        "publish_time": art.get("update_time", int(time.time())),
                        "download_time": int(time.time()),
                        "status": "synced",
                    })
                    existing_titles.add(t)
                    new_added += 1

            if new_added > 0:
                save_json(DOWNLOAD_HISTORY_FILE, history)
                logger.info("💾 [数据入库] 公众号【%s】新增 %d 篇文章至本地历史库 (总拉取: %d)", account_name, new_added, len(articles))

        # 7. 清理窗口
        if self.auto_cleanup:
            try:
                human_sleep(0.8, 1.2)
                cleanup_mac_wechat_windows()
            except Exception as clean_err:
                logger.warning("清理窗口时出现警告: %s", clean_err)

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
