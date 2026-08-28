"""步骤 4: 清理因自动化产生的 Web 窗口并恢复微信主界面。"""
from __future__ import annotations

import logging
from mac.mac_win import find_web_windows, activate_wechat, find_main_window
from mac.mac_as import press_escape, close_front_window
from mac.mac_input import human_sleep

logger = logging.getLogger("wechat_auto_mac.step4")


def cleanup_mac_wechat_windows(max_rounds: int = 6) -> None:
    """关闭所有弹出的 Web / 搜一搜 / 文章详情窗口（含多标签），并重置搜索状态。"""
    for _ in range(max_rounds):
        web_wins = find_web_windows()
        if not web_wins:
            break
        logger.info("当前仍有 %d 个 Web 窗口/标签，正在关闭当前前置窗口...", len(web_wins))
        activate_wechat()
        close_front_window()
        human_sleep(0.3, 0.6)

    # 激活主窗口并发送 Esc 退出搜索输入状态
    main = find_main_window(timeout=1.0)
    if main:
        activate_wechat()
        press_escape(times=2)
    logger.info("✅ 自动化窗口清理完成")
