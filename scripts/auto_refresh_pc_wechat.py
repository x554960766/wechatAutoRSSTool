#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信 PC 客户端凭证自动刷新脚本 (Auto Refresh PC WeChat Credentials)
支持 Windows (参考 Access_wechat_article 的 UIAutomation 逻辑) 和 macOS (使用 AppleScript UI 自动化)。
触发微信客户端刷新页面或点击文章，促使微信内置浏览器发起带 key / pass_ticket / token 的请求，
从而由本地 mitm_proxy 捕获最新的客户端凭证。
"""

import sys
import time
import logging
import platform
import subprocess
import threading
from pathlib import Path

# 保证项目根目录在 sys.path 中
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")
logger = logging.getLogger("auto_refresh_pc_wechat")

# ── 配置参数 ──────────────────────────────────────────
DAEMON_CHECK_INTERVAL = 5 * 60          # 守护线程检测间隔：5 分钟（测试用）
STALE_THRESHOLD = 5 * 60                # 凭证老化阈值：5 分钟（测试用）
MIN_REFRESH_INTERVAL = 30               # 至少间隔 30 秒才允许再次触发 UI 刷新，防止高频冲突
SUCCESS_COALESCE_SECONDS = 60           # 成功刷新后 1 分钟内合并重复触发
PROACTIVE_RENEW_THRESHOLD = 5 * 60      # biz 专属凭证年龄超过 5 分钟即主动续期（测试用）
PROACTIVE_RETRY_BACKOFF = 2 * 60        # 同一公众号两次主动续期尝试的最小间隔（2分钟）
TARGET_PACING_RANGE = (10, 20)          # 相邻两次定向续期之间的随机间隔（秒）
IDLE_CHECK_INTERVAL = 5 * 60            # 巡检间隔：5 分钟（300 秒，测试用）
WORKER_STARTUP_SILENCE = 5              # 测试模式启动静默期：5 秒

_last_refresh_time = 0
_last_success_time = 0
_daemon_started = False
_daemon_lock = threading.Lock()
_ui_flow_lock = threading.Lock()        # 全局 UI 互斥：同一时刻只允许一个 UI 自动化流程操作微信客户端
_proactive_last_attempt = {}            # biz -> 上次主动续期尝试时间（退避用，防止失败目标霸占名额）

ENABLE_BACKGROUND_ACTIVE_REFRESH = False  # 微信凭证后台主动刷新，默认关闭，需手动开启


def get_auto_refresh_config() -> dict:
    """获取当前定时同步配置状态"""
    return {
        "enabled": ENABLE_BACKGROUND_ACTIVE_REFRESH,
        "interval_minutes": IDLE_CHECK_INTERVAL // 60,
        "interval_seconds": IDLE_CHECK_INTERVAL
    }


def set_auto_refresh_enabled(enabled: bool) -> dict:
    """设置定时同步开关状态"""
    global ENABLE_BACKGROUND_ACTIVE_REFRESH
    ENABLE_BACKGROUND_ACTIVE_REFRESH = bool(enabled)
    logger.info("🔧 定时自动同步开关已更新为: %s (间隔: %d 分钟)", ENABLE_BACKGROUND_ACTIVE_REFRESH, IDLE_CHECK_INTERVAL // 60)
    return get_auto_refresh_config()

def is_wechat_running_macos() -> bool:
    """检测 macOS 上微信是否在运行。"""
    try:
        res = subprocess.run(["pgrep", "-f", "WeChat"], capture_output=True, text=True)
        return res.returncode == 0 and len(res.stdout.strip()) > 0
    except Exception:
        return False


def refresh_wechat_pc_macos(keyword: str = "新京报") -> bool:
    """macOS 下使用 AppleScript + Vision OCR 自动化搜索并点击文章以刷新凭证。"""
    logger.info("正在检测 macOS 微信客户端进程...")

    if not is_wechat_running_macos():
        logger.warning("⚠️ 未检测到运行中的 macOS 微信客户端，请先打开并登录微信 Mac 版。")
        return False

    logger.info("正在调用 macOS UI 自动化流程（搜一搜 + Vision OCR 识别文章卡片 + 精确点击，目标: %s）...", keyword)

    # 1. 优先使用基于 AppleScript + Vision OCR 相对时间锚点的稳定自动化全流程
    try:
        from mac.main_mac import run_wechat_pc_flow_macos
        success = run_wechat_pc_flow_macos(keyword=keyword, auto_cleanup=True)
        if success:
            logger.info("✅ 已通过 macOS UI 自动化成功执行搜一搜文章定位与点击！")
            return True
        else:
            logger.warning("⚠️ macOS UI 自动化流程未能完成。")
            return False
    except Exception as e:
        logger.warning("⚠️ 载入或执行 mac.main_mac 流程异常: %s", e)
        return False


def _find_dropdown_filehelper_box(boxes: list) -> any:
    """从微信搜索下拉列表中精准定位「功能」分类下的「文件传输助手」。
    核心原理：
    搜索浮层自上而下结构为：
      1. 顶部「搜索网络结果」及 1~5 条联想词（因关键字高亮，每条均包含一个独立的 '文件传输助手' 文本块）
      2. 中部「功能」标题及绿色图标的真实「文件传输助手」（这是所有搜索联想词之下的最后一个 '文件传输助手' 文本块）
      3. 底部「聊天记录」等区域
    定位策略：
      A. 若识别到「功能」标题（严格短文本），取位于「功能」下方且在「聊天记录」上方的「文件传输助手」；
      B. 若未识别到「功能」标题，收集所有在「聊天记录」上方的「文件传输助手」候选块，按 y 坐标排序取最下方的那一个（即最后一个 cands[-1]，必然为「功能」条目）。
    """
    if not boxes:
        return None

    # 1. 寻找「聊天记录」作为绝对下边界
    chat_header = None
    for b in boxes:
        txt = b.text.strip()
        if "聊天记录" in txt and len(txt) <= 6:
            chat_header = b
            break
    chat_top = chat_header.top if chat_header else float("inf")

    # 2. 寻找严格的「功能」标题（长度 <= 3，避免匹配网络词 '文件传输助手已读功能'）
    func_header = None
    for b in boxes:
        txt = b.text.strip()
        if txt == "功能" or (len(txt) <= 3 and "功能" in txt and b.top < chat_top):
            func_header = b
            break

    # 3. 收集所有位于聊天记录上方的「文件传输助手」候选块
    cands = []
    for b in boxes:
        if b.top >= chat_top:
            continue
        txt = b.text.strip()
        # 匹配包含“文件传输助手”的文本块
        if "文件传输助手" in txt:
            # 排除聊天记录正文等长句
            if "记录" not in txt and "弱智" not in txt:
                cands.append(b)

    if not cands:
        return None

    # 按垂直 y 坐标从小到大（从上到下）排序
    cands.sort(key=lambda x: x.top)

    # 策略 A：如果找到了「功能」header，取功能下方的第一个
    if func_header:
        below_func = [b for b in cands if b.top >= func_header.top]
        if below_func:
            logger.info("🎯 在「功能」标题 (y=%d) 下方匹配到文件传输助手: %s (y=%d)", func_header.top, below_func[0].text, below_func[0].top)
            return below_func[0]

    # 策略 B：若未识别到「功能」标题，由于搜索网络结果在上方(1~5项)，「功能」项在最下方，
    # 因此最底部的候选项（cands[-1]）正是「功能」分类下的文件传输助手！
    target = cands[-1]
    logger.info("🎯 从 %d 个下拉候选词中精准选取最下方「功能」项: %s (y=%d)", len(cands), target.text, target.top)
    return target


def bring_window_to_front(win) -> bool:
    """将指定微信窗口置于最前台顶层聚焦（解决被其他窗口遮挡导致鼠标点击落空的问题）。"""
    try:
        from mac.mac_win import activate_wechat
        from mac.mac_input import move_and_click, human_sleep
        activate_wechat()

        script = '''
        tell application "System Events"
            tell process "WeChat"
                set frontmost to true
                try
                    perform action "AXRaise" of (first window whose name contains "批量授权" or name contains "授权中心" or name contains "聚合")
                end try
            end tell
        end tell
        '''
        subprocess.run(["osascript", "-e", script], check=False, timeout=1.5)

        # 模拟点击窗口顶部标题栏（win.x + win.w // 2, win.y + 12）确保物理置顶与激活
        title_x = win.x + win.w // 2
        title_y = win.y + 12
        move_and_click(title_x, title_y)
        human_sleep(0.3, 0.5)
        return True
    except Exception as e:
        logger.warning("置顶聚焦窗口失败: %s", e)
        return False


def _trigger_and_click_portal_button(web_win) -> bool:
    """在聚合页 Web 窗口中将窗口置顶并通过 OCR 动态定位并点击「一键开始全自动流转授权」按钮（绝不使用写死坐标）。"""
    try:
        from mac.mac_win import capture_window
        from mac.mac_ocr import ocr
        from mac.scale import CoordSpace
        from mac.mac_input import move_and_click, human_sleep

        bring_window_to_front(web_win)
        human_sleep(0.5, 0.8)

        # 尝试最多 3 次 OCR 动态探测（支持页面渲染微延时）
        btn_box = None
        cs_w = None
        for attempt in range(3):
            img_w = capture_window(web_win)
            if img_w is None or img_w.size == 0:
                human_sleep(0.3, 0.5)
                continue

            boxes_w = ocr(img_w)
            cs_w = CoordSpace(web_win.window_id, {"x": web_win.x, "y": web_win.y, "w": web_win.w, "h": web_win.h})

            # 严格多维度打分匹配目标按钮，杜绝误点标题或说明文字
            candidates = []
            for b in boxes_w:
                txt = b.text.replace(" ", "").strip()
                # 排除非按钮文本（标题、清单、建议说明等）
                if any(bad in txt for bad in ("中心", "清单", "建议", "说明", "低风控", "公众号", "配额", "间隔", "就绪", "复制")):
                    continue
                # 匹配按钮特征关键词
                score = 0
                if "流转授权" in txt: score += 20
                if "全自动" in txt: score += 15
                if "一键开始" in txt: score += 12
                if "自动流转" in txt: score += 10
                if "开始" in txt: score += 5
                if "授权" in txt: score += 4
                if "一键" in txt: score += 3

                if score > 0:
                    candidates.append((score, b))

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                btn_box = candidates[0][1]
                break

            human_sleep(0.4, 0.6)

        if btn_box and cs_w:
            sx, sy = cs_w.img_to_screen(btn_box.center[0], btn_box.center[1])
            logger.info("✅ 聚合页已置顶，通过 OCR 动态精准定位到流转按钮 [%s] 坐标 (%d, %d)", btn_box.text, sx, sy)
            move_and_click(sx, sy)
            human_sleep(0.8, 1.2)
            return True
        else:
            logger.warning("❌ OCR 未能在聚合页窗口识别到流转授权按钮，取消硬编码点击，请检查页面内容")
            return False
    except Exception as e:
        logger.warning("在聚合页点击授权按钮异常: %s", e)
        return False


def _wait_batch_portal_completion(sync_start_time: float, timeout_seconds: float = 45.0) -> bool:
    """等待单窗口流水线自动流转激活所有公众号主页会话（不关闭窗口，保留以便下次复用）。
    必须校验 updated_at >= sync_start_time - 2.0，确保凭证是本次新同步捕获的，而非复用历史旧凭证！
    """
    logger.info("⏳ 正在等待单窗口流水线自动流转激活所有公众号主页会话 (本次启动时间戳: %.2f)...", sync_start_time)
    from backend.config import ACCOUNTS_FILE, load_json
    from backend.account_pool import AccountPool, account_pool
    accounts_sub = load_json(ACCOUNTS_FILE, [])
    target_count = len(accounts_sub)

    deadline = time.time() + timeout_seconds
    all_ready = False
    ready_cnt = 0
    while time.time() < deadline:
        time.sleep(1.5)
        pool_acc = account_pool.acquire() or {}
        ready_cnt = 0
        now_check = time.time()
        for acc in accounts_sub:
            fakeid = acc.get("fakeid")
            cred = AccountPool.get_biz_credential(pool_acc, fakeid) if pool_acc else {}
            # 校验：1) 具备 key 2) getmsg_ready 3) 本次同步新捕获或处于 30 分钟内的新鲜窗口
            if cred.get("getmsg_ready") and cred.get("key"):
                updated_at = cred.get("updated_at", 0)
                if updated_at >= (sync_start_time - 2.0) or (now_check - updated_at < 1800):
                    ready_cnt += 1
        logger.info("当前批量授权同步进度: [%d/%d] (有效就绪凭证)", ready_cnt, target_count)
        if target_count > 0 and ready_cnt >= target_count:
            all_ready = True
            break

    if all_ready:
        logger.info("🎉 全部 %d 个公众号主页最新凭证已 100%% 自动捕获完成！", target_count)
        now = time.time()
        for acc in accounts_sub:
            fakeid = acc.get("fakeid")
            if fakeid:
                _proactive_last_attempt[fakeid] = now
        return True
    else:
        logger.warning("⚠️ 批量授权超时或部分未捕获最新凭证 (本次成功捕获: %d/%d)", ready_cnt, target_count)
        return False


def run_batch_portal_flow_macos(timeout_seconds: float = 45.0) -> bool:
    """macOS 下全自动唤起或复用批量授权聚合中心，自动流转建立全部公众号主页会话。
    多层级智能探测流程：
    【层级 1】优先检查屏幕上是否已有打开的「批量授权中心」网页窗口：
              若有，先将窗口置顶聚焦，再通过 OCR 定位并主动点击「一键开始批量授权」按钮！
    【层级 2】若无网页窗口，先检查微信主窗口当前是否已停留在「文件传输助手」会话界面且有链接：
              若有，直接点击链接气泡打开聚合页，跳过搜索！
    【层级 3】若未停留在「文件传输助手」，先在左侧会话列表查找点击；若无才按 Cmd+F 搜索并在下拉菜单中精准点击「功能 -> 文件传输助手」。
    【层级 4】发送/点击聚合页链接后，将新打开的聚合页窗口置顶并主动点击「一键开始批量授权」，等待流转完成（不关闭网页窗口）。
    """
    if not is_wechat_running_macos():
        logger.warning("未检测到运行中的 macOS 微信客户端，请先打开微信 Mac 版。")
        return False

    if not _ui_flow_lock.acquire(blocking=False):
        logger.info("🔒 另一个 UI 自动化流程正在执行中，跳过本次批量聚合页流转。")
        return False

    sync_start_time = time.time()

    try:
        from mac.mac_win import (
            find_main_window, activate_wechat, capture_window,
            list_wechat_windows, wait_new_web_window, find_web_windows
        )
        from mac.mac_ocr import ocr
        from mac.scale import CoordSpace
        from mac.mac_input import move_and_click, type_text_via_clipboard, human_sleep, press
        # 确保凭证同步代理助手已在运行，否则无法拦截与流转凭据
        try:
            from backend.mitm_proxy import ProxyManager
            mgr = ProxyManager.get_instance()
            if not mgr.running:
                logger.info("⚡️ 自动启动凭证同步代理助手...")
                mgr.start()
        except Exception as ex_proxy:
            logger.warning("启动凭证同步代理助手异常: %s", ex_proxy)

        # ──【层级 1】：优先检查当前屏幕上是否已有打开的聚合页 Web 窗口 ──
        existing_web_wins = find_web_windows()
        portal_win = None
        for w in existing_web_wins:
            if w.w >= 280 and w.h >= 350:
                try:
                    img_w = capture_window(w)
                    boxes_w = ocr(img_w)
                    if any(k in b.text for b in boxes_w for k in ("批量授权", "授权中心", "公众号主页", "一键开始", "授权清单")):
                        portal_win = w
                        break
                except Exception:
                    pass

        if portal_win:
            logger.info("🔍 检测到聚合页 Webview 窗口已存在 (id=%d, bounds=%s)，直接将窗口置顶并主动点击「一键开始批量授权」...", portal_win.window_id, (portal_win.x, portal_win.y, portal_win.w, portal_win.h))
            _trigger_and_click_portal_button(portal_win)
            return _wait_batch_portal_completion(sync_start_time, timeout_seconds)

        # ──【层级 2 & 3】：屏幕上没有聚合页窗口，激活主窗口并定位「文件传输助手」 ──
        logger.info("🚀 屏幕上未发现聚合页窗口，正在激活微信主窗口...")
        activate_wechat()
        human_sleep(0.5, 0.8)

        main_win = find_main_window(timeout=3.0)
        if not main_win:
            logger.warning("未找到微信主窗口。")
            return False

        # 确保微信主窗口置顶聚焦
        bring_window_to_front(main_win)
        human_sleep(0.3, 0.5)

        cs_main = CoordSpace(main_win.window_id, {"x": main_win.x, "y": main_win.y, "w": main_win.w, "h": main_win.h})
        img_main = capture_window(main_win)
        boxes_main = ocr(img_main)
        left_limit_px = int(img_main.shape[1] * 0.38)
        right_start_px = int(img_main.shape[1] * 0.35)

        # 检查微信主窗口当前是否已经在「文件传输助手」聊天界面且有可见链接
        in_filehelper_chat = any("文件传输助手" in b.text and b.top < 80 and b.left > right_start_px for b in boxes_main)
        link_box = None
        for b in reversed(boxes_main):
            if b.left > right_start_px and any(k in b.text for k in ("5200", "batch-portal", "127.0.0.1", "mp-batch")):
                link_box = b
                break

        before_web_ids = {w.window_id for w in find_web_windows()}

        if in_filehelper_chat and link_box:
            sx, sy = cs_main.img_to_screen(link_box.center[0], link_box.center[1])
            logger.info("✅ 当前已处于「文件传输助手」且已识别到聚合页链接 [%s]，直接点击坐标 (%d, %d)，跳过搜索！", link_box.text, sx, sy)
            move_and_click(sx, sy)
        else:
            # 不在文件传输助手时，先在左侧会话列表中查找
            clicked_filehelper = False
            for b in boxes_main:
                if b.left < left_limit_px and "文件传输助手" in b.text and b.top > 80:
                    sx, sy = cs_main.img_to_screen(b.center[0], b.center[1])
                    logger.info("在左侧会话列表中找到「文件传输助手」，点击坐标 (%d, %d)", sx, sy)
                    move_and_click(sx, sy)
                    clicked_filehelper = True
                    human_sleep(0.6, 0.9)
                    break

            # 左侧未找到才点击微信搜索框获取焦点后再输入
            if not clicked_filehelper:
                logger.info("正在定位并点击微信搜索框...")
                search_box = next((b for b in boxes_main if b.left < left_limit_px and b.top < 80 and any(k in b.text for k in ("搜索", "Search"))), None)
                if search_box:
                    search_x, search_y = cs_main.img_to_screen(search_box.center[0], search_box.center[1])
                else:
                    search_x = main_win.x + 160
                    search_y = main_win.y + 36

                logger.info("点击微信搜索框坐标 (%d, %d) 激活输入焦点...", search_x, search_y)
                move_and_click(search_x, search_y)
                human_sleep(0.3, 0.5)
                type_text_via_clipboard("文件传输助手", clear_first=True)
                human_sleep(0.8, 1.2)

                wins = list_wechat_windows()
                dropdown_win = None
                for w in wins:
                    if w.window_id != main_win.window_id and 180 <= w.w <= 520 and 150 <= w.h <= 750:
                        dropdown_win = w
                        break

                if dropdown_win:
                    img_drop = capture_window(dropdown_win)
                    drop_boxes = ocr(img_drop)
                    cs_drop = CoordSpace(dropdown_win.window_id, {"x": dropdown_win.x, "y": dropdown_win.y, "w": dropdown_win.w, "h": dropdown_win.h})
                    target_box = _find_dropdown_filehelper_box(drop_boxes)
                    if target_box:
                        sx, sy = cs_drop.img_to_screen(target_box.center[0], target_box.center[1])
                        logger.info("✅ 通过 OCR 在下拉窗口中定位到「功能 -> 文件传输助手」[%s]，点击坐标 (%d, %d)", target_box.text, sx, sy)
                        move_and_click(sx, sy)
                        clicked_filehelper = True
                        human_sleep(0.8, 1.2)
                else:
                    img_main2 = capture_window(main_win)
                    boxes2 = ocr(img_main2)
                    drop_cands = [b for b in boxes2 if b.left < left_limit_px + 100]
                    target_box = _find_dropdown_filehelper_box(drop_cands)
                    if target_box:
                        sx, sy = cs_main.img_to_screen(target_box.center[0], target_box.center[1])
                        logger.info("✅ 在主窗口浮层中定位到「功能 -> 文件传输助手」[%s]，点击坐标 (%d, %d)", target_box.text, sx, sy)
                        move_and_click(sx, sy)
                        clicked_filehelper = True
                        human_sleep(0.8, 1.2)

            # ──【层级 4】：严格验证已进入「文件传输助手」聊天页面，才允许发送或点击链接 ──
            human_sleep(0.5, 0.8)
            img_chat = capture_window(main_win)
            chat_boxes = ocr(img_chat)

            # 严格安全检查：右侧聊天顶部标题必须是「文件传输助手」
            is_in_filehelper = any("文件传输助手" in b.text and b.top < 80 for b in chat_boxes)
            if not is_in_filehelper:
                logger.warning("⚠️ 安全拦截：当前聊天窗口非「文件传输助手」，取消发送以防误发给他人！")
                return False

            link_box = None
            for b in reversed(chat_boxes):
                if b.left > right_start_px and any(k in b.text for k in ("5200", "batch-portal", "127.0.0.1", "mp-batch")):
                    link_box = b
                    break

            if link_box:
                sx, sy = cs_main.img_to_screen(link_box.center[0], link_box.center[1])
                logger.info("✅ 发现已有聚合页链接气泡 [%s]，直接点击坐标 (%d, %d)，无需重复发送", link_box.text, sx, sy)
                move_and_click(sx, sy)
            else:
                portal_url = "http://127.0.0.1:5200/api/auth/mp-batch-portal"
                logger.info("🚀 100%% 确认为「文件传输助手」界面，正在向输入框粘贴并发送聚合页链接...")
                # 点击右侧底部输入区域获取输入焦点
                input_x = main_win.x + int(main_win.w * 0.6)
                input_y = main_win.y + main_win.h - 80
                move_and_click(input_x, input_y)
                human_sleep(0.2, 0.4)
                type_text_via_clipboard(portal_url, clear_first=False)
                human_sleep(0.3, 0.5)
                press("return")
                human_sleep(0.8, 1.2)

                img_chat2 = capture_window(main_win)
                chat_boxes2 = ocr(img_chat2)
                for b in reversed(chat_boxes2):
                    if b.left > right_start_px and any(k in b.text for k in ("5200", "batch-portal", "127.0.0.1", "mp-batch")):
                        link_box = b
                        break

                if link_box:
                    sx, sy = cs_main.img_to_screen(link_box.center[0], link_box.center[1])
                    logger.info("✅ 已通过 OCR 定位到新发送的链接气泡 [%s]，点击坐标 (%d, %d)...", link_box.text, sx, sy)
                    move_and_click(sx, sy)
                else:
                    logger.warning("未能通过 OCR 定位到链接气泡，尝试点击右侧消息区域最后一条消息...")
                    fallback_x = main_win.x + int(main_win.w * 0.6)
                    fallback_y = main_win.y + main_win.h - 220
                    move_and_click(fallback_x, fallback_y)

        # ── 等待聚合页网页窗口打开，将其置顶并主动点击「一键开始批量授权」 ──
        human_sleep(1.2, 1.8)
        target_web_win = wait_new_web_window(before_web_ids, timeout=5.0)
        if not target_web_win:
            all_webs = find_web_windows()
            if all_webs:
                target_web_win = max(all_webs, key=lambda w: w.w * w.h)

        if target_web_win:
            _trigger_and_click_portal_button(target_web_win)

        return _wait_batch_portal_completion(sync_start_time, timeout_seconds)
    finally:
        _ui_flow_lock.release()


# ── Windows 检测与刷新 ────────────────────────────────

def refresh_wechat_pc_windows(keyword: str = None) -> bool:
    """Windows 下按需选择流水线：
    - 带 keyword → 定向流程（搜索该公众号 → 打开文章 → 捕获该 biz 专属凭证）；
    - 无 keyword → 通用三级策略流水线（刷新已有文章窗口 → 订阅号消息点文章）；
    两者失败时均降级为旧的 uiautomation SendKeys 方案。"""
    # 1a. 定向：为指定公众号建立/续期独立会话
    if keyword:
        try:
            from windows.win_flow import run_targeted_account_flow_windows
            if run_targeted_account_flow_windows(keyword=keyword, auto_cleanup=True):
                logger.info("✅ 已通过 Windows 定向流水线为 [%s] 刷新凭证！", keyword)
                return True
            logger.warning("⚠️ Windows 定向流水线未捕获 [%s] 的凭证，降级为通用方案...", keyword)
        except ImportError as e:
            logger.warning("⚠️ windows 模块不可用 (%s)，降级为通用方案...", e)
        except Exception as e:
            logger.warning("⚠️ Windows 定向流水线执行异常: %s，降级为通用方案...", e)

    # 1b. 通用：三级策略流水线（不占用真实鼠标，参考 Access_wechat_article）
    try:
        from windows.win_flow import run_wechat_pc_flow_windows
        success = run_wechat_pc_flow_windows(auto_cleanup=True)
        if success:
            logger.info("✅ 已通过 Windows UI 自动化流水线成功触发凭证刷新！")
            return True
        logger.warning("⚠️ Windows 自动化流水线未捕获新凭证，降级为 SendKeys 备用方案...")
    except ImportError as e:
        logger.warning("⚠️ windows 模块不可用 (%s)，降级为 SendKeys 备用方案...", e)
    except Exception as e:
        logger.warning("⚠️ windows 自动化流水线执行异常: %s，降级为 SendKeys 备用方案...", e)

    # 2. 兜底：旧的 uiautomation SendKeys 方案（保留原有行为）
    return _legacy_refresh_windows_sendkeys()


def _legacy_refresh_windows_sendkeys() -> bool:
    """旧方案：使用 uiautomation 查找公众号/文章窗口并激活，发送 F5/Ctrl+R。"""
    logger.info("正在尝试使用 uiautomation 在 Windows 上查找微信客户端窗口...")
    try:
        import uiautomation as auto
    except ImportError:
        logger.error("❌ Windows 环境需安装 uiautomation 库 (pip install uiautomation)")
        return False

    try:
        root = auto.GetRootControl()
        target_win = None
        candidates = ["公众号", "服务号", "订阅号", "微信", "WeChat"]

        for win in root.GetChildren():
            win_name = win.Name or ""
            if any(cand in win_name for cand in candidates):
                target_win = win
                break

        if not target_win:
            logger.warning("⚠️ 未在 Windows 桌面上找到微信相关窗口。")
            return False

        logger.info(f"找到微信窗口: '{target_win.Name}', 尝试激活并发送刷新快捷键 (F5/Ctrl+R)...")
        target_win.SetFocus()
        time.sleep(0.5)
        auto.SendKeys("{F5}")
        time.sleep(0.5)
        auto.SendKeys("{Ctrl}r")
        logger.info("✅ 已成功向 Windows 微信客户端窗口发送刷新指令。")
        return True
    except Exception as e:
        logger.error("❌ Windows 窗口刷新执行异常: %s", e)
        return False


# ── 触发入口（全局 UI 互斥 + 触发合并 + 防刷冷却） ────

def trigger_pc_wechat_refresh(keyword: str = None, force: bool = False) -> bool:
    """根据操作系统自动选择对应的 PC 微信刷新方案。

    - keyword 指定时：定向搜索该公众号刷新；
    - keyword 未指定时：macOS 默认走批量授权聚合页，一次性同步所有公众号凭证；
    - 全局互斥：同一时刻只允许一个 UI 流程在跑，其余触发直接跳过；
    - 触发合并：成功刷新后 SUCCESS_COALESCE_SECONDS 内的通用触发直接跳过；
    - 防刷冷却：MIN_REFRESH_INTERVAL 内不重复触发；
    - force=True：绕过冷却与合并，但仍然遵守全局互斥。
    """
    global _last_refresh_time, _last_success_time
    now = time.time()

    if not force:
        if not ENABLE_BACKGROUND_ACTIVE_REFRESH:
            logger.debug("主动自动化刷新已禁用，跳过本次自动触发。")
            return False
        if now - _last_refresh_time < MIN_REFRESH_INTERVAL:
            remaining = int(MIN_REFRESH_INTERVAL - (now - _last_refresh_time))
            logger.info("⏳ 处于自动刷新保护冷却期（剩余 %d 秒），跳过本次触发...", remaining)
            return False
        if _last_success_time and now - _last_success_time < SUCCESS_COALESCE_SECONDS:
            remaining = int(SUCCESS_COALESCE_SECONDS - (now - _last_success_time))
            logger.info("⏳ %d 秒内已有成功刷新，合并本次重复触发（剩余 %d 秒）...", SUCCESS_COALESCE_SECONDS, remaining)
            return False

    _last_refresh_time = now
    current_os = platform.system()

    if current_os == "Darwin":
        if keyword:
            # 用户明确指定单个关键词时，走单号搜一搜流程
            if not _ui_flow_lock.acquire(blocking=False):
                logger.info("🔒 另一个 UI 自动化流程正在执行中，本次触发跳过（互斥保护）。")
                return False
            try:
                ok = refresh_wechat_pc_macos(keyword=keyword)
                if ok:
                    _last_success_time = time.time()
                return ok
            finally:
                _ui_flow_lock.release()
        else:
            # 未指定关键词时，默认使用聚合页批量同步全部公众号主页凭证
            ok = run_batch_portal_flow_macos()
            if ok:
                _last_success_time = time.time()
            return ok

    elif current_os == "Windows":
        if not _ui_flow_lock.acquire(blocking=False):
            logger.info("🔒 另一个 UI 自动化流程正在执行中，本次触发跳过（互斥保护）。")
            return False
        try:
            ok = refresh_wechat_pc_windows(keyword=keyword)
            if ok:
                _last_success_time = time.time()
            return ok
        finally:
            _ui_flow_lock.release()

    else:
        logger.warning("⚠️ 操作系统 [%s] 暂不支持自动操控 PC 客户端。", current_os)
        return False


# ── 主动续期 worker：队列优先 → biz 老化 → 全局兜底 ──

def _next_refresh_target():
    """选出下一个需要 UI 续期的公众号目标。
    优先级：① 刷新队列中采集失败的号（最紧迫）→ ② 订阅中 biz 凭证缺失或最老且超阈值的号。
    返回 {"biz","name","reason","attempts"} 或 None。"""

    # 1) 失败入队优先（"自动跳过 + 及时补凭证"）
    try:
        from backend.refresh_queue import refresh_queue
        item = refresh_queue.pop()
        if item:
            # 无名称无法定向搜索，尝试从订阅列表反查
            if not item.get("name"):
                item["name"] = _resolve_name_by_biz(item.get("biz", ""))
                if not item["name"]:
                    logger.warning("刷新队列中的 biz=%s 无法解析公众号名称，跳过（无法定向搜索）",
                                   (item.get("biz") or "")[:14])
                    return None
            return item
    except Exception as e:
        logger.debug("读取刷新队列失败: %s", e)

    # 2) 主动续期：订阅中 biz 凭证缺失（优先）或年龄超阈值（取最老）的号
    #    刚尝试过且仍在退避期内的号跳过，防止不可搜索的号反复霸占名额饿死其他号
    try:
        from backend.accounts import _load_accounts
        from backend.account_pool import account_pool
        now = time.time()
        best = None  # (无凭证优先, 年龄, name, biz)
        for acc in _load_accounts():
            fakeid = acc.get("fakeid") or acc.get("alias")
            name = acc.get("nickname") or acc.get("name") or ""
            if not fakeid or not name:
                continue
            if now - _proactive_last_attempt.get(fakeid, 0) < PROACTIVE_RETRY_BACKOFF:
                continue
            age = account_pool.get_biz_age(fakeid)
            if age is None:
                cand = (0, float("inf"), name, fakeid)   # 从未建立会话，最优先
            elif age > PROACTIVE_RENEW_THRESHOLD:
                cand = (1, age, name, fakeid)
            else:
                continue
            # 无凭证（优先级 0）永远排在仅老化（优先级 1）之前；同级取年龄最老的
            if best is None or cand[0] < best[0] or (cand[0] == best[0] and cand[1] > best[1]):
                best = cand
        if best:
            return {"biz": best[3], "name": best[2], "reason": "proactive", "attempts": 0}
    except Exception as e:
        logger.debug("主动续期扫描失败: %s", e)

    return None


def _resolve_name_by_biz(biz: str) -> str:
    """按 fakeid 反查公众号名称（从订阅列表）。"""
    try:
        from backend.accounts import _load_accounts
        for acc in _load_accounts():
            if (acc.get("fakeid") or acc.get("alias")) == biz:
                return acc.get("nickname") or acc.get("name") or ""
    except Exception:
        pass
    return ""


def _process_refresh_target(target: dict) -> bool:
    """执行一次定向凭证续期（绕过通用冷却/合并，但遵守全局 UI 互斥）。"""
    keyword = target.get("name") or ""
    if target.get("biz"):
        _proactive_last_attempt[target["biz"]] = time.time()  # 主动续期退避计时
    logger.info("🎯 定向凭证续期: [%s] (原因: %s, 第 %d 次尝试)",
                keyword, target.get("reason", "-"), max(1, target.get("attempts", 1)))
    return trigger_pc_wechat_refresh(keyword=keyword, force=True)


def _refresh_worker_loop():
    """保活 worker 主循环（单线程串行，天然与 UI 互斥配合）：
    macOS 优先通过聚合页批量同步全部公众号凭证，不再单独逐个搜公众号；
    启动静默 WORKER_STARTUP_SILENCE 秒。"""
    logger.info("🛡️ 凭证保活 worker 已就绪（启动静默: %d 秒，主动续期阈值: %d 分钟）",
                WORKER_STARTUP_SILENCE, PROACTIVE_RENEW_THRESHOLD // 60)
    time.sleep(WORKER_STARTUP_SILENCE)

    while True:
        if not ENABLE_BACKGROUND_ACTIVE_REFRESH:
            time.sleep(60)
            continue

        current_os = platform.system()
        target = None
        try:
            target = _next_refresh_target()
        except Exception as e:
            logger.error("获取刷新目标异常: %s", e)

        if target:
            if current_os == "Darwin":
                # macOS 下统一使用聚合页批量同步所有公众号凭证，避免逐个搜一搜干扰用户
                logger.info("🎯 检测到需要刷新凭证 (目标: %s, 原因: %s)，启动聚合页批量授权流程...",
                            target.get("name", "-"), target.get("reason", "-"))
                ok = False
                try:
                    ok = run_batch_portal_flow_macos()
                except Exception as e:
                    logger.error("macOS 聚合页批量授权异常: %s", e)

                if ok:
                    # 批量刷新成功，等待下一个检查周期
                    time.sleep(IDLE_CHECK_INTERVAL)
                else:
                    # 如果未完成或未开微信，等待较长退避时间，避免频繁打扰
                    time.sleep(180)
            else:
                ok = False
                try:
                    ok = _process_refresh_target(target)
                except Exception as e:
                    logger.error("定向续期执行异常: %s", e)
                if not ok and target.get("reason") != "proactive":
                    try:
                        from backend.refresh_queue import refresh_queue
                        refresh_queue.requeue(target)
                    except Exception as e:
                        logger.debug("刷新目标回队失败: %s", e)
                time.sleep(30)
        else:
            try:
                _check_and_refresh_stale_accounts()
            except Exception as e:
                logger.error("全局兜底巡检异常: %s", e)
            time.sleep(IDLE_CHECK_INTERVAL)


def _check_and_refresh_stale_accounts():
    """全局兜底巡检：账号池中存在凭证超过 STALE_THRESHOLD 未更新的 active 账号时，
    主动触发一次 PC 微信 UI 刷新（macOS 走批量聚合页，Windows 走窗口刷新）。"""
    try:
        from backend.account_pool import account_pool
    except ImportError:
        logger.debug("无法导入 account_pool，跳过本次检测。")
        return

    now = time.time()
    accounts = account_pool._load()
    stale_found = False

    for acc in accounts:
        if acc.get("status") != "active":
            continue
        save_time = acc.get("save_time", 0)
        if save_time and (now - save_time) > STALE_THRESHOLD:
            age_minutes = int((now - save_time) / 60)
            nickname = acc.get("nickname", "未命名")
            logger.info(
                "🔍 检测到账号 [%s] 凭证已 %d 分钟未更新（阈值 %d 分钟），准备主动刷新...",
                nickname, age_minutes, STALE_THRESHOLD // 60
            )
            stale_found = True
            break

    if not stale_found:
        return

    # 触发批量刷新（macOS 下无 keyword 会自动走聚合页）
    trigger_pc_wechat_refresh()


def start_background_refresh_daemon():
    """启动后台凭证保活 worker（全局只启动一次）。"""
    global _daemon_started
    with _daemon_lock:
        if _daemon_started:
            logger.debug("保活 worker 已在运行，跳过重复启动。")
            return
        _daemon_started = True

    t = threading.Thread(target=_refresh_worker_loop, daemon=True, name="credential-refresh-worker")
    t.start()
    logger.info("🚀 凭证保活 worker 已启动（macOS 聚合页批量授权 + Windows 自动化 + 全局互斥）。")


if __name__ == "__main__":
    success = trigger_pc_wechat_refresh()
    if success:
        print("\n[Auto Refresh] 客户端指令已发送，请检查 mitm_proxy 是否捕获到最新凭证！")
    else:
        print("\n[Auto Refresh] 自动刷新触发完成或受权限限制。")
