# -*- coding: utf-8 -*-
"""
微信视频号视频在线解析与下载模块
"""

import os
import re
import shutil
import socket
import sys
import time
import json
import random
import threading
import subprocess
import urllib.parse
import zipfile
from email.utils import formatdate
from xml.sax.saxutils import escape as xml_escape
import requests
import struct
import urllib3
from pathlib import Path
from flask import Blueprint, Response, jsonify, request

from backend.config import DATA_DIR, OUTPUT_DIR, get_settings, load_json, save_json
from backend.runtime import launch_chromium

channels_bp = Blueprint("channels", __name__, url_prefix="/api/channels")
CHANNELS_HISTORY_FILE = DATA_DIR / "channels_history.json"
CHANNELS_FAVORITES_FILE = DATA_DIR / "channels_favorites.json"
CHANNELS_FEEDS_FILE = DATA_DIR / "channels_parsed_feeds.json"

# 保护 channels_parsed_feeds.json 的读-改-写。采集(save_synced_feeds)与自动上传
# (process_pending_uploads)分属不同线程,各自 load→改→save 会后写覆盖先写,
# 造成新作品丢失 / uploaded 标记被抹掉后重复上传。所有 feeds_db 写入须持此锁。
FEEDS_LOCK = threading.RLock()
_HISTORY_LOCK = threading.RLock()


class ISAAC64:
    def __init__(self, key_uint64: int):
        self.rand_cnt = 255
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.seed = [0] * 256
        self.mm = [0] * 256
        self.seed[0] = key_uint64
        self.rand64_init()

    def mix(self, a, b, c, d, e, f, g, h):
        mask = 0xFFFFFFFFFFFFFFFF
        a = (a - e) & mask
        f = (f ^ (h >> 9)) & mask
        h = (h + a) & mask
        b = (b - f) & mask
        g = (g ^ ((a << 9) & mask)) & mask
        a = (a + b) & mask
        c = (c - g) & mask
        h = (h ^ (b >> 23)) & mask
        b = (b + c) & mask
        d = (d - h) & mask
        a = (a ^ ((c << 15) & mask)) & mask
        c = (c + d) & mask
        e = (e - a) & mask
        b = (b ^ (d >> 14)) & mask
        d = (d + e) & mask
        f = (f - b) & mask
        c = (c ^ ((e << 20) & mask)) & mask
        e = (e + f) & mask
        g = (g - c) & mask
        d = (d ^ (f >> 17)) & mask
        f = (f + g) & mask
        h = (h - d) & mask
        e = (e ^ ((g << 14) & mask)) & mask
        g = (g + h) & mask
        return a, b, c, d, e, f, g, h

    def rand64_init(self):
        golden = 0x9e3779b97f4a7c13
        a = b = c = d = e = f = g = h = golden
        for _ in range(4):
            a, b, c, d, e, f, g, h = self.mix(a, b, c, d, e, f, g, h)
        for i in range(0, 256, 8):
            a = (a + self.seed[i]) & 0xFFFFFFFFFFFFFFFF
            b = (b + self.seed[i+1]) & 0xFFFFFFFFFFFFFFFF
            c = (c + self.seed[i+2]) & 0xFFFFFFFFFFFFFFFF
            d = (d + self.seed[i+3]) & 0xFFFFFFFFFFFFFFFF
            e = (e + self.seed[i+4]) & 0xFFFFFFFFFFFFFFFF
            f = (f + self.seed[i+5]) & 0xFFFFFFFFFFFFFFFF
            g = (g + self.seed[i+6]) & 0xFFFFFFFFFFFFFFFF
            h = (h + self.seed[i+7]) & 0xFFFFFFFFFFFFFFFF
            a, b, c, d, e, f, g, h = self.mix(a, b, c, d, e, f, g, h)
            self.mm[i] = a; self.mm[i+1] = b; self.mm[i+2] = c; self.mm[i+3] = d
            self.mm[i+4] = e; self.mm[i+5] = f; self.mm[i+6] = g; self.mm[i+7] = h
        for i in range(0, 256, 8):
            a = (a + self.mm[i]) & 0xFFFFFFFFFFFFFFFF
            b = (b + self.mm[i+1]) & 0xFFFFFFFFFFFFFFFF
            c = (c + self.mm[i+2]) & 0xFFFFFFFFFFFFFFFF
            d = (d + self.mm[i+3]) & 0xFFFFFFFFFFFFFFFF
            e = (e + self.mm[i+4]) & 0xFFFFFFFFFFFFFFFF
            f = (f + self.mm[i+5]) & 0xFFFFFFFFFFFFFFFF
            g = (g + self.mm[i+6]) & 0xFFFFFFFFFFFFFFFF
            h = (h + self.mm[i+7]) & 0xFFFFFFFFFFFFFFFF
            a, b, c, d, e, f, g, h = self.mix(a, b, c, d, e, f, g, h)
            self.mm[i] = a; self.mm[i+1] = b; self.mm[i+2] = c; self.mm[i+3] = d
            self.mm[i+4] = e; self.mm[i+5] = f; self.mm[i+6] = g; self.mm[i+7] = h
        self.isaac64()

    def isaac64(self):
        self.cc = (self.cc + 1) & 0xFFFFFFFFFFFFFFFF
        self.bb = (self.bb + self.cc) & 0xFFFFFFFFFFFFFFFF
        for i in range(256):
            rem = i % 4
            if rem == 0:
                self.aa = (~(self.aa ^ (self.aa << 21))) & 0xFFFFFFFFFFFFFFFF
            elif rem == 1:
                self.aa = (self.aa ^ (self.aa >> 5)) & 0xFFFFFFFFFFFFFFFF
            elif rem == 2:
                self.aa = (self.aa ^ (self.aa << 12)) & 0xFFFFFFFFFFFFFFFF
            elif rem == 3:
                self.aa = (self.aa ^ (self.aa >> 33)) & 0xFFFFFFFFFFFFFFFF
            self.aa = (self.aa + self.mm[(i + 128) % 256]) & 0xFFFFFFFFFFFFFFFF
            x = self.mm[i]
            y = (self.mm[(x >> 3) % 256] + self.aa + self.bb) & 0xFFFFFFFFFFFFFFFF
            self.mm[i] = y
            self.bb = (self.mm[(y >> 11) % 256] + x) & 0xFFFFFFFFFFFFFFFF
            self.seed[i] = self.bb

    def isaac_random(self) -> int:
        result = self.seed[self.rand_cnt]
        if self.rand_cnt == 0:
            self.isaac64()
            self.rand_cnt = 255
        else:
            self.rand_cnt -= 1
        return result


def decrypt_channels_data(data: bytearray, key: int, enc_len: int = 131072):
    if len(data) == 0:
        return
    actual_enc_len = min(len(data), enc_len)
    aa_inst = ISAAC64(key)
    for i in range(0, actual_enc_len, 8):
        rand_num = aa_inst.isaac_random()
        temp_bytes = struct.pack(">Q", rand_num)
        for j in range(8):
            idx = i + j
            if idx >= actual_enc_len:
                return
            data[idx] ^= temp_bytes[j]



def add_channels_history_item(
    title: str,
    item_type: str,
    file_path: str,
    size_bytes: int,
    feed_id: str = None,
    uploaded: bool = None,
    cos_url: str = None,
    upload_error: str = None,
):
    """保存下载记录到视频号历史记录文件（兼容云端自动上传扩展字段）"""
    from backend.config import load_json, save_json
    entry = {
        "title": title,
        "type": item_type,
        "path": str(file_path),
        "size": f"{size_bytes / (1024 * 1024):.2f} MB" if size_bytes else "未知",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if feed_id is not None:
        entry["feed_id"] = feed_id
    if uploaded is not None:
        entry["uploaded"] = bool(uploaded)
    if cos_url is not None:
        entry["cos_url"] = cos_url
    if upload_error is not None:
        entry["upload_error"] = upload_error

    with _HISTORY_LOCK:
        history = load_json(CHANNELS_HISTORY_FILE, [])
        history.insert(0, entry)
        save_json(CHANNELS_HISTORY_FILE, history[:150])


def sanitize_filename(desc: str, createtime: str, suffix: str = ".mp4") -> str:
    """清理并生成合法的文件名"""
    if not suffix:
        suffix = ".mp4"
    elif not suffix.startswith("."):
        suffix = "." + suffix

    if desc:
        clean = re.sub(r'[\\/:*?"<>|\r\n]', "", desc).strip()
        if clean:
            return clean[:120] + suffix
    if createtime:
        try:
            from datetime import datetime
            d = datetime.fromtimestamp(int(createtime))
            return f"video_{d.strftime('%Y%m%d_%H%M%S')}{suffix}"
        except Exception:
            pass
    return f"video{suffix}"


def _unique_filepath(directory: Path, filename: str) -> Path:
    """若目标文件已存在，自动追加 _1/_2 序号避免覆盖。"""
    filepath = directory / filename
    if not filepath.exists():
        return filepath
    base_name = filepath.stem
    ext = filepath.suffix
    counter = 1
    while filepath.exists():
        filepath = directory / f"{base_name}_{counter}{ext}"
        counter += 1
    return filepath


class _IPv4ConnMixin:
    """强制通过 AF_INET 建立 TCP 连接，同时保留原 hostname 用于 TLS SNI 与证书校验。"""

    def _new_conn(self) -> socket.socket:
        try:
            ipv4_infos = socket.getaddrinfo(
                self._dns_host, self.port, socket.AF_INET, socket.SOCK_STREAM
            )
        except socket.gaierror as e:
            raise urllib3.exceptions.NameResolutionError(self.host, self, e) from e

        err = None
        for af, socktype, proto, _, sa in ipv4_infos:
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if self.socket_options:
                    for opt in self.socket_options:
                        sock.setsockopt(*opt)
                if self.timeout is not None and self.timeout is not urllib3.util.connection._DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sa)
                return sock
            except socket.timeout as e:
                if sock is not None:
                    sock.close()
                raise urllib3.exceptions.ConnectTimeoutError(
                    self, f"Connection to {self.host} timed out. (connect timeout={self.timeout})"
                ) from e
            except OSError as e:
                err = e
                if sock is not None:
                    sock.close()
        raise urllib3.exceptions.NewConnectionError(
            self, f"Failed to establish IPv4 connection to {self.host}: {err}"
        )


class _IPv4HTTPConnection(_IPv4ConnMixin, urllib3.connection.HTTPConnection):
    pass


class _IPv4HTTPSConnection(_IPv4ConnMixin, urllib3.connection.HTTPSConnection):
    pass


class _IPv4HTTPConnectionPool(urllib3.HTTPConnectionPool):
    ConnectionCls = _IPv4HTTPConnection


class _IPv4HTTPSConnectionPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = _IPv4HTTPSConnection


class _IPv4HTTPAdapter(requests.adapters.HTTPAdapter):
    """仅限当前 Session 的 IPv4 适配器，无全局 socket monkeypatch 副作用。"""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _IPv4HTTPConnectionPool,
            "https": _IPv4HTTPSConnectionPool,
        }


def open_cdn_response(url: str, headers=None, stream: bool = True, timeout: int = 60):
    """直连 CDN 发起 GET 请求；遇 IPv6 TLS 证书/握手异常时自动回退 IPv4 重试。"""
    req_headers = headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }

    def _do_get(ipv4_only: bool):
        sess = requests.Session()
        sess.trust_env = False
        if ipv4_only:
            adapter = _IPv4HTTPAdapter()
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
        try:
            resp = sess.get(
                url,
                headers=req_headers,
                proxies={"http": None, "https": None},
                stream=stream,
                timeout=timeout,
            )
            resp.raise_for_status()
            return sess, resp
        except Exception:
            sess.close()
            raise

    try:
        return _do_get(ipv4_only=False)
    except requests.exceptions.SSLError as ssl_err:
        print(f"[Channels CDN] SSL error on default stack ({ssl_err}), retrying via IPv4...", flush=True)
        return _do_get(ipv4_only=True)


def generate_rid() -> str:
    """生成微信视频号 API 所需的 _rid 参数"""
    timestamp_hex = hex(int(time.time()))[2:]
    random_hex = "".join(random.choices("0123456789abcdef", k=8))
    return f"{timestamp_hex}-{random_hex}"


def local_parse_with_yuanbao(share_url: str, cookie: str) -> dict:
    """100% 软件内部本地解析（调用腾讯元宝 + 微信视频号协议接口）"""
    # ---- 步骤 1: 调用腾讯元宝解析分享链接以获取 exportId 和 generalToken ----
    parse_url = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
    parse_headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": "https://yuanbao.tencent.com",
        "referer": "https://yuanbao.tencent.com/chat",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "cookie": cookie,
        "x-source": "web"
    }
    payload1 = {
        "type": "video_channel_url",
        "url": share_url,
        "scene": 1
    }
    
    r1 = requests.post(parse_url, json=payload1, headers=parse_headers, timeout=15)
    if r1.status_code != 200:
        raise RuntimeError(f"腾讯元宝接口返回异常 (HTTP {r1.status_code})")
        
    res1 = r1.json()
    
    # ── 写入深度诊断日志 ──
    import json
    from backend.config import DATA_DIR
    log_file = DATA_DIR / "parse_debug.log"
    try:
        with open(log_file, "a", encoding="utf-8") as f_log:
            f_log.write(f"\n=== [{time.strftime('%Y-%m-%d %H:%M:%S')}] 元宝 API 请求与响应 ===\n")
            f_log.write(f"请求 URL: {share_url}\n")
            f_log.write(f"元宝响应 JSON: {json.dumps(res1, ensure_ascii=False)}\n")
    except Exception:
        pass
        
    data = res1.get("data")
    if res1.get("code") != 0 or not data or not data.get("playable_url"):
        err_msg = res1.get("msg") or res1.get("error") or "元宝登录 Cookie 可能已失效，请重新登录获取"
        raise RuntimeError(f"腾讯元宝接口返回异常: {err_msg}")
        
    playable_url = data.get("playable_url")
    
    # 从元宝返回的直链参数中提取 eid 和 token
    parsed_query = urllib.parse.parse_qs(urllib.parse.urlparse(playable_url).query)
    general_token = parsed_query.get("token", [""])[0]
    export_id = parsed_query.get("eid", [""])[0]
    
    if not general_token or not export_id:
        raise RuntimeError("未能从元宝响应中获取有效的 Token 或 ExportId")

    # ---- 步骤 2: 使用提取出的凭证直接向微信视频号预览服务器获取视频详细数据 ----
    feed_info_url = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
    rid = generate_rid()
    api_url = f"{feed_info_url}?_rid={rid}&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed"
    
    referer = (
        f"https://channels.weixin.qq.com/finder-preview/pages/feed"
        f"?entry_card_type=48&comment_scene=39&appid=0"
        f"&token={urllib.parse.quote(general_token)}"
        f"&entry_scene=0&eid={urllib.parse.quote(export_id)}"
    )
    
    feed_headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": referer,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    payload2 = {
        "baseReq": {"generalToken": general_token},
        "exportId": export_id
    }
    
    r2 = requests.post(api_url, json=payload2, headers=feed_headers, timeout=15)
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"微信视频号接口请求异常 (HTTP {r2.status_code})")

    res2 = r2.json()
    if isinstance(res2, dict) and res2.get("errCode", 0) != 0:
        raw_err = str(res2.get("errMsg") or (res2.get("data") or {}).get("errMsg") or "")
        clean_err = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_err)).strip()
        raise RuntimeError(clean_err or f"视频号接口返回错误 (errCode={res2.get('errCode')})")
    return res2


# ── 自动获取元宝 Cookie (Playwright 联动) ─────────────────────────
import threading

_cookie_task = {
    "status": "idle",
    "cookie": None,
    "error": None
}
_cookie_lock = threading.Lock()
_cookie_thread = None
_cookie_task_started_at = 0.0


def _do_cookie_acquisition():
    global _cookie_task
    from playwright.sync_api import sync_playwright
    import time
    from backend.config import save_settings
    
    with _cookie_lock:
        _cookie_task = {
            "status": "running",
            "cookie": None,
            "error": None
        }
        
    try:
        print("[元宝扫码] 正在启动 Playwright Chromium 浏览器...", flush=True)
        with sync_playwright() as p:
            browser = launch_chromium(p.chromium, headless=False)
            print("[元宝扫码] 浏览器启动成功，正在打开腾讯元宝页面...", flush=True)
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            try:
                page.goto("https://yuanbao.tencent.com/", wait_until="domcontentloaded", timeout=30000)
            except Exception as ep:
                print(f"[元宝扫码] 页面加载提示: {ep}", flush=True)
            
            cookie_captured = None
            start_time = time.time()
            
            # 最大等待 5 分钟 (300秒)
            while time.time() - start_time < 300:
                if page.is_closed():
                    break
                    
                cookies = context.cookies()
                # 检查元宝核心登录凭证 Cookie 'hy_token'
                has_token = any(c['name'] == 'hy_token' for c in cookies)
                
                if has_token:
                    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                    cookie_captured = cookie_str
                    print("[元宝扫码] 成功检测到 hy_token 登录态！", flush=True)
                    break
                    
                time.sleep(1.5)
                
            # 冗余读取：如果窗口被关掉，做最后一次检查
            if not cookie_captured:
                cookies = context.cookies()
                has_token = any(c['name'] == 'hy_token' for c in cookies)
                if has_token or len(cookies) > 5:
                    cookie_captured = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                    
            try:
                browser.close()
            except Exception:
                pass
            
            if cookie_captured:
                # 抓取成功，直接保存到配置文件中
                settings = get_settings()
                settings["yuanbao_cookie"] = cookie_captured
                save_settings(settings)
                print("[元宝扫码] Cookie 已成功保存到系统设置中！", flush=True)
                
                with _cookie_lock:
                    _cookie_task = {
                        "status": "success",
                        "cookie": cookie_captured,
                        "error": None
                    }
            else:
                with _cookie_lock:
                    _cookie_task = {
                        "status": "failed",
                        "cookie": None,
                        "error": "未检测到微信扫码登录状态，请确保成功登录并进入腾讯元宝对话页"
                    }
                    
    except Exception as e:
        err_str = str(e)
        if "closed" in err_str.lower() or "target page" in err_str.lower():
            err_str = "浏览器窗口已被关闭，自动获取已取消。"
        print(f"[元宝扫码] 异常: {err_str}", flush=True)
        with _cookie_lock:
            _cookie_task = {
                "status": "failed",
                "cookie": None,
                "error": err_str
            }


@channels_bp.route("/start_cookie_acquisition", methods=["POST"])
def start_cookie_acquisition():
    """启动本地 Playwright 浏览器，引导用户登录腾讯元宝并自动截获 Cookie"""
    global _cookie_thread, _cookie_task_started_at
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))

    with _cookie_lock:
        is_alive = _cookie_thread and _cookie_thread.is_alive()
        is_timeout = (time.time() - _cookie_task_started_at) > 300
        if _cookie_task["status"] == "running" and is_alive and not is_timeout and not force:
            return jsonify({"message": "抓取任务已在运行中，请在已打开的浏览器窗口中扫码..."}), 200

        _cookie_task = {
            "status": "running",
            "cookie": None,
            "error": None
        }
        _cookie_task_started_at = time.time()
            
    _cookie_thread = threading.Thread(target=_do_cookie_acquisition, daemon=True)
    _cookie_thread.start()
    return jsonify({"message": "已成功唤起本地浏览器，请在弹出的窗口中完成微信扫码登录..."})


@channels_bp.route("/cookie_acquisition_status", methods=["GET"])
def cookie_acquisition_status():
    """查询本地 Cookie 抓取任务的状态"""
    with _cookie_lock:
        return jsonify(_cookie_task)


def save_parsed_video_to_db(result):
    """把解析成功的视频和作者信息自动保存到本地作者库和收藏列表"""
    if not result or result.get("errCode") != 0:
        return
    data = result.get("data") or {}
    fi = data.get("feedInfo")
    ai = data.get("authorInfo")
    if not fi or not ai:
        return
        
    username = ai.get("username") or ai.get("nickname")
    nickname = ai.get("nickname") or "未命名作者"
    head_img_url = ai.get("headImgUrl") or ""
    
    if not username:
        return
        
    username = urllib.parse.unquote(username)
        
    # 1. 持锁把作者与作品写入本地收藏和作者库
    with FEEDS_LOCK:
        try:
            favorites = load_json(CHANNELS_FAVORITES_FILE, [])
            found_fav = False
            for fav in favorites:
                if fav.get("username") == username:
                    if head_img_url:
                        fav["head_img_url"] = head_img_url
                    if nickname and nickname != "未命名作者":
                        fav["nickname"] = nickname
                    found_fav = True
                    break
            if not found_fav:
                favorites.append({
                    "username": username,
                    "nickname": nickname,
                    "head_img_url": head_img_url,
                    "added_time": int(time.time())
                })
            save_json(CHANNELS_FAVORITES_FILE, favorites)
        except Exception as ef:
            print(f"自动保存作者到收藏失败: {ef}")

        try:
            feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
            if username not in feeds_db:
                feeds_db[username] = []

            feed_id = fi.get("id")
            if feed_id:
                description = fi.get("description") or ""
                cover_url = fi.get("coverUrl") or ""
                video_url = fi.get("videoUrl") or ""

                video_url_h264 = fi.get("h264VideoInfo", {}).get("videoUrl", "")
                video_url_h265 = fi.get("h265VideoInfo", {}).get("videoUrl", "")
                if not video_url_h264:
                    video_url_h264 = video_url
                if not video_url_h265:
                    video_url_h265 = video_url

                createtime = str(fi.get("createtime", 0))
                decode_key = fi.get("decodeKey") or ""

                item = {
                    "id": feed_id,
                    "description": description,
                    "cover_url": cover_url,
                    "video_url": video_url,
                    "video_url_h264": video_url_h264,
                    "video_url_h265": video_url_h265,
                    "createtime": createtime,
                    "decode_key": decode_key
                }

                exists = False
                for ex_item in feeds_db[username]:
                    if ex_item.get("id") == feed_id:
                        ex_item.update(item)
                        exists = True
                        break

                if not exists:
                    feeds_db[username].append(item)

                save_json(CHANNELS_FEEDS_FILE, feeds_db)
        except Exception as ev:
            print(f"自动保存视频到作者库失败: {ev}")


def _extract_channels_share_url(raw_text: str) -> str:
    """从分享文案或链接中提取微信视频号合法链接。"""
    for pat in (
        r'(https?://weixin\.qq\.com/sph/[a-zA-Z0-9]+)',
        r'(https?://channels\.weixin\.qq\.com/mobile/sf/[a-zA-Z0-9_]+)',
        r'(https?://channels\.weixin\.qq\.com/finder-preview/pages/sph\?[^\s"\'<>]+)',
        r'(https?://channels\.weixin\.qq\.com/web/pages/feed\?[^\s"\'<>]+)',
        r'(https?://[^\s"\'<>]+)',
    ):
        m = re.search(pat, raw_text)
        if m:
            return m.group(1)
    return raw_text


@channels_bp.route("/fetch_video_profile", methods=["POST"])
def fetch_video_profile():
    """解析视频号分享链接，提取视频 CDN 地址和元数据（支持本地解析、私有Worker以及免翻墙智能穿透）"""
    data = request.get_json() or {}
    url = str(data.get("url") if data.get("url") is not None else "").strip()
    if not url:
        return jsonify({"error": "请输入有效的视频号链接"}), 400

    share_url = _extract_channels_share_url(url)

    # 获取系统配置，检测是否配置了本地解析凭证或私有 Worker
    settings = get_settings()
    yuanbao_cookie = settings.get("yuanbao_cookie", "").strip()
    custom_worker = settings.get("custom_channels_worker", "").strip()

    local_err = None
    # ================= 模式 1: 100% 软件内部本地解析（如用户配置了元宝 Cookie） =================
    if yuanbao_cookie:
        try:
            result = local_parse_with_yuanbao(share_url, yuanbao_cookie)
            save_parsed_video_to_db(result)
            return jsonify(result)
        except Exception as e:
            local_err = str(e)
            # 本地解析失败时，可优雅回退到云端代理模式
            import traceback
            from backend.config import DATA_DIR
            log_file = DATA_DIR / "parse_debug.log"
            try:
                with open(log_file, "a", encoding="utf-8") as f_log:
                    f_log.write(f"\n--- [{time.strftime('%Y-%m-%d %H:%M:%S')}] 本地元宝解析发生错误 ---\n")
                    f_log.write(f"异常消息: {str(e)}\n")
                    f_log.write("详细调用栈:\n")
                    traceback.print_exc(file=f_log)
            except Exception:
                pass
            print(f"本地元宝解析失败，正在尝试云端方案... 错误: {str(e)}")

    # ================= 模式 2: 云端代理解析模式 =================
    if custom_worker:
        target_api = f"{custom_worker.rstrip('/')}/api/fetch_video_profile"
    else:
        target_api = "https://sph.litao.workers.dev/api/fetch_video_profile"

    # 尝试直接访问或使用用户配置代理
    from backend.config import get_proxies_dict, report_proxy_status
    proxies = get_proxies_dict()
    proxy_url = proxies.get("http") if proxies else None

    try:
        resp = requests.post(
            target_api,
            json={"url": share_url},
            headers={"Content-Type": "application/json"},
            proxies=proxies,
            timeout=8
        )
        if resp.status_code == 200:
            if proxy_url:
                report_proxy_status(proxy_url, success=True)
            result = resp.json()
            save_parsed_video_to_db(result)
            return jsonify(result)
    except Exception:
        if proxy_url:
            report_proxy_status(proxy_url, success=False)

    # 兜底：自动使用免翻墙 CDN 穿透通道
    if not custom_worker:
        import random
        from backend.config import get_random_subdomain
        
        backup_hosts = [
            "worker-proxy.asia",
            "net-proxy.asia",
            "1235566.space",
            "worker-proxy.shop",
            "worker-proxys.cyou",
            "worker-proxy.cyou"
        ]
        random.shuffle(backup_hosts)

        for host in backup_hosts:
            actual_host = f"node-{get_random_subdomain()}.{host}"
            tunnel_proxy_url = f"https://{actual_host}:443"
            tunnel_proxies = {"http": tunnel_proxy_url, "https": tunnel_proxy_url}
            
            try:
                resp = requests.post(
                    target_api,
                    json={"url": share_url},
                    headers={"Content-Type": "application/json"},
                    proxies=tunnel_proxies,
                    timeout=12
                )
                if resp.status_code == 200:
                    result = resp.json()
                    save_parsed_video_to_db(result)
                    return jsonify(result)
            except Exception:
                continue

    if local_err:
        return jsonify({"error": f"解析失败：{local_err}。云端备用通道亦无法连通，请在设置中更新元宝 Cookie 凭证或检查网络代理设置"}), 502
    return jsonify({"error": "解析服务暂时不可用（本地未配置元宝凭证且云端中继不可达），请在设置中配置腾讯元宝凭证或开启代理后重试"}), 502


def _format_size(b: float) -> str:
    if b >= 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024 * 1024):.1f} GB"
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{int(b)} B"


class _DownloadCancelled(Exception):
    pass


def _stream_to_file_atomic(
    url: str,
    dest_path: Path,
    decrypt_key=None,
    cancel_event=None,
    progress_cb=None,
):
    """通过 .part 临时文件流式下载并在需要时解密前 128KB，结束后原子重命名。"""
    part_path = dest_path.with_name(dest_path.name + ".part")
    sess = None
    try:
        sess, resp = open_cdn_response(url, stream=True, timeout=60)
        total_size = int(resp.headers.get("content-length", 0) or 0)
        total_str = _format_size(total_size) if total_size > 0 else ""
        downloaded_bytes = 0
        last_speed_time = time.time()
        last_speed_bytes = 0
        speed_str = ""

        with open(part_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=131072):
                if cancel_event and cancel_event.is_set():
                    raise _DownloadCancelled()
                if not chunk:
                    continue
                f.write(chunk)
                downloaded_bytes += len(chunk)
                if progress_cb:
                    now = time.time()
                    if now - last_speed_time >= 0.5:
                        spd = (downloaded_bytes - last_speed_bytes) / max(0.01, now - last_speed_time)
                        speed_str = f"{_format_size(spd)}/s"
                        last_speed_time = now
                        last_speed_bytes = downloaded_bytes
                    pct = min(99, max(0, int(downloaded_bytes / total_size * 100))) if total_size > 0 else 0
                    progress_cb(pct, _format_size(downloaded_bytes), total_str, speed_str)

        if decrypt_key:
            try:
                key_val = int(decrypt_key)
                if key_val > 0:
                    with open(part_path, "r+b") as f:
                        file_data = bytearray(f.read(131072))
                        decrypt_channels_data(file_data, key_val)
                        f.seek(0)
                        f.write(file_data)
            except Exception as ed:
                print(f"解密视频文件失败: {ed}")

        os.replace(part_path, dest_path)
    except Exception:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        raise
    finally:
        if sess is not None:
            sess.close()


def _download_channel_asset(
    channels_dir: Path,
    video_url: str,
    description: str,
    createtime: str,
    decrypt_key=None,
    suffix: str = ".mp4",
    item_kind: str = "",
    files=None,
    bgm_url: str = "",
    cancel_event=None,
    progress_cb=None,
):
    """统一执行视频/封面/音频(MP3)/图文打包(.zip)下载与历史落盘。"""
    channels_dir.mkdir(parents=True, exist_ok=True)
    kind = (item_kind or "").strip().lower()
    req_suffix = (suffix or "").strip().lower()
    if req_suffix and not req_suffix.startswith("."):
        req_suffix = "." + req_suffix

    # 1. 图文合集打包下载（支持图片列表 + 可选 BGM）
    if kind == "picture" or (isinstance(files, list) and len(files) > 0):
        img_urls = [u for u in (files or []) if isinstance(u, str) and u.strip()]
        if not img_urls and video_url:
            img_urls = [video_url]
        filename = sanitize_filename(description, createtime, suffix=".zip")
        filepath = _unique_filepath(channels_dir, filename)
        part_zip = filepath.with_name(filepath.name + ".part")
        try:
            with zipfile.ZipFile(part_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                total_items = len(img_urls) + (1 if bgm_url else 0)
                for idx, img_u in enumerate(img_urls, 1):
                    if cancel_event and cancel_event.is_set():
                        raise _DownloadCancelled()
                    sess, resp = open_cdn_response(img_u, stream=False, timeout=30)
                    try:
                        ct = (resp.headers.get("content-type") or "").lower()
                        ext = ".png" if "png" in ct else (".webp" if "webp" in ct else ".jpg")
                        zf.writestr(f"{idx:02d}{ext}", resp.content)
                    finally:
                        sess.close()
                    if progress_cb and total_items:
                        progress_cb(min(95, int(idx / total_items * 100)), f"{idx}/{total_items}", "", "")
                if bgm_url:
                    sess, resp = open_cdn_response(bgm_url, stream=False, timeout=30)
                    try:
                        zf.writestr("bgm.mp3", resp.content)
                    finally:
                        sess.close()
            os.replace(part_zip, filepath)
        except Exception:
            if part_zip.exists():
                try:
                    part_zip.unlink()
                except OSError:
                    pass
            raise
        history_type = "图文"
    # 2. 封面图片下载（禁用 ISAAC-64 XOR 解密）
    elif kind == "cover" or req_suffix in (".jpg", ".jpeg", ".png", ".webp"):
        ext = req_suffix if req_suffix in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
        filename = sanitize_filename(description, createtime, suffix=ext)
        filepath = _unique_filepath(channels_dir, filename)
        _stream_to_file_atomic(video_url, filepath, decrypt_key=None, cancel_event=cancel_event, progress_cb=progress_cb)
        history_type = "封面"
    # 3. 音频 MP3 提取（下载视频后由 ffmpeg 转码并删除临时 MP4）
    elif kind == "mp3" or req_suffix == ".mp3":
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError("提取 MP3 音频需要系统安装 ffmpeg")
        filename = sanitize_filename(description, createtime, suffix=".mp3")
        filepath = _unique_filepath(channels_dir, filename)
        tmp_mp4 = filepath.with_suffix(".tmp.mp4")
        part_mp3 = filepath.with_name(filepath.name + ".part")
        try:
            _stream_to_file_atomic(video_url, tmp_mp4, decrypt_key=decrypt_key, cancel_event=cancel_event, progress_cb=progress_cb)
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(tmp_mp4), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(part_mp3)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            os.replace(part_mp3, filepath)
        finally:
            for p in (tmp_mp4, part_mp3):
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
        history_type = "音频"
    # 4. 默认视频下载
    else:
        ext = req_suffix or ".mp4"
        filename = sanitize_filename(description, createtime, suffix=ext)
        filepath = _unique_filepath(channels_dir, filename)
        _stream_to_file_atomic(video_url, filepath, decrypt_key=decrypt_key, cancel_event=cancel_event, progress_cb=progress_cb)
        history_type = "视频"

    try:
        size_bytes = filepath.stat().st_size
        add_channels_history_item(description or filepath.name, history_type, str(filepath), size_bytes)
    except Exception as eh:
        print(f"写入视频号专属下载历史记录失败: {eh}")

    return filepath, history_type


@channels_bp.route("/download", methods=["POST"])
def download_video():
    """在后端下载并自动解密视频/封面/音频/图文到本地下载目录"""
    data = request.get_json() or {}
    video_url = str(data.get("url") if data.get("url") is not None else "").strip()
    description = str(data.get("description") or data.get("filename") or "").strip()
    createtime = str(data.get("createtime") if data.get("createtime") is not None else "").strip()
    decrypt_key = data.get("decrypt_key") or data.get("key")
    suffix = str(data.get("suffix") or "").strip()
    item_kind = str(data.get("type") or "").strip()
    files = data.get("files") if isinstance(data.get("files"), list) else None
    bgm_url = str(data.get("bgm_url") or data.get("bgmUrl") or "").strip()

    if not video_url and not files:
        return jsonify({"error": "下载链接不能为空"}), 400

    settings = get_settings()
    base_download_dir = Path(settings.get("download_dir") or str(OUTPUT_DIR))
    channels_dir = base_download_dir / "channels"

    try:
        filepath, history_type = _download_channel_asset(
            channels_dir=channels_dir,
            video_url=video_url,
            description=description,
            createtime=createtime,
            decrypt_key=decrypt_key,
            suffix=suffix,
            item_kind=item_kind,
            files=files,
            bgm_url=bgm_url,
        )
        return jsonify({
            "success": True,
            "message": f"{history_type}下载成功",
            "path": str(filepath),
            "filename": filepath.name,
            "directory": str(channels_dir),
        })
    except Exception as e:
        return jsonify({"error": f"下载失败: {str(e)}"}), 500


@channels_bp.route("/open-folder", methods=["POST"])
def open_folder():
    """在文件管理器中打开视频号下载文件夹"""
    settings = get_settings()
    base_download_dir = Path(settings.get("download_dir") or str(OUTPUT_DIR))
    channels_dir = base_download_dir / "channels"
    
    try:
        channels_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.run(["open", str(channels_dir)])
        elif sys.platform == "win32":
            subprocess.run(["explorer", str(channels_dir)])
        else:
            subprocess.run(["xdg-open", str(channels_dir)])
        return jsonify({"message": "文件夹已打开"})
    except Exception as e:
        return jsonify({"error": f"打开文件夹失败: {str(e)}"}), 500


@channels_bp.route("/history", methods=["GET"])
def get_history():
    """获取视频号下载历史记录"""
    from backend.config import load_json
    history = load_json(CHANNELS_HISTORY_FILE, [])
    return jsonify(history)


@channels_bp.route("/history", methods=["DELETE"])
def clear_history():
    """清除视频号下载历史记录"""
    from backend.config import save_json
    with _HISTORY_LOCK:
        save_json(CHANNELS_HISTORY_FILE, [])
    return jsonify({"message": "历史记录已清空"})


@channels_bp.route("/open-file", methods=["POST"])
def open_file():
    """在系统默认程序中打开特定的视频文件"""
    import subprocess
    import sys
    data = request.get_json() or {}
    path_str = data.get("path", "")
    if not path_str:
        return jsonify({"error": "路径不能为空"}), 400
    try:
        path = Path(path_str)
        if not path.exists():
            return jsonify({"error": "文件不存在"}), 404
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        elif sys.platform == "win32":
            subprocess.run(["explorer", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
        return jsonify({"message": "已打开"})
    except Exception as e:
        return jsonify({"error": f"打开失败: {str(e)}"}), 500


@channels_bp.route("/open-parent", methods=["POST"])
def open_parent():
    """打开文件所在的父目录并选中当前文件"""
    import subprocess
    import sys
    data = request.get_json() or {}
    path_str = data.get("path", "")
    if not path_str:
        return jsonify({"error": "路径不能为空"}), 400
    try:
        path = Path(path_str)
        if not path.exists():
            return jsonify({"error": "文件或文件夹不存在"}), 404
        if path.is_file():
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", str(path)])
            elif sys.platform == "win32":
                subprocess.run(f'explorer /select,"{path.resolve()}"', shell=True)
            else:
                subprocess.run(["xdg-open", str(path.parent)])
        else:
            if sys.platform == "darwin":
                subprocess.run(["open", str(path)])
            elif sys.platform == "win32":
                subprocess.run(f'explorer "{path.resolve()}"', shell=True)
            else:
                subprocess.run(["xdg-open", str(path)])
        return jsonify({"message": "已打开"})
    except Exception as e:
        return jsonify({"error": f"打开失败: {str(e)}"}), 500


@channels_bp.route("/favorites", methods=["GET"])
def get_favorites():
    """获取收藏的视频号作者列表"""
    favorites = load_json(CHANNELS_FAVORITES_FILE, [])
    return jsonify(favorites)


@channels_bp.route("/favorites", methods=["POST"])
def add_favorite():
    """添加视频号作者到收藏列表"""
    data = request.get_json() or {}
    username = str(data.get("username") if data.get("username") is not None else "").strip()
    nickname = str(data.get("nickname") if data.get("nickname") is not None else "").strip()
    head_img_url = str(data.get("head_img_url") if data.get("head_img_url") is not None else "").strip()
    video_url = str(data.get("video_url") if data.get("video_url") is not None else "").strip()

    if not username:
        return jsonify({"error": "作者 ID 不能为空"}), 400

    with FEEDS_LOCK:
        favorites = load_json(CHANNELS_FAVORITES_FILE, [])
        # 检查是否已存在
        for fav in favorites:
            if fav.get("username") == username:
                if nickname and nickname != "已同步作者":
                    fav["nickname"] = nickname
                if head_img_url:
                    fav["head_img_url"] = head_img_url
                if video_url:
                    fav["video_url"] = video_url
                save_json(CHANNELS_FAVORITES_FILE, favorites)
                return jsonify({"message": "作者已在收藏列表中", "favorites": favorites})

        favorites.append({
            "username": username,
            "nickname": nickname or "未命名",
            "head_img_url": head_img_url,
            "video_url": video_url,
            "added_time": int(time.time())
        })
        save_json(CHANNELS_FAVORITES_FILE, favorites)
        return jsonify({"message": "收藏成功", "favorites": favorites})


@channels_bp.route("/favorites/<username>", methods=["DELETE"])
def remove_favorite(username):
    """从收藏列表删除视频号作者及其同步的数据"""
    if not username:
        return jsonify({"error": "作者 ID 不能为空"}), 400

    username = urllib.parse.unquote(username)
    with FEEDS_LOCK:
        favorites = load_json(CHANNELS_FAVORITES_FILE, [])

        nickname = None
        for fav in favorites:
            if fav.get("username") == username:
                nickname = fav.get("nickname")
                break

        new_favorites = [fav for fav in favorites if fav.get("username") != username]
        save_json(CHANNELS_FAVORITES_FILE, new_favorites)

        feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
        if username in feeds_db:
            feeds_db.pop(username, None)
        if nickname and nickname in feeds_db:
            feeds_db.pop(nickname, None)
        save_json(CHANNELS_FEEDS_FILE, feeds_db)

    return jsonify({"message": "已删除作者及同步数据", "favorites": new_favorites})


@channels_bp.route("/author-videos/<username>", methods=["GET"])
def get_author_videos(username):
    """获取指定作者已解析的视频列表"""
    if not username:
        return jsonify([])
    username = urllib.parse.unquote(username)
    feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
    author_videos = feeds_db.get(username, [])
    return jsonify(author_videos)


@channels_bp.route("/author-videos/<username>", methods=["POST"])
def add_author_video(username):
    """为指定作者添加/保存一条解析成功的视频信息"""
    if not username:
        return jsonify({"error": "作者 ID 不能为空"}), 400
    username = urllib.parse.unquote(username)
    feed = request.get_json() or {}
    feed_id = feed.get("id")
    if not feed_id:
        return jsonify({"error": "视频 ID 不能为空"}), 400

    with FEEDS_LOCK:
        feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
        if username not in feeds_db:
            feeds_db[username] = []

        exists = False
        for item in feeds_db[username]:
            if item.get("id") == feed_id:
                item.update(feed)
                exists = True
                break

        if not exists:
            feeds_db[username].append(feed)

        save_json(CHANNELS_FEEDS_FILE, feeds_db)
        return jsonify({"message": "视频已保存到作者作品列表", "videos": feeds_db[username]})


# ── Interceptor Proxy Admin Endpoints ──────────────────────────

@channels_bp.route("/proxy/status", methods=["GET"])
def get_proxy_status():
    """获取代理服务运行状态与证书信任状态"""
    from backend.mitm_proxy import ProxyManager, check_cert_trusted
    manager = ProxyManager.get_instance()
    return jsonify({
        "proxy_running": manager.running,
        "cert_installed": check_cert_trusted(),
        "proxy_port": manager.port,
        "last_error": getattr(manager, "last_error", None),
    })


@channels_bp.route("/proxy/start", methods=["POST"])
def start_proxy_server():
    """启动本地拦截代理服务"""
    from backend.mitm_proxy import ProxyManager
    manager = ProxyManager.get_instance()
    try:
        success = manager.start()
        if success:
            return jsonify({"message": "同步代理服务已成功启动，系统代理已设置", "running": True})
        else:
            return jsonify({"error": "启动同步代理服务失败，端口可能被占用"}), 500
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@channels_bp.route("/proxy/stop", methods=["POST"])
def stop_proxy_server():
    """停止本地拦截代理服务"""
    from backend.mitm_proxy import ProxyManager
    manager = ProxyManager.get_instance()
    manager.stop()
    return jsonify({"message": "同步代理服务已关闭，系统代理已还原", "running": False})


@channels_bp.route("/proxy/install-cert", methods=["POST"])
def force_install_cert():
    """生成并安装/信任 CA 证书"""
    from backend.mitm_proxy import install_system_cert, ensure_ca_certificates
    _, ca_cert_path = ensure_ca_certificates()
    success = install_system_cert(ca_cert_path)
    if success:
        return jsonify({"message": "证书安装/信任成功"})
    else:
        return jsonify({"error": "证书安装失败，请检查系统权限后重试"}), 500


@channels_bp.route("/proxy/uninstall-cert", methods=["POST"])
def force_uninstall_cert():
    """卸载 CA 证书"""
    from backend.mitm_proxy import uninstall_system_cert, check_cert_trusted
    if not check_cert_trusted():
        return jsonify({"message": "证书已卸载"})
    success = uninstall_system_cert()
    if success:
        return jsonify({"message": "证书卸载成功"})
    else:
        return jsonify({"error": "证书卸载失败，请检查系统权限后重试"}), 500


@channels_bp.route("/proxy/download-cert", methods=["GET"])
def download_proxy_cert():
    """下载 CA 根证书文件以供手动安装"""
    from backend.mitm_proxy import CA_CERT_PATH, ensure_ca_certificates
    from flask import send_file
    ensure_ca_certificates()
    if not CA_CERT_PATH.exists():
        return jsonify({"error": "CA 证书生成失败"}), 500
    return send_file(
        str(CA_CERT_PATH),
        mimetype="application/x-x509-ca-cert",
        as_attachment=True,
        download_name="Channels_CA.crt"
    )


_WIN_WECHAT_PROCS = {
    "wechat.exe", "weixin.exe", "wechatapp.exe", "wechatappex.exe",
    "weixinappex.exe", "wechatplayer.exe", "weixinplayer.exe",
}


def is_wechat_running() -> bool:
    """跨平台检测微信主程序及内置 Chromium 渲染子进程是否仍在运行。"""
    if sys.platform == "darwin":
        try:
            chk = subprocess.run(
                ["pgrep", "-f", r"WeChat\.app/Contents/MacOS/(WeChat|WeChatAppEx|WeApp)"],
                capture_output=True,
                timeout=3,
            )
            return chk.returncode == 0
        except Exception:
            return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            TH32CS_SNAPPROCESS = 0x00000002
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            k32 = ctypes.windll.kernel32
            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == INVALID_HANDLE_VALUE:
                return False
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                if k32.Process32FirstW(snap, ctypes.byref(entry)):
                    while True:
                        if str(entry.szExeFile).lower() in _WIN_WECHAT_PROCS:
                            return True
                        if not k32.Process32NextW(snap, ctypes.byref(entry)):
                            break
            finally:
                k32.CloseHandle(snap)
        except Exception:
            return False
    return False


_SIMPLE_CACHE_MAGIC = 0xFCFB6D1BA7725C30
_CACHE_KEY_TARGETS = (
    "res.wx.qq.com/t/wx_fed/cdn_libs/res/libpag/",
    "channels.weixin.qq.com/web/pages/",
    "virtual_svg-icons-register.publish",
    "applymic.publish",
    "connect.publish",
)


def _match_simple_cache_target(cache_file: Path) -> bool:
    """按 Chromium Simple Cache v5 格式解析缓存键，仅精准识别视频号脚本/页面缓存。"""
    try:
        with open(cache_file, "rb") as f:
            header = f.read(24)
            if len(header) < 24:
                return False
            magic, version, key_len = struct.unpack("<QII", header[:16])
            if magic != _SIMPLE_CACHE_MAGIC or version != 5 or key_len == 0 or key_len > 16384:
                return False
            key_bytes = f.read(key_len)
        key_str = key_bytes.decode("utf-8", errors="ignore").lower()
        return any(t in key_str for t in _CACHE_KEY_TARGETS)
    except Exception:
        return False


def evict_windows_channels_cache_if_idle() -> int:
    """Windows 下若微信未运行，精准清理 Chromium Simple Cache 中过期的视频号/LiteApp 脚本缓存，保留登录凭证与 Cookie。"""
    if sys.platform != "win32" or is_wechat_running():
        return 0

    removed = 0
    profile_roots = []
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        profile_roots.append(Path(appdata) / "Tencent" / "xwechat" / "radium" / "web" / "profiles")
        profile_roots.append(Path(appdata) / "Tencent" / "WeChat" / "radium" / "web" / "profiles")
    if localappdata:
        profile_roots.append(Path(localappdata) / "Tencent" / "xwechat" / "radium" / "web" / "profiles")
        profile_roots.append(Path(localappdata) / "Tencent" / "WeChat" / "radium" / "web" / "profiles")
    for root in profile_roots:
        if not root.is_dir():
            continue
        try:
            profiles = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
        except Exception:
            continue
        for prof in profiles:
            cache_data = prof / "Cache" / "Cache_Data"
            if not cache_data.is_dir():
                continue
            try:
                entries = list(cache_data.iterdir())
            except Exception:
                continue
            for entry in entries:
                if entry.is_file() and re.fullmatch(r"[0-9a-f]{16}_0", entry.name):
                    if _match_simple_cache_target(entry):
                        prefix = entry.name[:-2]
                        for suffix in ("_0", "_1", "_s"):
                            target = cache_data / f"{prefix}{suffix}"
                            if target.is_file():
                                try:
                                    target.unlink()
                                    removed += 1
                                except Exception:
                                    pass
    return removed


@channels_bp.route("/clear-cache", methods=["POST"])
def clear_wechat_cache():
    """清除微信视频号内置浏览器缓存（兼容 macOS 与 Windows 微信 3.x / 4.x）"""
    import shutil

    paths_to_delete = [
        # macOS paths
        os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles"),
        os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles_to_delete"),
        os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/cache"),
        os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches/profiles"),
        # Windows WeChat 3.x & 4.x (xwechat) paths
        os.path.expandvars(r"%APPDATA%\Tencent\WeChat\radium\web\profiles"),
        os.path.expandvars(r"%APPDATA%\Tencent\WeChat\radium\web\profiles_to_delete"),
        os.path.expandvars(r"%APPDATA%\Tencent\WeChat\radium\cache"),
        os.path.expandvars(r"%APPDATA%\Tencent\xwechat\radium\web\profiles"),
        os.path.expandvars(r"%APPDATA%\Tencent\xwechat\radium\web\profiles_to_delete"),
        os.path.expandvars(r"%APPDATA%\Tencent\xwechat\radium\cache"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tencent\WeChat\radium\web\profiles"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tencent\xwechat\radium\web\profiles"),
    ]

    if is_wechat_running():
        hint = "请使用 Command+Q 彻底退出微信后再重试！" if sys.platform == "darwin" else "请在系统托盘右键彻底退出微信（含 WeChatAppEx / Weixin 后台进程）后再重试！"
        return jsonify({
            "error": f"检测到微信桌面端（或内置浏览器进程）仍在运行，{hint}"
        }), 400

    deleted_count = 0
    tcc_blocked_paths = []
    errors = []

    for path in paths_to_delete:
        if not os.path.exists(path):
            continue
        try:
            try:
                os.listdir(path)
            except PermissionError:
                tcc_blocked_paths.append(path)
                continue

            shutil.rmtree(path, ignore_errors=True)
            if os.path.exists(path):
                for root, dirs, files in os.walk(path, topdown=False):
                    for name in files:
                        try:
                            os.remove(os.path.join(root, name))
                        except Exception:
                            pass
                    for name in dirs:
                        try:
                            os.rmdir(os.path.join(root, name))
                        except Exception:
                            pass
                try:
                    os.rmdir(path)
                except Exception:
                    pass

            if not os.path.exists(path):
                deleted_count += 1
            else:
                errors.append(f"部分文件可能被微信占用，无法完全删除: {path}")
        except PermissionError:
            tcc_blocked_paths.append(path)
        except Exception as e:
            errors.append(f"删除 {path} 失败: {str(e)}")

    if tcc_blocked_paths and sys.platform == "darwin":
        return jsonify({
            "error": (
                "macOS 系统沙盒保护（TCC）拦截了程序直接删除微信容器目录（Operation not permitted）。"
                "请先用 Command+Q 退出微信，然后通过访达 (Finder) 按 Command+Shift+G 前往 "
                "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium 手动移走 cache 与 web/profiles。"
            )
        }), 400

    if errors:
        return jsonify({
            "error": "，".join(errors) + "。请先在电脑上【彻底退出微信】，然后再试一次！"
        }), 400

    return jsonify({
        "success": True,
        "message": f"已成功清除微信视频号浏览器缓存（清理 {deleted_count} 个目录）！请重启微信后再打开视频号页面。"
    })


import uuid
import threading

_download_tasks = {}
_download_tasks_lock = threading.Lock()

def _do_async_download_video(
    task_id,
    video_url,
    description,
    createtime,
    decrypt_key,
    suffix="",
    item_kind="",
    files=None,
    bgm_url="",
):
    global _download_tasks

    settings = get_settings()
    base_download_dir = Path(settings.get("download_dir") or str(OUTPUT_DIR))
    channels_dir = base_download_dir / "channels"

    with _download_tasks_lock:
        task = _download_tasks.get(task_id)
        if not task:
            return
        cancel_event = task["cancel_event"]
        if cancel_event.is_set():
            task["status"] = "cancelled"
            return

    def _on_progress(pct, downloaded, total, speed):
        with _download_tasks_lock:
            if task_id in _download_tasks:
                _download_tasks[task_id]["progress"] = pct
                _download_tasks[task_id]["downloaded"] = downloaded
                _download_tasks[task_id]["total"] = total
                _download_tasks[task_id]["speed"] = speed

    try:
        filepath, _ = _download_channel_asset(
            channels_dir=channels_dir,
            video_url=video_url,
            description=description,
            createtime=createtime,
            decrypt_key=decrypt_key,
            suffix=suffix,
            item_kind=item_kind,
            files=files,
            bgm_url=bgm_url,
            cancel_event=cancel_event,
            progress_cb=_on_progress,
        )
        with _download_tasks_lock:
            if task_id in _download_tasks:
                _download_tasks[task_id]["status"] = "success"
                _download_tasks[task_id]["progress"] = 100
                _download_tasks[task_id]["result"] = {
                    "path": str(filepath),
                    "filename": filepath.name,
                    "directory": str(channels_dir),
                }
    except _DownloadCancelled:
        with _download_tasks_lock:
            if task_id in _download_tasks:
                _download_tasks[task_id]["status"] = "cancelled"
    except Exception as e:
        with _download_tasks_lock:
            if task_id in _download_tasks:
                _download_tasks[task_id]["status"] = "failed"
                _download_tasks[task_id]["error"] = str(e)


@channels_bp.route("/download/start", methods=["POST"])
def start_download_video_async():
    """开始异步下载视频/封面/音频/图文"""
    global _download_tasks
    data = request.get_json() or {}
    video_url = str(data.get("url") if data.get("url") is not None else "").strip()
    description = str(data.get("description") or data.get("filename") or "").strip()
    createtime = str(data.get("createtime") if data.get("createtime") is not None else "").strip()
    decrypt_key = data.get("decrypt_key") or data.get("key")
    suffix = str(data.get("suffix") or "").strip()
    item_kind = str(data.get("type") or "").strip()
    files = data.get("files") if isinstance(data.get("files"), list) else None
    bgm_url = str(data.get("bgm_url") or data.get("bgmUrl") or "").strip()

    if not video_url and not files:
        return jsonify({"error": "下载链接不能为空"}), 400

    task_id = str(uuid.uuid4())

    with _download_tasks_lock:
        if len(_download_tasks) > 200:
            keys_to_remove = [k for k, t in _download_tasks.items() if t["status"] in ("success", "failed", "cancelled")]
            for k in keys_to_remove[:50]:
                _download_tasks.pop(k, None)

        _download_tasks[task_id] = {
            "status": "downloading",
            "progress": 0,
            "cancel_event": threading.Event(),
            "error": None,
            "result": None
        }

    thread = threading.Thread(
        target=_do_async_download_video,
        args=(task_id, video_url, description, createtime, decrypt_key, suffix, item_kind, files, bgm_url),
        daemon=True
    )
    thread.start()

    return jsonify({"success": True, "task_id": task_id})


@channels_bp.route("/download/status/<task_id>", methods=["GET"])
def get_async_download_status(task_id):
    """获取异步下载状态"""
    with _download_tasks_lock:
        task = _download_tasks.get(task_id)
        if not task:
            return jsonify({"error": "未找到指定的下载任务"}), 404
        
        return jsonify({
            "status": task["status"],
            "progress": task["progress"],
            "downloaded": task.get("downloaded", ""),
            "total": task.get("total", ""),
            "speed": task.get("speed", ""),
            "error": task["error"],
            "result": task["result"]
        })


@channels_bp.route("/download/cancel/<task_id>", methods=["POST"])
def cancel_async_download(task_id):
    """取消异步下载"""
    with _download_tasks_lock:
        task = _download_tasks.get(task_id)
        if not task:
            return jsonify({"error": "未找到指定的下载任务"}), 404
        
        task["cancel_event"].set()
        task["status"] = "cancelled"
        return jsonify({"success": True, "message": "下载已请求取消"})


@channels_bp.route("/upload-log", methods=["GET"])
def get_upload_log():
    """获取视频号自动上传日志（channels_upload_log.jsonl 最近 N 条，新→旧）"""
    from backend.channels_upload import CHANNELS_UPLOAD_LOG_FILE
    limit = request.args.get("limit", 100, type=int)
    entries = []
    try:
        if CHANNELS_UPLOAD_LOG_FILE.exists():
            with open(CHANNELS_UPLOAD_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    except Exception as e:
        return jsonify({"error": f"读取上传日志失败: {e}"}), 500
    entries.reverse()
    return jsonify(entries)


@channels_bp.route("/process-uploads", methods=["POST"])
def process_uploads():
    """处理待上传作品：下载→COS→服务器"""
    from backend.channels_upload import process_pending_uploads
    from backend.config import load_json

    feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
    pending_count = sum(
        1
        for items in feeds_db.values()
        for item in items
        if item.get("needs_upload") and not item.get("uploaded")
        and not item.get("upload_failed") and item.get("video_url")
    )

    def bg_task():
        try:
            process_pending_uploads()
        except Exception as e:
            print(f"Upload process error: {e}")

    threading.Thread(target=bg_task, daemon=True).start()
    return jsonify({"started": True, "pending_count": pending_count})


@channels_bp.route("/sync-feed", methods=["POST"])
def sync_feed_endpoint():
    """在 Flask 主进程内持锁接收注入脚本同步的作者作品（避免跨进程并发写冲突）。"""
    payload = request.get_json() or {}
    username = payload.get("username")
    feeds = payload.get("feeds", [])
    try:
        from backend.mitm_proxy import save_synced_feeds
        save_synced_feeds(username, feeds)
        return jsonify({"code": 0, "success": True, "message": "Feeds synced successfully"})
    except Exception as ex:
        return jsonify({"success": False, "error": str(ex)}), 500


@channels_bp.route("/rss/<username>", methods=["GET"])
def get_author_rss(username):
    """按作者输出视频号 RSS 2.0 订阅源。"""
    username = urllib.parse.unquote(username or "")
    if not username:
        return jsonify({"error": "作者 ID 不能为空"}), 400

    with FEEDS_LOCK:
        favs = load_json(CHANNELS_FAVORITES_FILE, [])
        feeds_db = load_json(CHANNELS_FEEDS_FILE, {})
        items = list(feeds_db.get(username, []))
        author_meta = next((f for f in favs if isinstance(f, dict) and f.get("username") == username), {})

    nickname = (author_meta or {}).get("nickname") or (items[0].get("nickname") if items else "") or username
    items.sort(key=lambda x: int(x.get("createtime") or 0), reverse=True)

    xml_items = []
    for it in items[:50]:
        fid = str(it.get("id") or "")
        desc = it.get("description") or fid or "未命名作品"
        title_line = desc.splitlines()[0][:80]
        video_link = it.get("cos_url") or it.get("video_url_h265") or it.get("video_url_h264") or it.get("video_url") or ""
        cover = it.get("cover_url") or ""
        try:
            pub_ts = int(it.get("createtime") or 0)
        except Exception:
            pub_ts = 0
        pub_date = formatdate(pub_ts, usegmt=True) if pub_ts > 0 else formatdate(usegmt=True)
        body_html = ""
        if cover:
            body_html += f'<p><img src="{xml_escape(cover)}" alt="cover"/></p>'
        if video_link:
            body_html += f'<p><a href="{xml_escape(video_link)}">观看/下载视频</a></p>'
        body_html += f"<p>{xml_escape(desc)}</p>"
        body_html_safe = body_html.replace("]]>", "]]]]><![CDATA[>")
        xml_items.append(
            f"<item>"
            f"<title>{xml_escape(title_line)}</title>"
            f"<link>{xml_escape(video_link)}</link>"
            f"<guid isPermaLink=\"false\">{xml_escape(username)}:{xml_escape(fid)}</guid>"
            f"<pubDate>{xml_escape(pub_date)}</pubDate>"
            f"<description><![CDATA[{body_html_safe}]]></description>"
            f"</item>"
        )

    channel_link = f"https://channels.weixin.qq.com/web/pages/profile?username={urllib.parse.quote(username)}"
    rss_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>{xml_escape(nickname)} - 微信视频号</title>"
        f"<link>{xml_escape(channel_link)}</link>"
        f"<description>{xml_escape(nickname)} 的视频号订阅源</description>"
        + "".join(xml_items)
        + "</channel></rss>"
    )
    return Response(rss_xml, mimetype="application/rss+xml; charset=utf-8")

