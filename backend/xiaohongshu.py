"""
小红书视频与图文下载模块
"""

import os
import re
import time
import random
import yaml
import shutil
import threading
import queue
import atexit
import json
import base64
import html
import requests
import httpx
import math
from curl_cffi import requests as curl_requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, quote
from lxml import etree
from flask import Blueprint, jsonify, request

from backend.config import (
    DATA_DIR, load_json, save_json, get_settings, get_proxies_dict, report_proxy_status
)

# 签名接口依赖（可选）：用于博主笔记列表的 user_posted API（见设计文档 §7）。
# 未安装时签名路径自动禁用，get_user_profile 会降级到主页 SSR 解析。
try:
    from xhshow import Xhshow
except Exception:
    Xhshow = None

xhs_bp = Blueprint("xhs", __name__, url_prefix="/api/xhs")

# ── 存储路径 ──────────────────────────────────────────────
XHS_DIR = DATA_DIR / "xhs_downloads"
XHS_ACCOUNTS_FILE = DATA_DIR / "xhs_accounts.json"
XHS_HISTORY_FILE = DATA_DIR / "xhs_history.json"

# ── 任务管理器 ────────────────────────────────────────────
_download_tasks = {}
_download_lock = threading.Lock()

# ── 工具函数 ──────────────────────────────────────────────
def lognormal_sleep(avg_delay: float = 6.0, sigma: float = 0.6):
    """对数正态分布随机等待时间，模拟人类在浏览器中的浏览/阅读停留行为，比均匀随机更自然以规避频控"""
    mu = math.log(avg_delay) - (sigma**2 / 2)
    wait_time = max(0.5, random.lognormvariate(mu, sigma))
    time.sleep(wait_time)

def clean_filename(filename: str) -> str:
    """清理文件名，移除不支持的字符"""
    filename = re.sub(r'[\\/:*?"<>|\n\r\t]', "", filename)
    filename = filename.strip().replace(" ", "_")
    return filename[:80] if filename else "untitled"

def write_note_text(note_dir: Path, detail: dict) -> int:
    """把笔记文字内容写入 文案.txt（对应 XHS-Downloader 记录的作品标题/描述/标签/数据字段）。
    返回写入字节数；失败不抛异常（不影响媒体下载）。"""
    try:
        stats = detail.get("stats", {}) or {}
        tags = detail.get("tags", []) or []
        lines = [
            f"标题: {detail.get('title', '')}",
            f"作者: {detail.get('author', {}).get('nickname', '')}",
            f"发布时间: {detail.get('publish_time', '')}",
            f"类型: {detail.get('type', '')}",
            f"链接: {detail.get('note_url', '')}",
            f"点赞: {stats.get('liked', '')}  收藏: {stats.get('collected', '')}  "
            f"评论: {stats.get('comment', '')}  分享: {stats.get('share', '')}",
            f"标签: {' '.join('#' + t for t in tags)}",
            "",
            detail.get("desc", "") or "",
        ]
        content = "\n".join(lines)
        text_path = note_dir / "文案.txt"
        text_path.write_text(content, encoding="utf-8")
        return text_path.stat().st_size
    except Exception as e:
        print(f"写入文案失败: {e}")
        return 0

def write_note_html(note_dir: Path, detail: dict, video_file: str = None, image_items: list = None, cos_url: str = None) -> int:
    """生成与原文相似、可直接双击打开的 index.html，包含完整图文/视频媒体链接。
    返回写入字节数；失败不抛异常。"""
    try:
        from html import escape
        image_items = image_items or []
        stats = detail.get("stats", {}) or {}
        tags = detail.get("tags", []) or []
        title = escape(detail.get("title", "") or "无标题")
        author = escape(detail.get("author", {}).get("nickname", ""))
        avatar = escape(detail.get("author", {}).get("avatar", "") or "")
        ptime = escape(detail.get("publish_time", ""))
        link = escape(detail.get("note_url", ""))
        desc = escape(detail.get("desc", "") or "")
        images = detail.get("images", []) or []

        media_html = ""
        effective_video = cos_url or detail.get("cos_url") or detail.get("video_url") or video_file or detail.get("video")
        if effective_video:
            media_html += f'<video controls src="{escape(str(effective_video))}"></video>'

        if images:
            for img_url in images:
                if img_url:
                    media_html += f'<img src="{escape(img_url)}" referrerpolicy="no-referrer" />'
        elif image_items:
            for it in image_items:
                if it.get("img"):
                    media_html += f'<img src="{escape(it["img"])}" />'
                if it.get("live"):
                    media_html += f'<video controls src="{escape(it["live"])}"></video>'

        tags_html = " ".join(f'<span class="tag">#{escape(t)}</span>' for t in tags)
        stats_line = (f"赞 {escape(str(stats.get('liked','')))} · 藏 {escape(str(stats.get('collected','')))}"
                      f" · 评 {escape(str(stats.get('comment','')))} · 分享 {escape(str(stats.get('share','')))}")
        avatar_html = f'<img class="avatar" src="{avatar}" referrerpolicy="no-referrer" />' if avatar else ''

        html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#333;}}
.card{{max-width:600px;margin:24px auto;background:#fff;border-radius:12px;padding:20px 24px;box-shadow:0 2px 12px rgba(0,0,0,.06);}}
.author{{display:flex;align-items:center;gap:10px;margin-bottom:16px;}}
.avatar{{width:40px;height:40px;border-radius:50%;object-fit:cover;background:#eee;}}
.name{{font-weight:600;}}
.time{{color:#999;font-size:.85rem;}}
h1{{font-size:1.25rem;margin:8px 0 12px;line-height:1.4;}}
img,video{{width:100%;border-radius:8px;margin:8px 0;display:block;}}
.desc{{white-space:pre-wrap;line-height:1.7;font-size:1rem;margin:12px 0;}}
.tags{{margin:12px 0;}}
.tag{{color:#13386c;margin-right:8px;font-size:.95rem;}}
.stats{{color:#999;font-size:.85rem;border-top:1px solid #eee;padding-top:12px;margin-top:12px;}}
.link{{font-size:.8rem;color:#bbb;word-break:break-all;margin-top:6px;}}
</style></head>
<body><div class="card">
<div class="author">{avatar_html}<div><div class="name">{author}</div><div class="time">{ptime}</div></div></div>
<h1>{title}</h1>
{media_html}
<div class="desc">{desc}</div>
<div class="tags">{tags_html}</div>
<div class="stats">{stats_line}</div>
<div class="link">原文: <a href="{link}">{link}</a></div>
</div></body></html>"""
        html_path = note_dir / "index.html"
        html_path.write_text(html_doc, encoding="utf-8")
        return html_path.stat().st_size
    except Exception as e:
        print(f"写入 HTML 失败: {e}")
        return 0

def upload_xhs_video_to_cos(video_path: Path, note_id: str, max_retries: int = 3, return_error: bool = False) -> str | None | tuple[str | None, str | None]:
    """把本地下载的小红书视频文件上传到腾讯云 COS，返回公网链接。
    - 上传路径前缀统一为 channels/ (如 channels/{note_id}.mp4)
    - 强制使用小红书专属存储桶 chenshipin-hg-1305012248 与对应域名，完全隔离公众号/视频号
    - 失败自动重试 3 次，若使用 STS 临时凭证接口则在重试时强行刷新 token
    - 详细打印并输出 COS 失败原因
    - 若 return_error=True，返回 (cos_url, error_msg)；否则直接返回 cos_url (兼容旧调用)
    """
    if not video_path or not video_path.exists():
        err = f"视频文件不存在: {video_path}"
        print(f"[小红书 COS] 错误: {err}", flush=True)
        return (None, err) if return_error else None

    if video_path.stat().st_size == 0:
        err = f"视频文件大小为 0 字节: {video_path}"
        print(f"[小红书 COS] 错误: {err}", flush=True)
        return (None, err) if return_error else None

    settings = get_settings()
    token_api_url = (settings.get("cos_token_api_url") or "").strip()

    # 小红书专属存储桶配置（彻底与公众号/视频号隔离，不受全局 cos_bucket 覆盖影响）
    target_bucket = str(settings.get("xhs_cos_bucket") or "chenshipin-hg-1305012248").strip()
    target_region = str(settings.get("xhs_cos_region") or settings.get("cos_region") or "ap-guangzhou").strip()
    target_domain = str(settings.get("xhs_cos_cds_domain") or f"https://{target_bucket}.cos.{target_region}.myqcloud.com/").strip()
    target_prefix = str(settings.get("xhs_cos_prefix") or "channels/").strip()
    if not target_prefix.endswith("/"):
        target_prefix += "/"
    target_prefix = target_prefix.lstrip("/")

    def _get_cos_cfg(force_refresh: bool = False) -> tuple[dict | None, str | None]:
        if token_api_url:
            try:
                from backend.channels_upload import _fetch_cos_token
                cfg = _fetch_cos_token(token_api_url, force_refresh=force_refresh)
                if cfg:
                    return cfg, None
            except Exception as e:
                return None, f"获取 STS 临时凭证失败: {e}"

        s_id = str(settings.get("cos_secret_id") if settings.get("cos_secret_id") is not None else "").strip()
        s_key = str(settings.get("cos_secret_key") if settings.get("cos_secret_key") is not None else "").strip()
        if s_id and s_key:
            return {
                "secret_id": s_id,
                "secret_key": s_key,
                "region": target_region,
                "bucket": target_bucket,
            }, None

        missing = []
        if not s_id: missing.append("cos_secret_id")
        if not s_key: missing.append("cos_secret_key")
        return None, f"未配置腾讯云 COS 凭证密钥（缺少 {', '.join(missing)} 且未配置 cos_token_api_url）"

    # 获取初始凭证
    cos_cfg, cfg_err = _get_cos_cfg(force_refresh=False)
    if not cos_cfg:
        print(f"[小红书 COS] 跳过上传: {cfg_err}", flush=True)
        return (None, cfg_err) if return_error else None

    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as e:
        err = f"未安装 qcloud_cos 依赖包: {e}"
        print(f"[小红书 COS] 错误: {err}", flush=True)
        return (None, err) if return_error else None

    key = f"{target_prefix}{note_id or int(time.time())}.mp4"

    try:
        data = video_path.read_bytes()
    except Exception as e:
        err = f"读取视频文件数据失败: {e}"
        print(f"[小红书 COS] 错误: {err}", flush=True)
        return (None, err) if return_error else None

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            # 重试时若走 STS 接口则强行刷新凭证
            if attempt > 1 and token_api_url:
                refreshed_cfg, _ = _get_cos_cfg(force_refresh=True)
                if refreshed_cfg:
                    cos_cfg = refreshed_cfg

            cos_config_kwargs = {
                "Region": target_region,
                "SecretId": cos_cfg["secret_id"],
                "SecretKey": cos_cfg["secret_key"],
            }
            if cos_cfg.get("token"):
                cos_config_kwargs["Token"] = cos_cfg["token"]

            config = CosConfig(**cos_config_kwargs)
            client = CosS3Client(config)
            client.put_object(Bucket=target_bucket, Body=data, Key=key)

            cos_url = f"{target_domain.rstrip('/')}/{key.lstrip('/')}"
            print(f"[小红书 COS] 视频上传成功 (尝试 {attempt}/{max_retries}): {cos_url}", flush=True)
            return (cos_url, None) if return_error else cos_url
        except Exception as e:
            last_err = e
            print(f"[小红书 COS] 视频上传失败 (第 {attempt}/{max_retries} 次重试, note_id={note_id}): {e}", flush=True)
            if attempt < max_retries:
                time.sleep(1.0 * attempt)

    final_err_msg = f"重试 {max_retries} 次均失败: {last_err}"
    print(f"[小红书 COS] 视频上传最终失败 (note_id={note_id}): {final_err_msg}", flush=True)
    return (None, final_err_msg) if return_error else None

def generate_note_content(detail: dict, cos_url: str = None) -> str:
    """区分视频和图文生成 content：
    - 视频：文案描述 + 标签 + <video src="腾讯云视频链接"></video>
    - 图文：纯正文 HTML 片段格式，包含段落 <p> 与所有高清原图 <p><img src="..." referrerpolicy="no-referrer" /></p>
    """
    note_type = detail.get("type", "图文")
    desc = detail.get("desc") or ""
    tags = detail.get("tags") or []
    images = detail.get("images") or []
    effective_video_url = cos_url or detail.get("cos_url") or detail.get("video_url") or detail.get("video") or ""

    is_video = (note_type == "视频") or bool(effective_video_url)

    if is_video:
        # 视频笔记：文案描述 + 标签 + <video src="..."></video> 拼接到最后
        parts = []
        if desc:
            parts.append(desc)
        if tags:
            tags_str = " ".join("#" + t for t in tags)
            parts.append(tags_str)
        if effective_video_url:
            parts.append(f'<video src="{effective_video_url}"></video>')
        return "\n\n".join(parts) if parts else (f'<video src="{effective_video_url}"></video>' if effective_video_url else desc)

    # 图文笔记：纯正文 HTML 片段（包含文字段落、标签与所有图片标签）
    html_parts = []
    if desc:
        lines = [line.strip() for line in desc.split("\n") if line.strip()]
        for line in lines:
            html_parts.append(f"<p>{html.escape(line)}</p>")

    if tags:
        tags_str = " ".join("#" + html.escape(t) for t in tags)
        html_parts.append(f"<p>{tags_str}</p>")

    if images:
        for img_url in images:
            if img_url:
                html_parts.append(f'<p><img src="{html.escape(img_url)}" referrerpolicy="no-referrer" /></p>')

    return "".join(html_parts) if html_parts else (f"<p>{html.escape(desc)}</p>" if desc else "")

def write_note_data_json(note_dir: Path, detail: dict, cos_url: str = None, video_file: str = None, image_items: list = None) -> dict:
    """生成与公众号对齐的 data.json，视频与图文分别使用对应的 content 格式"""
    try:
        author = detail.get("author", {}) or {}
        author_nick = author.get("nickname") or author.get("nickName") or "未知博主"
        title = detail.get("title") or detail.get("note_id") or "无标题"
        note_url = detail.get("note_url") or f"https://www.xiaohongshu.com/explore/{detail.get('note_id', '')}"
        cover_url = detail.get("cover") or (detail.get("images", [""])[0] if detail.get("images") else "")
        publish_time = detail.get("publish_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        desc = detail.get("desc") or ""
        tags = detail.get("tags") or []
        images = detail.get("images") or []

        # 区分视频与图文生成 content 内容
        content = generate_note_content(detail, cos_url=cos_url)

        data = {
            "source": author_nick,
            "title": title,
            "url": note_url,
            "cover_url": cover_url,
            "publish_time": publish_time,
            "content": content,
            "desc": desc,
            "type": detail.get("type", "图文"),
            "tags": tags,
            "images": images,
            "stats": detail.get("stats", {}),
            "video_url": cos_url or detail.get("video") or "",
            "cos_url": cos_url or "",
            "note_id": detail.get("note_id", ""),
            "author": author,
            "local_dir": str(note_dir),
            "created_time": time.time(),
        }

        data_path = note_dir / "data.json"
        data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", errors="replace")
        return data
    except Exception as e:
        print(f"写入 data.json 失败: {e}")
        return {}

def post_xhs_note_to_server(data_json: dict) -> tuple[bool, str | None]:
    """把小红书笔记（以公众号相同的格式）推送到远端服务器"""
    if not data_json or not isinstance(data_json, dict):
        return False, "缺少有效的笔记数据"

    settings = get_settings()
    upload_enabled = settings.get("xhs_upload_enabled")
    if upload_enabled is None:
        upload_enabled = settings.get("rss_upload_enabled", False)

    if not upload_enabled:
        return False, "服务器上传功能未启用"

    upload_url = (settings.get("xhs_upload_url") or settings.get("rss_upload_url") or "").strip()
    if not upload_url:
        return False, "未配置上传接口地址 (xhs_upload_url / rss_upload_url)"

    content_str = data_json.get("content") or data_json.get("desc") or ""
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")

    device_id = settings.get("xhs_device_id") or settings.get("device_id") or "小红书_caiji100"

    article_item = {
        "source": data_json.get("source", ""),
        "title": data_json.get("title", ""),
        "url": data_json.get("url", ""),
        "cover_url": data_json.get("cover_url", ""),
        "publish_time": data_json.get("publish_time", ""),
        "content": content_b64,
        "video_url": data_json.get("video_url") or data_json.get("cos_url") or "",
        "cos_url": data_json.get("cos_url") or "",
        "desc": data_json.get("desc", ""),
        "type": data_json.get("type", "图文"),
        "tags": data_json.get("tags", []),
        "images": data_json.get("images", []),
        "note_id": data_json.get("note_id", ""),
    }

    payload = {
        "articles": [article_item],
        "deviceId": device_id,
    }

    try:
        resp = requests.post(
            upload_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            proxies=get_proxies_dict(),
            timeout=30
        )
        resp_text = resp.content.decode("utf-8", errors="replace") if hasattr(resp, "content") and resp.content else (resp.text or "")
        print(f"[小红书服务器POST] 上传响应状态码: {resp.status_code}, 响应: {resp_text[:300]}")
        resp.raise_for_status()

        try:
            body = json.loads(resp_text)
            if isinstance(body, dict) and body.get("success") is False:
                return False, body.get("message") or body.get("error") or "远端接口返回失败"
        except Exception:
            pass

        return True, None
    except Exception as e:
        err_msg = str(e)
        print(f"[小红书服务器POST] 上传失败: {err_msg}")
        return False, err_msg


# ── 小红书 API 客户端 ─────────────────────────────────────
class XhsClient:
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    
    BASE_HEADERS = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "referer": "https://www.xiaohongshu.com/explore",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": USER_AGENT,
    }
    
    def get_headers(self) -> dict:
        headers = self.BASE_HEADERS.copy()
        settings = get_settings()
        cookie = settings.get("xhs_cookie", "")
        if cookie:
            headers["cookie"] = cookie.strip()
        return headers
        
    def _request(self, url: str, method: str = "GET", retries: int = None, **kwargs) -> curl_requests.Response:
        if retries is None:
            settings = get_settings()
            retries = settings.get("max_retries", 3)

        headers = self.get_headers()
        if "headers" in kwargs:
            headers.update(kwargs["headers"])
        kwargs["headers"] = headers

        kwargs.pop("allow_redirects", None)

        last_err = None
        for attempt in range(retries):
            lognormal_sleep(avg_delay=3.5)

            proxies = get_proxies_dict()
            proxy_url = proxies.get("http") if proxies else None

            try:
                resp = curl_requests.request(
                    method, url, impersonate="chrome124",
                    proxy=proxy_url, timeout=20, **kwargs
                )

                if proxy_url:
                    report_proxy_status(proxy_url, success=True)

                if resp.status_code in (461, 406):
                    raise ValueError("触发小红书风控限制（461/406），请在登录页更新 Cookie 或稍后再试。")

                return resp
            except Exception as e:
                if proxy_url:
                    report_proxy_status(proxy_url, success=False)
                last_err = e
                print(f"Request failed (attempt {attempt + 1}/{retries}) for {url}: {e}")

        if last_err:
            raise last_err
        raise RuntimeError(f"请求 {url} 失败且未捕获到具体异常")

    def resolve_short_link(self, url: str) -> str:
        if not url.startswith("http"):
            url = "http://" + url
        headers = self.get_headers()
        proxies = get_proxies_dict()
        proxy_url = proxies.get("http") if proxies else None
        try:
            resp = curl_requests.get(
                url, headers=headers, impersonate="chrome124",
                proxy=proxy_url, timeout=15, allow_redirects=True
            )
            if proxy_url:
                report_proxy_status(proxy_url, success=True)
            return str(resp.url)
        except Exception as e:
            if proxy_url:
                report_proxy_status(proxy_url, success=False)
            print(f"Error resolving short link {url}: {e}")
            return url

    def extract_profile_url(self, text: str) -> str:
        """从任意包含文字和链接的字符串中提取博主主页链接，并自动还原短链"""
        text = text.strip()
        profile_pattern = re.compile(r"(?:https?://)?(?:www\.)?xiaohongshu\.com/user/profile/[a-zA-Z0-9_-]+")
        match = profile_pattern.search(text)
        if match:
            url = match.group(0)
            return url if url.startswith("http") else "https://" + url

        short_pattern = re.compile(r"(?:https?://)?xhslink\.(?:com|cn)/[^\s\"<>\\^`{|}，。；！？、【】《》]+")
        match = short_pattern.search(text)
        if match:
            resolved = self.resolve_short_link(match.group(0))
            return resolved

        return text

    def extract_links(self, text: str) -> list[str]:
        words = text.split()
        results = []
        
        short_pattern = re.compile(r"(?:https?://)?xhslink\.(?:com|cn)/[^\s\"<>\\^`{|}，。；！？、【】《》]+")
        share_pattern = re.compile(r"(?:https?://)?(?:www\.)?xiaohongshu\.com/discovery/item/\S+")
        link_pattern = re.compile(r"(?:https?://)?(?:www\.)?xiaohongshu\.com/explore/\S+")
        user_note_pattern = re.compile(r"(?:https?://)?(?:www\.)?xiaohongshu\.com/user/profile/[a-z0-9]+/\S+")
        
        for word in words:
            # Check SHORT first
            short_match = short_pattern.search(word)
            if short_match:
                matched_url = short_match.group(0)
                resolved_url = self.resolve_short_link(matched_url)
                if resolved_url:
                    if share_pattern.search(resolved_url) or link_pattern.search(resolved_url) or user_note_pattern.search(resolved_url):
                        results.append(resolved_url)
                continue
            
            # Check SHARE
            share_match = share_pattern.search(word)
            if share_match:
                url = share_match.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                results.append(url)
                continue
                
            # Check LINK
            link_match = link_pattern.search(word)
            if link_match:
                url = link_match.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                results.append(url)
                continue
                
            # Check USER_NOTE
            user_note_match = user_note_pattern.search(word)
            if user_note_match:
                url = user_note_match.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                results.append(url)
                continue
                
        return results

    def extract_note_id(self, url: str) -> str:
        # explore / discovery 链接：/explore/<id> 或 /item/<id>
        match = re.search(r"(?:explore|item)/([0-9a-zA-Z]+)", url)
        if match:
            return match.group(1)
        # 主页内笔记链接：/user/profile/<uid>/<note_id>（对齐 XHS-Downloader 的 ID_USER）
        match = re.search(r"user/profile/[0-9a-zA-Z]+/([0-9a-zA-Z]+)", url)
        if match:
            return match.group(1)
        return ""

    def get_initial_state(self, url: str) -> dict:
        resp = self._request(url)
        if resp.status_code != 200:
            raise RuntimeError(f"请求失败，HTTP 状态码: {resp.status_code}")
            
        html = resp.text
        parser = etree.HTMLParser()
        tree = etree.HTML(html, parser)
        if tree is None:
            raise ValueError("无法解析网页 HTML 结构")
            
        scripts = tree.xpath("//script/text()")
        state_text = None
        for script in reversed(scripts):
            script = script.strip()
            if script.startswith("window.__INITIAL_STATE__"):
                state_text = script
                break
                
        if not state_text:
            if "login" in resp.url or "captcha" in html or "验证码" in html or "404?source=" in resp.url or "error_code=" in resp.url:
                raise ValueError("触发风控或 Cookie 失效，请在登录页更新 Cookie。")
            raise ValueError("未在页面中找到 window.__INITIAL_STATE__，请检查链接或更新 Cookie。")
            
        eq_idx = state_text.find("=")
        if eq_idx == -1:
            raise ValueError("window.__INITIAL_STATE__ 格式不正确，缺少 '=' 赋值")
            
        state_json_str = state_text[eq_idx+1:].strip()
        if state_json_str.endswith(";"):
            state_json_str = state_json_str[:-1].strip()
            
        cleaned = state_json_str
        cleaned = re.sub(r"\bundefined\b", "null", cleaned)
        cleaned = re.sub(r"new\s+Map\(\s*(?:\[[^\]]*\])?\s*\)", "{}", cleaned)
        cleaned = re.sub(r"new\s+Set\(\s*(?:\[[^\]]*\])?\s*\)", "[]", cleaned)
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", cleaned)

        try:
            try:
                state = json.loads(cleaned)
            except Exception:
                state = yaml.safe_load(cleaned)
            if not isinstance(state, dict):
                raise ValueError("解析得到的数据类型不是 dict")
            return state
        except Exception as e:
            raise ValueError(f"解析 window.__INITIAL_STATE__ 失败: {str(e)}")

    def format_count(self, count) -> str:
        if count is None:
            return "0"
        try:
            c = int(count)
            if c >= 10000:
                return f"{round(c / 10000, 1)}万"
            return str(c)
        except Exception:
            return str(count)

    def get_note_detail(self, url: str) -> dict:
        state = self.get_initial_state(url)
        
        note = None
        if "noteData" in state:
            note = state.get("noteData", {}).get("data", {}).get("noteData")
        if not note and "note" in state:
            note_detail_map = state.get("note", {}).get("noteDetailMap", {})
            if note_detail_map and isinstance(note_detail_map, dict):
                note = list(note_detail_map.values())[-1].get("note")
                
        if not note:
            raise ValueError("触发风控或 Cookie 失效，请在登录页更新 Cookie。")
            
        note_id = note.get("noteId") or note.get("id", "")
        title = note.get("title", "").strip()
        desc = note.get("desc", "").strip()
        
        tag_list = note.get("tagList", [])
        tags = []
        if isinstance(tag_list, list):
            for tag in tag_list:
                if isinstance(tag, dict) and tag.get("name"):
                    tags.append(tag["name"])
                    
        interact = note.get("interactInfo", {})
        stats = {
            "liked": self.format_count(interact.get("likedCount")),
            "collected": self.format_count(interact.get("collectedCount")),
            "comment": self.format_count(interact.get("commentCount")),
            "share": self.format_count(interact.get("shareCount"))
        }
        
        t = note.get("time")
        if t:
            try:
                dt = datetime.fromtimestamp(float(t) / 1000)
                publish_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                timestamp = int(float(t) / 1000)
            except Exception:
                publish_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                timestamp = int(time.time())
        else:
            publish_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            timestamp = int(time.time())
            
        user_info = note.get("user", {})
        author_id = user_info.get("userId", "")
        author = {
            "user_id": author_id,
            "nickname": user_info.get("nickname", user_info.get("nickName", "未知博主")),
            "avatar": user_info.get("avatar", ""),
            "url": f"https://www.xiaohongshu.com/user/profile/{author_id}" if author_id else ""
        }
        
        note_type_raw = note.get("type", "normal")
        image_list = note.get("imageList", [])
        if not isinstance(image_list, list):
            image_list = []
            
        if note_type_raw == "video" or note.get("video"):
            note_type = "视频"
        else:
            note_type = "图文"
            
        images = []
        lives = []
        
        settings = get_settings()
        img_fmt = settings.get("xhs_image_format", "png")
        live_enabled = settings.get("xhs_download_live", True)
        
        for img in image_list:
            if not isinstance(img, dict):
                continue
            img_url = img.get("urlDefault") or img.get("url") or ""
            if img_url:
                try:
                    img_url = bytes(img_url, "utf-8").decode("unicode_escape")
                except Exception:
                    pass
                
                parts = img_url.split("/")
                if len(parts) >= 6:
                    token = "/".join(parts[5:]).split("!")[0]
                else:
                    token = parts[-1].split("!")[0]
                    
                if img_fmt == "auto":
                    final_img_url = f"https://sns-img-bd.xhscdn.com/{token}"
                else:
                    final_img_url = f"https://ci.xiaohongshu.com/{token}?imageView2/format/{img_fmt}"
                    
                images.append(final_img_url)
                
                live_url = None
                if live_enabled:
                    stream = img.get("stream")
                    if isinstance(stream, dict):
                        h264 = stream.get("h264")
                        if isinstance(h264, list) and len(h264) > 0:
                            item = h264[0]
                            if isinstance(item, dict):
                                live_url = item.get("masterUrl")
                lives.append(live_url)
                
        video = None
        video_candidates = []
        video_info = note.get("video") or note.get("media") or {}
        if isinstance(video_info, dict) and video_info:
            media = video_info.get("media", {}) if isinstance(video_info.get("media"), dict) else (video_info if "stream" in video_info else {})
            stream = media.get("stream", {}) if isinstance(media.get("stream"), dict) else (video_info.get("stream", {}) if isinstance(video_info.get("stream"), dict) else (note.get("stream", {}) if isinstance(note.get("stream"), dict) else {}))
            
            all_streams = []
            if isinstance(stream, dict):
                for k, v in stream.items():
                    if isinstance(v, list):
                        all_streams.extend(v)
                    elif isinstance(v, dict):
                        all_streams.append(v)

            def stream_sort_key(s):
                if not isinstance(s, dict):
                    return (-1, 0, 0)
                v_codec = str(s.get("videoCodec", "")).lower()
                s_desc = str(s.get("streamDesc", "")).lower()
                # 优先 H.264/AVC 编码（EF4 或包含 264），全设备与浏览器完美播放绝不黑屏
                is_h264 = 1 if (v_codec in ("ef4", "h264", "avc", "avc1") or "264" in s_desc or s.get("streamType") == 259) else 0
                res = float(s.get("height") or 0) * float(s.get("width") or 0) or float(s.get("height") or 0)
                size = float(s.get("size") or s.get("videoBitrate") or 0)
                return (is_h264, res, size)

            valid_streams = [s for s in all_streams if isinstance(s, dict)]
            if valid_streams:
                valid_streams.sort(key=stream_sort_key, reverse=True)
                for s in valid_streams:
                    master_url = s.get("masterUrl")
                    if master_url and master_url not in video_candidates:
                        video_candidates.append(master_url)
                    backup_urls = s.get("backupUrls", [])
                    if isinstance(backup_urls, list):
                        for bu in backup_urls:
                            if bu and bu not in video_candidates:
                                video_candidates.append(bu)
                    url_field = s.get("url") or s.get("mainUrl")
                    if url_field and url_field not in video_candidates:
                        video_candidates.append(url_field)

            origin_key = video_info.get("consumer", {}).get("originVideoKey")
            if origin_key:
                for host in ["sns-video-bd.xhscdn.com", "sns-video-al.xhscdn.com", "sns-video-hw.xhscdn.com", "sns-video-qc.xhscdn.com"]:
                    u = f"https://{host}/{origin_key}"
                    if u not in video_candidates:
                        video_candidates.append(u)

            for k in ["masterUrl", "url", "videoUrl", "mainUrl"]:
                u = video_info.get(k) or media.get(k)
                if u and u not in video_candidates:
                    video_candidates.append(u)

        if video_candidates:
            video = video_candidates[0]
            note_type = "视频"
                            
        cover = images[0] if images else ""
        if note_type == "视频":
            cover_info = note.get("cover", {})
            if isinstance(cover_info, dict):
                cover_url = cover_info.get("urlDefault") or cover_info.get("url")
                if cover_url:
                    try:
                        cover = bytes(cover_url, "utf-8").decode("unicode_escape")
                    except Exception:
                        cover = cover_url
                        
        token_match = re.search(r"xsec_token=([a-zA-Z0-9_\-]+)", url)
        xsec_token = token_match.group(1) if token_match else ""
        clean_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec_token:
            clean_url += f"?xsec_token={xsec_token}"
            
        return {
            "note_id": note_id,
            "note_url": clean_url,
            "title": title,
            "desc": desc,
            "type": note_type,
            "tags": tags,
            "author": author,
            "stats": stats,
            "publish_time": publish_time,
            "timestamp": timestamp,
            "images": images,
            "lives": lives,
            "video": video,
            "video_candidates": video_candidates,
            "cover": cover
        }

    def _parse_note_item(self, it: dict) -> dict | None:
        """把 user_posted API 或 SSR / DOM 的单条 note 解析成前端列表项；非 dict 返回 None。"""
        if not isinstance(it, dict):
            return None

        # 1. 提取 note_id 与 xsec_token
        note_card = it.get("noteCard") or it.get("note_card") or {}
        nid = it.get("note_id") or it.get("id") or note_card.get("noteId") or note_card.get("note_id") or ""
        xsec_token = it.get("xsec_token") or it.get("xsecToken") or note_card.get("xsecToken") or note_card.get("xsec_token") or ""

        # 2. 提取 title
        title = (
            it.get("display_title")
            or it.get("title")
            or it.get("displayTitle")
            or note_card.get("displayTitle")
            or note_card.get("display_title")
            or ""
        )

        # 3. 提取 cover
        cover_obj = it.get("cover") or note_card.get("cover") or {}
        if isinstance(cover_obj, str):
            cover = cover_obj
        elif isinstance(cover_obj, dict):
            cover = cover_obj.get("url_default") or cover_obj.get("urlDefault") or cover_obj.get("url") or ""
            if not cover:
                info_list = cover_obj.get("info_list") or cover_obj.get("infoList") or []
                if info_list:
                    cover = (info_list[-1] or {}).get("url", "")
        else:
            cover = ""

        # 4. 提取 interact_info (liked) 与 sticky (置顶标记)
        interact = (
            it.get("interact_info")
            or it.get("interactInfo")
            or note_card.get("interactInfo")
            or note_card.get("interact_info")
            or {}
        )
        if isinstance(interact, dict):
            liked_raw = interact.get("liked_count") or interact.get("likedCount") or it.get("liked", "0")
            sticky = bool(interact.get("sticky") or interact.get("is_sticky") or interact.get("isSticky"))
        else:
            liked_raw = str(interact) if interact else "0"
            sticky = bool(it.get("sticky") or note_card.get("sticky"))
        if not sticky:
            sticky = bool(it.get("sticky") or note_card.get("sticky"))
        liked = self.format_count(liked_raw)

        # 5. 提取 type
        note_type = "video" if (it.get("type") == "video" or note_card.get("type") == "video") else "normal"

        # 6. 提取 timestamp 与 publish_time
        t_raw = it.get("time") or note_card.get("time")
        timestamp = 0
        publish_time = ""
        if t_raw:
            try:
                t_sec = float(t_raw) / 1000.0 if float(t_raw) > 1e11 else float(t_raw)
                timestamp = int(t_sec)
                publish_time = datetime.fromtimestamp(t_sec).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        return {
            "note_id": nid,
            "xsec_token": xsec_token,
            "title": title,
            "cover": cover,
            "type": note_type,
            "liked": liked,
            "sticky": sticky,
            "timestamp": timestamp,
            "publish_time": publish_time,
        }

    def get_user_posted(self, user_id: str, xsec_token: str = "", cursor: str = "", xsec_source: str = "pc_feed") -> tuple:
        """用 xhshow 签名调用 user_posted API，返回 (notes_list, has_more, next_cursor)。
        签名失败 / 风控 / 空数据时抛异常，由调用方降级到 SSR 解析。"""
        if Xhshow is None:
            raise RuntimeError("未安装 xhshow，签名接口不可用")
        cookie = get_settings().get("xhs_cookie", "").strip()
        if not cookie:
            raise RuntimeError("签名接口需要登录 Cookie，请在登录页配置")
        if not user_id:
            raise ValueError("缺少 user_id")

        # 确保 xsec_token 被 URL 解码，防止双重 URL 编码导致签名/Token失效
        from urllib.parse import unquote
        xsec_token = unquote(xsec_token)

        uri = "/api/sns/web/v1/user_posted"
        params = {
            "num": "30",
            "cursor": cursor,
            "user_id": user_id,
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
            "xsec_source": xsec_source,
        }
        # 签名头按 xhshow 对 GET 的序列化规则（quote(value, safe=",")）计算，
        # 发送的查询串必须与签名时完全一致，否则 406。
        # 统一 User-Agent，防止签名与请求头不一致触发风控。
        from xhshow.config.config import CryptoConfig
        config = CryptoConfig().with_overrides(PUBLIC_USERAGENT=self.USER_AGENT)
        headers = Xhshow(config).sign_headers_get(
            uri=uri, cookies=cookie, params=params, sign_format="xyw", user_id=user_id
        )
        headers["cookie"] = cookie
        headers["user-agent"] = self.USER_AGENT
        headers["referer"] = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        headers["accept"] = "application/json, text/plain, */*"
        headers["accept-language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        headers["sec-ch-ua"] = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = '"Windows"'
        headers["origin"] = "https://www.xiaohongshu.com"
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
        headers["sec-fetch-site"] = "same-site"

        query = "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in params.items())
        full_url = "https://edith.xiaohongshu.com" + uri + "?" + query

        proxies = get_proxies_dict()
        proxy_url = proxies.get("http") if proxies else None
        lognormal_sleep(avg_delay=4.5)
        try:
            resp = curl_requests.get(
                full_url, headers=headers, impersonate="chrome124",
                proxy=proxy_url, timeout=20
            )
            if proxy_url:
                report_proxy_status(proxy_url, success=True)
        except Exception as e:
            if proxy_url:
                report_proxy_status(proxy_url, success=False)
            raise

        try:
            body = resp.json()
        except Exception:
            raise ValueError(f"签名接口返回非 JSON（HTTP {resp.status_code}）")

        data = body.get("data") or {}
        raw = data.get("notes") or []
        if not raw:
            raise ValueError(
                f"签名接口未返回笔记（HTTP {resp.status_code}，msg={body.get('msg')}），可能触发风控或 Cookie 失效"
            )

        notes_list = []
        for it in raw:
            parsed = self._parse_note_item(it)
            if parsed:
                notes_list.append(parsed)
        return notes_list, bool(data.get("has_more")), data.get("cursor", "")

    def _parse_ssr_notes(self, state: dict) -> list:
        """从主页 __INITIAL_STATE__ 解析首屏笔记（降级用）。
        注意：小红书已从主页 SSR 中移除 note_id，此结果仅供浏览、无法下载。"""
        user_data = state.get("user", {})
        raw_notes = user_data.get("notes", [])
        flat_notes = []
        if isinstance(raw_notes, list):
            for group in raw_notes:
                if isinstance(group, list):
                    flat_notes.extend(group)
                elif isinstance(group, dict):
                    flat_notes.append(group)

        notes_list = []
        for item in flat_notes:
            if not isinstance(item, dict):
                continue
            note_card = item.get("noteCard", {})
            cover = note_card.get("cover", {}).get("urlDefault", "")
            try:
                cover = bytes(cover, "utf-8").decode("unicode_escape")
            except Exception:
                pass
            interact = note_card.get("interactInfo", {})
            liked = self.format_count(interact.get("likedCount", "0")) if isinstance(interact, dict) else "0"
            sticky = bool(interact.get("sticky")) if isinstance(interact, dict) else False
            t_raw = item.get("time") or note_card.get("time")
            timestamp = 0
            publish_time = ""
            if t_raw:
                try:
                    t_sec = float(t_raw) / 1000.0 if float(t_raw) > 1e11 else float(t_raw)
                    timestamp = int(t_sec)
                    publish_time = datetime.fromtimestamp(t_sec).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            notes_list.append({
                "note_id": item.get("id", "") or note_card.get("noteId", ""),
                "xsec_token": item.get("xsecToken", ""),
                "title": note_card.get("displayTitle", ""),
                "cover": cover,
                "type": "video" if note_card.get("type") == "video" else "normal",
                "liked": liked,
                "sticky": sticky,
                "timestamp": timestamp,
                "publish_time": publish_time,
            })
        return notes_list

    def get_user_profile(self, url: str) -> dict:
        url = self.extract_profile_url(url)
        state = self.get_initial_state(url)
        user_data = state.get("user", {})
        page_data = user_data.get("userPageData", {})
        basic_info = page_data.get("basicInfo", {})

        nickname = basic_info.get("nickname", "").strip()
        avatar = basic_info.get("images", "").strip()
        desc = basic_info.get("desc", "").strip()
        red_id = basic_info.get("redId", "").strip()

        fans = "0"
        interactions = page_data.get("interactions", [])
        if isinstance(interactions, list):
            for item in interactions:
                if isinstance(item, dict) and item.get("type") == "fans":
                    fans = self.format_count(item.get("count", "0"))
                    break

        user_id = basic_info.get("userId", "")
        if not user_id:
            path_parts = urlparse(url).path.split("/")
            if len(path_parts) >= 4 and path_parts[2] == "profile":
                user_id = path_parts[3]

        user_obj = {
            "user_id": user_id,
            "nickname": nickname,
            "avatar": avatar,
            "desc": desc,
            "red_id": red_id,
            "fans": fans,
            "url": url
        }

        from urllib.parse import unquote
        token_match = re.search(r"xsec_token=([^&]+)", url)
        xsec_token = unquote(token_match.group(1)) if token_match else ""

        source_match = re.search(r"xsec_source=([^&]+)", url)
        xsec_source = source_match.group(1) if source_match else "pc_feed"

        # 优先用浏览器拦截（签名由页面 JS 生成，不依赖 xhshow）；
        # 失败时尝试 xhshow 签名接口；最后降级 SSR（无 note_id，仅供浏览）。
        result = {"user": user_obj}
        try:
            notes_list, browser_has_more = _session_manager.get_first_batch(user_id)
            result["notes"] = notes_list
            result["has_more"] = browser_has_more
        except Exception as browser_err:
            print(f"浏览器拦截获取笔记列表失败: {browser_err}")
            try:
                notes_list, _has_more, _cursor = self.get_user_posted(user_id, xsec_token, xsec_source=xsec_source)
                result["notes"] = notes_list
            except Exception as e:
                result["notes"] = self._parse_ssr_notes(state)
                result["warning"] = (
                    f"笔记列表获取失败（浏览器: {browser_err}；签名: {e}）；已降级为主页首屏解析，"
                    "该列表缺少笔记 ID 无法下载，请改用「链接下载」逐条下载。"
            )
        return result

    def download_file(self, url: str, save_path: Path) -> int:
        headers = self.get_headers()
        proxies = get_proxies_dict()
        proxy_url = proxies.get("http") if proxies else None
        
        tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
        try:
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(url, headers=headers, proxies=proxies, stream=True, timeout=30)
            if r.status_code != 200:
                # 尝试不带 Referer/Cookie 的标准请求头重试（部分 CDN 域名对跨域 Referer 拦截）
                clean_headers = {
                    "User-Agent": self.USER_AGENT,
                    "Accept": "*/*",
                }
                r = requests.get(url, headers=clean_headers, proxies=proxies, stream=True, timeout=30)

            if r.status_code == 200:
                with open(tmp_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                if save_path.exists():
                    save_path.unlink()
                tmp_path.rename(save_path)
                if proxy_url:
                    report_proxy_status(proxy_url, success=True)
                return save_path.stat().st_size
            else:
                raise Exception(f"HTTP 状态码: {r.status_code}")
        except Exception as e:
            if proxy_url:
                report_proxy_status(proxy_url, success=False)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise e

# ── 持久化浏览器会话（博主笔记「加载更多」用）──────────────────
class _XhsBrowserSession:
    """单个博主主页的常驻 headless 浏览器会话。

    sync Playwright 对象绑定其创建线程，Flask 各请求线程不同，故由一个专属
    worker 线程持有浏览器，通过命令队列接收「继续滚动 / 关闭」。这样「加载更多」
    复用同一活页面继续往下滚，cursor 连续、按 note_id 天然去重，O(1) 续抓。"""

    def __init__(self, user_id: str, cookie: str, user_agent: str):
        self.user_id = user_id
        self.last_used = time.time()
        self._cookie = cookie
        self._user_agent = user_agent
        self._client = XhsClient()          # 仅借用 _parse_note_item / format_count
        self._cmd_q = queue.Queue()
        self._captured = []                 # worker 线程独占：累计的原始 note dict
        self._seen_ids = set()              # 已返回给调用方的 note_id
        self._pages_loaded = 0              # 累计观测到的 user_posted 响应数
        self._has_more = True
        self._ready = threading.Event()
        self._init_error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=45)
        if self._init_error:
            raise self._init_error
        if not self._ready.is_set():
            # worker 仍卡在 goto，未就绪：入队 close，等它脱困后自行关闭浏览器，避免孤儿进程。
            self.close()
            raise RuntimeError("浏览器会话初始化超时")

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def close(self):
        try:
            self._cmd_q.put(("close",))
        except Exception:
            pass

    def scroll_more(self, max_pages: int = 3, timeout: int = 90) -> tuple:
        """向 worker 请求多滚 max_pages 页，返回 (新增 notes, has_more)。"""
        holder = queue.Queue(maxsize=1)
        self._cmd_q.put(("scroll", max_pages, holder))
        try:
            ok, payload = holder.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("浏览器会话无响应（可能已崩溃）")
        self.last_used = time.time()
        if not ok:
            raise payload
        return payload

    def _cookie_pairs(self) -> list:
        pairs = []
        for part in self._cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs.append({
                    "name": k.strip(), "value": v.strip(),
                    "domain": ".xiaohongshu.com", "path": "/"
                })
        return pairs

    def _on_response(self, response):
        if "/api/sns/web/v1/user_posted" not in response.url:
            return
        try:
            body = response.json()
            data = body.get("data") or {}
            self._pages_loaded += 1
            self._captured.extend(data.get("notes") or [])
            if not data.get("has_more"):
                self._has_more = False
        except Exception:
            pass

    def _do_scroll(self, page, max_pages: int) -> tuple:
        target = self._pages_loaded + max_pages
        for _ in range(max_pages * 6 + 3):
            if not self._has_more or self._pages_loaded >= target:
                break
            page.mouse.wheel(0, 1000)
            page.wait_for_timeout(2000)

        fresh = []
        for it in self._captured:
            parsed = self._client._parse_note_item(it)
            if not parsed:
                continue
            nid = parsed["note_id"]
            if nid and nid in self._seen_ids:
                continue
            if nid:
                self._seen_ids.add(nid)
            fresh.append(parsed)
        return fresh, self._has_more

    def _run(self):
        from playwright.sync_api import sync_playwright
        from backend.runtime import launch_chromium

        profile_url = f"https://www.xiaohongshu.com/user/profile/{self.user_id}"
        try:
            with sync_playwright() as p:
                browser = launch_chromium(
                    p.chromium, headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                try:
                    context = browser.new_context(
                        viewport={'width': 1280, 'height': 800},
                        user_agent=self._user_agent
                    )
                    context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                    )
                    context.add_cookies(self._cookie_pairs())
                    page = context.new_page()
                    page.on("response", self._on_response)
                    page.goto(profile_url, wait_until="networkidle", timeout=30000)

                    # 提取首屏（第 1 页）SSR 及 DOM 笔记
                    try:
                        initial_notes = page.evaluate("""() => {
                            const list = [];
                            const seen = new Set();

                            const unwrap = (obj) => {
                                if (!obj) return [];
                                if (Array.isArray(obj)) return obj;
                                if (obj._value && Array.isArray(obj._value)) return obj._value;
                                if (obj._rawValue && Array.isArray(obj._rawValue)) return obj._rawValue;
                                if (obj.value && Array.isArray(obj.value)) return obj.value;
                                return [];
                            };

                            // 1. 从 __INITIAL_STATE__ / __INITIAL_DATA__ 中解析（包含 Vue3 ref 响应式对象解包）
                            try {
                                const state = window.__INITIAL_STATE__ || window.__INITIAL_DATA__;
                                if (state && state.user) {
                                    const rawNotes = unwrap(state.user.notes);
                                    const flat = [];
                                    for (const item of rawNotes) {
                                        const unwrappedItem = unwrap(item);
                                        if (unwrappedItem.length > 0) {
                                            flat.push(...unwrappedItem);
                                        } else if (item && typeof item === 'object') {
                                            flat.push(item);
                                        }
                                    }
                                    for (const item of flat) {
                                        if (!item || typeof item !== 'object') continue;
                                        const card = item.noteCard || item.note_card || {};
                                        const coverObj = card.cover || item.cover || {};
                                        const cover = (coverObj.urlDefault || coverObj.url_default || coverObj.url) || 
                                                      ((coverObj.infoList && coverObj.infoList[0]) ? coverObj.infoList[0].url : '') || '';
                                        const interact = card.interactInfo || card.interact_info || {};
                                        const nid = item.id || item.noteId || item.note_id || card.noteId || card.note_id || '';
                                        const title = card.displayTitle || card.display_title || '';
                                        const token = item.xsecToken || item.xsec_token || card.xsecToken || card.xsec_token || '';
                                        const isSticky = !!(interact.sticky || interact.is_sticky || card.sticky || item.sticky);
                                        const rawTime = card.time || item.time || 0;
                                        if (nid && !seen.has(nid)) {
                                            list.push({
                                                note_id: nid,
                                                xsec_token: token,
                                                display_title: title,
                                                cover: cover,
                                                time: rawTime,
                                                type: (card.type === 'video' || item.type === 'video') ? 'video' : 'normal',
                                                interact_info: {
                                                    liked_count: String(interact.likedCount || interact.liked_count || '0'),
                                                    sticky: isSticky
                                                },
                                                sticky: isSticky
                                            });
                                            seen.add(nid);
                                        }
                                    }
                                }
                            } catch(e) {
                                console.error('SSR extract error:', e);
                            }

                            // 2. 检查 DOM 补充缺失的 note_id / xsec_token
                            try {
                                const items = document.querySelectorAll('section.note-item, .note-item, div[class*="note-item"]');
                                items.forEach((el, idx) => {
                                    const link = el.querySelector('a[href*="/explore/"]') || el.querySelector('a');
                                    const href = link ? (link.getAttribute('href') || '') : '';
                                    let nid = '';
                                    let token = '';
                                    const match = href.match(/\\/explore\\/([a-zA-Z0-9_-]+)/);
                                    if (match) nid = match[1];
                                    const tokenMatch = href.match(/xsec_token=([^&]+)/);
                                    if (tokenMatch) token = decodeURIComponent(tokenMatch[1]);

                                    if (idx < list.length && !list[idx].note_id && nid) {
                                        list[idx].note_id = nid;
                                        if (token && !list[idx].xsec_token) list[idx].xsec_token = token;
                                        seen.add(nid);
                                    } else if (nid && !seen.has(nid)) {
                                        const img = el.querySelector('img');
                                        const cover = img ? (img.src || img.getAttribute('data-src') || '') : '';
                                        const titleEl = el.querySelector('.title, .footer .title, span.title, [class*="title"]');
                                        const title = titleEl ? (titleEl.textContent || '').trim() : '';
                                        const likeEl = el.querySelector('.like-wrapper, .count, [class*="like"], [class*="count"]');
                                        const liked = likeEl ? (likeEl.textContent || '').trim() : '0';
                                        const isVideo = !!el.querySelector('.play-icon, [class*="play"], svg[class*="play"]');
                                        const isSticky = !!el.querySelector('[class*="sticky"], [class*="top"], .note-item-top');
                                        list.push({
                                            note_id: nid,
                                            xsec_token: token,
                                            display_title: title,
                                            cover: cover,
                                            type: isVideo ? 'video' : 'normal',
                                            interact_info: {
                                                liked_count: liked,
                                                sticky: isSticky
                                            },
                                            sticky: isSticky
                                        });
                                        seen.add(nid);
                                    }
                                });
                            } catch(e) {
                                console.error('DOM extract error:', e);
                            }

                            return list;
                        }""")
                        if initial_notes:
                            self._captured = initial_notes + self._captured
                            self._pages_loaded += 1
                    except Exception as e:
                        print(f"提取首屏笔记异常: {e}")

                    if self._pages_loaded == 0:
                        page.wait_for_timeout(3000)
                    if self._pages_loaded == 0:
                        for _ in range(3):
                            page.mouse.wheel(0, 800)
                            page.wait_for_timeout(2000)
                            if self._pages_loaded > 0:
                                break

                    self._ready.set()

                    while True:
                        cmd = self._cmd_q.get()
                        if cmd[0] == "close":
                            break
                        if cmd[0] == "scroll":
                            _, max_pages, holder = cmd
                            try:
                                holder.put((True, self._do_scroll(page, max_pages)))
                            except Exception as e:
                                holder.put((False, e))
                finally:
                    browser.close()
        except Exception as e:
            self._init_error = e
            self._ready.set()


class _XhsSessionManager:
    """模块级单例：管理常驻浏览器会话。单活策略（界面一次只看一个博主，
    ≤1 个浏览器），空闲超时由 reaper 守护线程回收。"""

    IDLE_TIMEOUT = 120  # 秒

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reap_loop, daemon=True).start()

    def _new_session(self, user_id: str) -> _XhsBrowserSession:
        cookie = get_settings().get("xhs_cookie", "").strip()
        if not cookie:
            raise RuntimeError("需要登录 Cookie")
        return _XhsBrowserSession(user_id, cookie, XhsClient.USER_AGENT)

    def get_first_batch(self, user_id: str, first_pages: int = 3) -> tuple:
        """选择/刷新博主：始终新建会话从主页顶部开始，关闭其它所有会话。"""
        with self._lock:
            for sess in self._sessions.values():
                sess.close()
            self._sessions.clear()
            sess = self._new_session(user_id)
            self._sessions[user_id] = sess
        return sess.scroll_more(first_pages)

    def get_more(self, user_id: str, max_pages: int = 3, loaded_count: int = 0) -> tuple:
        """加载更多：复用活会话续滚；会话已被回收/崩溃则重建并滚到已展示深度之后。"""
        with self._lock:
            sess = self._sessions.get(user_id)
            if sess and not sess.is_alive():
                self._sessions.pop(user_id, None)
                sess = None
            rebuilt = sess is None
            if rebuilt:
                sess = self._new_session(user_id)
                self._sessions[user_id] = sess
        if rebuilt:
            # 新会话从顶部开始：滚到已展示页数之后，再靠前端 note_id 去重丢弃重叠。
            catch_up = (loaded_count // 30) + max_pages
            return sess.scroll_more(catch_up)
        return sess.scroll_more(max_pages)

    def _reap_loop(self):
        while True:
            time.sleep(30)
            now = time.time()
            with self._lock:
                for uid, sess in list(self._sessions.items()):
                    if not sess.is_alive() or now - sess.last_used > self.IDLE_TIMEOUT:
                        sess.close()
                        self._sessions.pop(uid, None)

    def shutdown(self):
        """程序优雅退出时关闭所有常驻浏览器（由 atexit 调用）。"""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            sess.close()
        for sess in sessions:
            sess.join(timeout=5)


_session_manager = _XhsSessionManager()
atexit.register(_session_manager.shutdown)

# ── 登录态校验 ────────────────────────────────────────────
def check_xhs_login(cookie: str) -> dict:
    """用 xhshow 签名调 /user/me 判定登录态。
    返回 {logged_in, guest, user_id}；logged_in 为 None 表示无法判定（未装 xhshow / 网络失败）。
    注意：小红书对游客也会下发 web_session，故不能用 web_session 是否存在判断登录。"""
    cookie = (cookie or "").strip()
    if not cookie:
        return {"logged_in": False, "guest": True, "user_id": ""}
    if Xhshow is None:
        return {"logged_in": None, "guest": None, "user_id": "", "error": "未安装 xhshow"}
    uri = "/api/sns/web/v2/user/me"
    try:
        # 统一 User-Agent，防止签名与请求头不一致触发风控。
        from xhshow.config.config import CryptoConfig
        config = CryptoConfig().with_overrides(PUBLIC_USERAGENT=XhsClient.USER_AGENT)
        headers = Xhshow(config).sign_headers_get(uri=uri, cookies=cookie, params={}, sign_format="xyw")
        headers["cookie"] = cookie
        headers["user-agent"] = XhsClient.USER_AGENT
        headers["referer"] = "https://www.xiaohongshu.com/"
        headers["accept"] = "application/json, text/plain, */*"
        headers["accept-language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        headers["sec-ch-ua"] = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = '"Windows"'
        headers["origin"] = "https://www.xiaohongshu.com"
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
        headers["sec-fetch-site"] = "same-site"
        proxies = get_proxies_dict()
        proxy_url = proxies.get("http") if proxies else None
        resp = curl_requests.get(
            "https://edith.xiaohongshu.com" + uri, headers=headers,
            impersonate="chrome124", proxy=proxy_url, timeout=15
        )
        body = resp.json()
        inner = body.get("data") or {}
        if body.get("success") and "guest" in inner:
            guest = bool(inner.get("guest", True))
            return {"logged_in": (not guest), "guest": guest, "user_id": inner.get("user_id", "")}
        # success:false 或缺 guest 字段（如「登录已过期」「账号存在异常」）→ 无法确认登录态，
        # 不能据此判为游客（避免把真登录误判成游客）。
        return {"logged_in": None, "guest": None, "user_id": "", "error": body.get("msg") or f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"logged_in": None, "guest": None, "user_id": "", "error": str(e)}

# ── 线程下载工作逻辑 ───────────────────────────────────────
def _do_xhs_download_thread(task_id: str, urls: list, account_name: str):
    client = XhsClient()
    history = load_json(XHS_HISTORY_FILE, [])
    
    downloaded_ids = {item.get("note_id") for item in history if isinstance(item, dict) and item.get("success")}
    
    for idx, url in enumerate(urls):
        with _download_lock:
            task = _download_tasks.get(task_id)
            if not task:
                return
            if task.get("cancel_requested"):
                task["status"] = "cancelled"
                task["end_time"] = time.time()
                task["current"] = "下载已取消"
                save_json(XHS_HISTORY_FILE, history)
                return
                
        note_id = client.extract_note_id(url)
        
        if note_id and note_id in downloaded_ids:
            with _download_lock:
                task = _download_tasks.get(task_id)
                if task:
                    task["skipped"] += 1
                    task["current"] = f"跳过已下载: {note_id}"
                    task["results"].append({
                        "title": f"Note_{note_id}",
                        "success": True,
                        "skipped": True
                    })
            continue
            
        with _download_lock:
            task = _download_tasks.get(task_id)
            if task:
                task["current"] = f"正在解析第 {idx + 1}/{len(urls)} 篇..."
                
        try:
            detail = client.get_note_detail(url)
            author_nick = clean_filename(detail["author"]["nickname"])
            pub_date = detail["publish_time"].split()[0]
            clean_title = clean_filename(detail["title"]) or detail["note_id"]
            
            note_dir = XHS_DIR / author_nick / f"{pub_date}_{clean_title}"
            note_dir.mkdir(parents=True, exist_ok=True)

            # 写入笔记文字内容（标题/正文/标签/数据），与媒体文件一同保存
            text_size = write_note_text(note_dir, detail)

            with _download_lock:
                task = _download_tasks.get(task_id)
                if task:
                    task["current"] = f"正在下载: {clean_title}..."

            success = True
            error_msg = None
            total_size = text_size
            video_file_name = None      # 用于生成 HTML：本地视频文件名
            image_items = []            # 用于生成 HTML：[{img, live}]

            settings = get_settings()
            img_fmt = settings.get("xhs_image_format", "png")
            img_ext = img_fmt if img_fmt != "auto" else "jpg"
            if img_ext == "jpeg":
                img_ext = "jpg"

            if detail["type"] == "视频" or detail.get("video") or detail.get("video_candidates"):
                candidates = detail.get("video_candidates") or ([detail["video"]] if detail.get("video") else [])
                if candidates:
                    save_file = note_dir / f"{clean_title}.mp4"
                    last_download_err = None
                    video_ok = False
                    for v_url in candidates:
                        try:
                            size = client.download_file(v_url, save_file)
                            if size > 0:
                                total_size += size
                                video_file_name = save_file.name
                                video_ok = True
                                break
                        except Exception as e:
                            last_download_err = e
                            continue
                    if video_ok:
                        # 尝试将视频上传到腾讯云 COS (支持失败自动重试3次)
                        cos_url, cos_err = upload_xhs_video_to_cos(save_file, detail["note_id"], return_error=True)
                        if cos_url:
                            detail["cos_url"] = cos_url
                            detail["video_url"] = cos_url
                        elif cos_err:
                            detail["cos_error"] = cos_err
                    else:
                        success = False
                        error_msg = f"视频下载失败: {last_download_err or '所有备选视频源均无法下载'}"
                else:
                    success = False
                    error_msg = "未找到可用无水印视频链接"
            else:
                for img_idx, img_url in enumerate(detail["images"]):
                    save_file = note_dir / f"{img_idx + 1:02d}.{img_ext}"
                    try:
                        size = client.download_file(img_url, save_file)
                        total_size += size
                    except Exception as e:
                        success = False
                        error_msg = f"图片 {img_idx + 1} 下载失败: {str(e)}"
                        break

                    item = {"img": save_file.name, "live": None}
                    live_url = detail["lives"][img_idx] if img_idx < len(detail["lives"]) else None
                    if live_url:
                        live_file = note_dir / f"{img_idx + 1:02d}.mp4"
                        try:
                            time.sleep(random.uniform(0.3, 0.8))
                            size = client.download_file(live_url, live_file)
                            total_size += size
                            item["live"] = live_file.name
                        except Exception as e:
                            print(f"Warning: Live Photo download failed: {e}")

                    image_items.append(item)
                    time.sleep(random.uniform(0.3, 0.8))

            # 生成可直接打开的 HTML（引用本地已下载媒体）
            total_size += write_note_html(note_dir, detail, video_file_name, image_items)

            # 生成与公众号对齐的 data.json（包含视频 COS 链接与文字描述合并内容）
            data_json = write_note_data_json(note_dir, detail, cos_url=detail.get("cos_url"), video_file=video_file_name, image_items=image_items)

            # 如果开启了服务器上传，像公众号一样推送到远端服务器
            uploaded_ok = False
            upload_err = None
            if success:
                uploaded_ok, upload_err = post_xhs_note_to_server(data_json)
                    
            with _download_lock:
                task = _download_tasks.get(task_id)
                if task:
                    if success:
                        task["completed"] += 1
                        task["results"].append({
                            "title": detail["title"] or detail["note_id"],
                            "success": True,
                            "path": str(note_dir),
                            "cos_url": detail.get("cos_url", ""),
                            "uploaded": uploaded_ok
                        })
                    else:
                        task["failed"] += 1
                        task["results"].append({
                            "title": detail["title"] or detail["note_id"],
                            "success": False,
                            "error": error_msg
                        })
                        
            history.append({
                "title": detail["title"] or detail["note_id"],
                "note_id": detail["note_id"],
                "type": detail["type"],
                "author": detail["author"]["nickname"],
                "path": str(note_dir),
                "size": total_size,
                "time": time.time(),
                "success": success,
                "error": error_msg,
                "cos_url": detail.get("cos_url", ""),
                "cos_error": detail.get("cos_error", None),
                "uploaded": uploaded_ok if success else False,
                "upload_error": upload_err if (success and not uploaded_ok) else None,
                "upload_time": time.time() if (success and uploaded_ok) else None,
            })
            save_json(XHS_HISTORY_FILE, history)
            
        except Exception as e:
            err_str = str(e)
            with _download_lock:
                task = _download_tasks.get(task_id)
                if task:
                    task["failed"] += 1
                    task["results"].append({
                        "title": f"Link_{idx + 1}",
                        "success": False,
                        "error": err_str
                    })
                    
            history.append({
                "title": f"解析失败链接_{idx + 1}",
                "note_id": note_id or f"failed_{idx}",
                "type": "未知",
                "author": "未知",
                "path": "",
                "size": 0,
                "time": time.time(),
                "success": False,
                "error": err_str
            })
            save_json(XHS_HISTORY_FILE, history)
            
        if idx < len(urls) - 1:
            lognormal_sleep(avg_delay=5.0)
            
    with _download_lock:
        task = _download_tasks.get(task_id)
        if task:
            if task["status"] == "cancelling":
                task["status"] = "cancelled"
            else:
                task["status"] = "completed"
            task["end_time"] = time.time()
            task["current"] = ""

# ── API 路由 ──────────────────────────────────────────────
@xhs_bp.route("/parse", methods=["POST"])
def parse_url():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "请输入小红书链接"}), 400
        
    client = XhsClient()
    try:
        links = client.extract_links(url)
        if not links:
            return jsonify({"error": "未能从输入中提取到有效小红书链接，请确保包含explore或discovery链接"}), 400
        
        detail = client.get_note_detail(links[0])
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": f"解析失败: {str(e)}"}), 500

@xhs_bp.route("/download", methods=["POST"])
def download_urls():
    data = request.get_json() or {}
    urls = data.get("urls", [])
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split("\n") if u.strip()]
        
    if not urls:
        return jsonify({"error": "请输入小红书链接"}), 400
        
    client = XhsClient()
    extracted_urls = []
    for u in urls:
        extracted_urls.extend(client.extract_links(u))
        
    if not extracted_urls:
        return jsonify({"error": "未能从输入中提取到任何有效小红书链接"}), 400
        
    task_id = f"xhs_{int(time.time())}"
    with _download_lock:
        _download_tasks[task_id] = {
            "status": "running",
            "total": len(extracted_urls),
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "current": "正在初始化任务...",
            "results": [],
            "start_time": time.time(),
            "cancel_requested": False
        }
        
    thread = threading.Thread(
        target=_do_xhs_download_thread,
        args=(task_id, extracted_urls, "url_download"),
        daemon=True
    )
    thread.start()
    
    return jsonify({"task_id": task_id, "message": f"已启动下载任务，共 {len(extracted_urls)} 个链接", "count": len(extracted_urls)})

@xhs_bp.route("/accounts", methods=["GET"])
def list_accounts():
    accounts = load_json(XHS_ACCOUNTS_FILE, [])
    return jsonify({"accounts": accounts, "total": len(accounts)})

@xhs_bp.route("/accounts/parse", methods=["POST"])
def parse_user_url():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "请输入博主主页链接"}), 400
        
    client = XhsClient()
    try:
        profile = client.get_user_profile(url)
        return jsonify(profile)
    except Exception as e:
        return jsonify({"error": f"解析博主主页失败: {str(e)}"}), 500

@xhs_bp.route("/accounts", methods=["POST"])
def add_account():
    user = request.get_json() or {}
    user_id = user.get("user_id", "").strip()
    nickname = user.get("nickname", "").strip()
    if not user_id or not nickname:
        return jsonify({"error": "博主 ID 和昵称不能为空"}), 400
        
    accounts = load_json(XHS_ACCOUNTS_FILE, [])
    for acc in accounts:
        if acc.get("user_id") == user_id:
            return jsonify({"error": "该博主已在收藏中"}), 400
            
    user["added_time"] = time.time()
    accounts.append(user)
    save_json(XHS_ACCOUNTS_FILE, accounts)
    return jsonify({"message": "添加成功", "account": user})

@xhs_bp.route("/accounts/<user_id>", methods=["DELETE"])
def remove_account(user_id):
    accounts = load_json(XHS_ACCOUNTS_FILE, [])
    new_accounts = [a for a in accounts if a.get("user_id") != user_id]
    if len(new_accounts) == len(accounts):
        return jsonify({"error": "未找到该博主"}), 404
        
    save_json(XHS_ACCOUNTS_FILE, new_accounts)
    return jsonify({"message": "已取消收藏"})

@xhs_bp.route("/accounts/<user_id>/notes", methods=["GET"])
def list_user_notes(user_id):
    accounts = load_json(XHS_ACCOUNTS_FILE, [])
    url = None
    for acc in accounts:
        if acc.get("user_id") == user_id:
            url = acc.get("url")
            break

    if not url:
        url = f"https://www.xiaohongshu.com/user/profile/{user_id}"

    client = XhsClient()
    try:
        profile = client.get_user_profile(url)
        return jsonify({
            "notes": profile.get("notes", []),
            "has_more": profile.get("has_more", False),
            "warning": profile.get("warning")
        })
    except Exception as e:
        return jsonify({"error": f"获取博主笔记失败: {str(e)}"}), 500

@xhs_bp.route("/accounts/<user_id>/notes/more", methods=["POST"])
def load_more_notes(user_id):
    data = request.get_json() or {}
    loaded_count = data.get("loaded", 0)

    try:
        notes_list, has_more = _session_manager.get_more(user_id, loaded_count=loaded_count)
        return jsonify({"notes": notes_list, "has_more": has_more})
    except Exception as e:
        return jsonify({"error": f"加载更多笔记失败: {str(e)}"}), 500

@xhs_bp.route("/download-notes", methods=["POST"])
def download_notes():
    data = request.get_json() or {}
    notes = data.get("notes", [])
    account_name = data.get("account_name", "unknown")
    if not notes:
        return jsonify({"error": "没有选中要下载的笔记"}), 400
        
    urls = []
    for note in notes:
        nid = note.get("note_id")
        token = note.get("xsec_token")
        if nid:
            url = f"https://www.xiaohongshu.com/explore/{nid}"
            if token:
                url += f"?xsec_token={token}&xsec_source=pc_user"
            urls.append(url)
            
    if not urls:
        return jsonify({"error": "选中的笔记缺少笔记 ID，无法下载（博主列表可能来自降级解析）。请改用「链接下载」逐条下载。"}), 400
        
    task_id = f"xhs_{int(time.time())}"
    with _download_lock:
        _download_tasks[task_id] = {
            "status": "running",
            "total": len(urls),
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "current": "正在初始化任务...",
            "results": [],
            "start_time": time.time(),
            "cancel_requested": False
        }
        
    thread = threading.Thread(
        target=_do_xhs_download_thread,
        args=(task_id, urls, account_name),
        daemon=True
    )
    thread.start()
    
    return jsonify({"task_id": task_id})

@xhs_bp.route("/download-status/<task_id>", methods=["GET"])
def get_task_status(task_id):
    with _download_lock:
        task = _download_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)

@xhs_bp.route("/download-cancel/<task_id>", methods=["POST"])
def cancel_task(task_id):
    with _download_lock:
        task = _download_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] != "running":
        return jsonify({"message": "任务已结束"})
        
    task["cancel_requested"] = True
    task["status"] = "cancelling"
    return jsonify({"message": "正在取消任务..."})

@xhs_bp.route("/history", methods=["GET"])
def get_history():
    history = load_json(XHS_HISTORY_FILE, [])
    limit = request.args.get("limit", 100, type=int)
    
    indexed_history = []
    for idx, item in enumerate(history):
        if isinstance(item, dict):
            indexed = dict(item)
            indexed["_index"] = idx
            indexed_history.append(indexed)
            
    indexed_history.sort(key=lambda x: x.get("time", 0), reverse=True)
    if limit > 0:
        indexed_history = indexed_history[:limit]
        
    return jsonify({"history": indexed_history, "total": len(history)})

@xhs_bp.route("/history", methods=["DELETE"])
def clear_history():
    save_json(XHS_HISTORY_FILE, [])
    return jsonify({"message": "历史已清空"})

@xhs_bp.route("/history/<int:index>", methods=["DELETE"])
def delete_history_item(index):
    history = load_json(XHS_HISTORY_FILE, [])
    if index < 0 or index >= len(history):
        return jsonify({"error": "历史记录不存在"}), 404
        
    item = history[index]
    path_str = item.get("path", "") if isinstance(item, dict) else ""
    file_status = "no_path"
    
    if path_str:
        try:
            path = Path(path_str)
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                file_status = "deleted"
            else:
                file_status = "missing"
        except Exception as e:
            file_status = f"error: {str(e)}"
            
    history.pop(index)
    save_json(XHS_HISTORY_FILE, history)
    
    messages = {
        "deleted": "已删除下载文件和记录",
        "missing": "下载文件已不存在，已删除记录",
        "no_path": "记录没有文件路径，已删除记录",
    }
    msg = messages.get(file_status, f"下载文件清理失败（{file_status}），但已清除历史记录")
    return jsonify({"message": msg, "file_status": file_status})

@xhs_bp.route("/open-folder", methods=["POST"])
def open_folder():
    import subprocess
    import sys
    import os
    
    data = request.get_json() or {}
    account = data.get("account", "").strip()
    
    path = XHS_DIR
    if account:
        path = path / clean_filename(account)
        
    try:
        path.mkdir(parents=True, exist_ok=True)
        resolved_path = str(path.resolve())
        if sys.platform == "darwin":
            subprocess.run(["open", resolved_path])
        elif sys.platform == "win32":
            if hasattr(os, "startfile"):
                os.startfile(resolved_path)
            else:
                subprocess.run(f'explorer "{resolved_path}"', shell=True)
        else:
            subprocess.run(["xdg-open", resolved_path])
        return jsonify({"message": "文件夹已打开"})
    except Exception as e:
        return jsonify({"error": f"打开文件夹失败: {str(e)}"}), 500

@xhs_bp.route("/open-file", methods=["POST"])
def open_file():
    import subprocess
    import sys
    import os
    
    data = request.get_json() or {}
    path_str = data.get("path", "")
    if not path_str:
        return jsonify({"error": "路径不能为空"}), 400
        
    try:
        path = Path(path_str)
        if not path.exists():
            return jsonify({"error": "文件或文件夹不存在"}), 404
            
        resolved_path = str(path.resolve())
        if sys.platform == "darwin":
            subprocess.run(["open", resolved_path])
        elif sys.platform == "win32":
            if hasattr(os, "startfile"):
                os.startfile(resolved_path)
            else:
                subprocess.run(f'explorer "{resolved_path}"', shell=True)
        else:
            subprocess.run(["xdg-open", resolved_path])
        return jsonify({"message": "已打开"})
    except Exception as e:
        return jsonify({"error": f"打开失败: {str(e)}"}), 500

@xhs_bp.route("/open-parent", methods=["POST"])
def open_parent():
    import subprocess
    import sys
    import os
    
    data = request.get_json() or {}
    path_str = data.get("path", "")
    if not path_str:
        return jsonify({"error": "路径不能为空"}), 400
        
    try:
        path = Path(path_str)
        if not path.exists():
            return jsonify({"error": "文件或文件夹不存在"}), 404
            
        resolved_path = str(path.resolve())
        if path.is_file():
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", resolved_path])
            elif sys.platform == "win32":
                subprocess.run(f'explorer /select,"{resolved_path}"', shell=True)
            else:
                subprocess.run(["xdg-open", str(path.parent.resolve())])
        else:
            if sys.platform == "darwin":
                subprocess.run(["open", resolved_path])
            elif sys.platform == "win32":
                if hasattr(os, "startfile"):
                    os.startfile(resolved_path)
                else:
                    subprocess.run(f'explorer "{resolved_path}"', shell=True)
            else:
                subprocess.run(["xdg-open", resolved_path])
        return jsonify({"message": "已打开"})
    except Exception as e:
        return jsonify({"error": f"打开失败: {str(e)}"}), 500


# ── 自动采集路由 ──────────────────────────────────────────
@xhs_bp.route("/auto-collect/status", methods=["GET"])
def auto_collect_status():
    from backend.xhs_scheduler import xhs_collector
    return jsonify(xhs_collector.get_status())


@xhs_bp.route("/auto-collect/toggle", methods=["POST"])
def auto_collect_toggle():
    from backend.xhs_scheduler import xhs_collector
    data = request.get_json() or {}
    user_id = data.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "缺少 user_id"}), 400

    auto_collect = data.get("auto_collect_enabled")
    interval = data.get("collect_interval_minutes")
    target = xhs_collector.update_account_config(user_id, auto_collect=auto_collect, interval_minutes=interval)
    if not target:
        return jsonify({"error": "未找到指定博主"}), 404
    return jsonify({"message": "设置已更新", "account": target})


@xhs_bp.route("/auto-collect/trigger/<user_id>", methods=["POST"])
def auto_collect_trigger(user_id):
    from backend.xhs_scheduler import xhs_collector
    try:
        res = xhs_collector.trigger_collect(user_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@xhs_bp.route("/auto-collect/trigger-all", methods=["POST"])
def auto_collect_trigger_all():
    from backend.xhs_scheduler import xhs_collector
    res = xhs_collector.trigger_all()
    return jsonify(res)


@xhs_bp.route("/auto-collect/stop-all", methods=["POST"])
def auto_collect_stop_all():
    from backend.xhs_scheduler import xhs_collector
    res = xhs_collector.stop_all_collect()
    return jsonify(res)


@xhs_bp.route("/auto-collect/stop/<user_id>", methods=["POST"])
def auto_collect_stop_account(user_id):
    from backend.xhs_scheduler import xhs_collector
    res = xhs_collector.stop_account_collect(user_id)
    return jsonify(res)


@xhs_bp.route("/auto-collect/settings", methods=["POST"])
def auto_collect_save_settings():
    from backend.config import get_settings, save_settings
    data = request.get_json() or {}
    current = get_settings()

    for key in [
        "xhs_auto_collect_enabled",
        "xhs_collect_interval_minutes",
        "xhs_collect_window_start_hour",
        "xhs_collect_window_end_hour",
        "xhs_collect_max_per_account",
        "xhs_collect_cooldown_minutes",
        "xhs_upload_enabled",
        "xhs_upload_url",
        "xhs_device_id",
        "xhs_cos_prefix",
    ]:
        if key in data:
            current[key] = data[key]

    save_settings(current)
    return jsonify({"message": "自动采集设置已保存", "settings": current})


@xhs_bp.route("/auto-collect/logs", methods=["GET"])
def auto_collect_logs():
    from backend.xhs_scheduler import xhs_collector
    limit = request.args.get("limit", 50, type=int)
    logs = xhs_collector.get_collect_logs(limit)
    return jsonify({"logs": logs, "total": len(logs)})

