"""macOS 微信窗口查找、激活与后台截图。"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

import numpy as np
import Quartz
from AppKit import NSWorkspace

WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
WECHAT_OWNER_NAMES = {"WeChat", "微信"}


@dataclass
class MacWindow:
    """一个窗口的完整描述。bounds 单位是「点 (point)」。"""
    window_id: int
    title: str
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def __repr__(self) -> str:
        return f"MacWindow(id={self.window_id}, title={self.title!r}, bounds=({self.x},{self.y},{self.w},{self.h}))"


# ---------------------------------------------------------------- 枚举与查找

def list_wechat_windows(min_size: int = 100) -> list[MacWindow]:
    """枚举当前屏幕上所有微信相关的有效窗口。"""
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    out = []
    for w in infos:
        if w.get("kCGWindowOwnerName") not in WECHAT_OWNER_NAMES:
            continue
        b = w.get("kCGWindowBounds", {})
        width = b.get("Width", 0)
        height = b.get("Height", 0)
        if width < min_size or height < min_size:
            continue
        out.append(MacWindow(
            window_id=w["kCGWindowNumber"],
            title=w.get("kCGWindowName", "") or "",
            x=int(b.get("X", 0)),
            y=int(b.get("Y", 0)),
            w=int(width),
            h=int(height),
        ))
    return out


def find_main_window(timeout: float = 1.0) -> MacWindow | None:
    """
    主窗口判定：
    1. 标题为「微信」或「WeChat」；
    2. 微信 4.x 个别场景主窗口标题为空，退化为取「宽度在 700~1800 之间且高度>=500」的最大窗口。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = list_wechat_windows()
        for w in wins:
            if w.title in ("微信", "WeChat"):
                return w
        cands = [w for w in wins if 700 <= w.w <= 1800 and w.h >= 500]
        if cands:
            return max(cands, key=lambda w: w.w * w.h)
        time.sleep(0.2)
    return None


def find_web_windows(exclude_ids: set[int] | None = None) -> list[MacWindow]:
    """
    公众号/文章/搜一搜窗口判定：不是主窗口的其他微信窗口。
    微信 4.x 中文章页与搜一搜 Web 窗口通常是独立窗口。
    """
    main = find_main_window(timeout=0.5)
    exclude = set(exclude_ids or set())
    if main:
        exclude.add(main.window_id)
    return [w for w in list_wechat_windows() if w.window_id not in exclude]


def wait_new_web_window(before_ids: set[int], timeout: float = 12.0) -> MacWindow | None:
    """等待新增的 Web 窗口出现（如搜一搜、文章详情等）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        news = [w for w in find_web_windows() if w.window_id not in before_ids]
        if news:
            return max(news, key=lambda w: w.w * w.h)
        time.sleep(0.4)
    return None


# ---------------------------------------------------------------- 激活

def activate_wechat() -> None:
    """把微信 App 置前；若未运行则拉起。"""
    ws = NSWorkspace.sharedWorkspace()
    for app in ws.runningApplications():
        if app.bundleIdentifier() == WECHAT_BUNDLE_ID:
            app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
            return
    subprocess.run(["open", "-a", "WeChat"], check=False)


def launch_wechat() -> None:
    """拉起微信 App。"""
    subprocess.run(["open", "-a", "WeChat"], check=False)


# ---------------------------------------------------------------- 截图（后台可截）

def capture_window(win: MacWindow) -> np.ndarray:
    """
    按窗口 ID 截图，返回 BGR ndarray（像素坐标系，Retina 下通常为 2x 分辨率）。
    窗口被遮挡也能截到正确内容。
    """
    img_ref = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        win.window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming
        | Quartz.kCGWindowImageNominalResolution,
    )
    if img_ref is None:
        raise RuntimeError(f"截图失败 window_id={win.window_id}（请检查终端是否授予'屏幕录制'权限）")

    w = Quartz.CGImageGetWidth(img_ref)
    h = Quartz.CGImageGetHeight(img_ref)
    bpr = Quartz.CGImageGetBytesPerRow(img_ref)
    provider = Quartz.CGImageGetDataProvider(img_ref)
    data = Quartz.CGDataProviderCopyData(provider)

    arr = np.frombuffer(data, dtype=np.uint8)
    arr = arr.reshape((h, bpr // 4, 4))[:, :w, :]
    bgr = arr[:, :, [2, 1, 0]].copy()   # BGRA -> BGR
    return bgr
