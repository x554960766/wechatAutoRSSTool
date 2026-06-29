"""
版本检查与更新模块
通过 GitHub Releases API 检查新版本，支持下载更新包到本地
"""

import os
import re
import sys
import threading
import requests
from pathlib import Path
from flask import Blueprint, jsonify, request

from backend.config import APP_VERSION, app_dir, get_proxies_dict

updater_bp = Blueprint('updater', __name__)

# 公司内部私有仓库——Releases 非公开，访问与下载均需鉴权
GITHUB_REPO = "x554960766/wechatAutoRSSTool"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# 访问私有仓库 Releases 所需的 token。不写死明文：
#   1) 优先读环境变量 WECHAT_UPDATER_TOKEN（本地/CI 调试用）
#   2) 打包分发时把 _EMBEDDED_TOKEN 填上（会随客户端分发）
# ⚠️ 内部专用：请使用「细粒度 fine-grained、只读 Contents、仅限本仓库、设置较短有效期」的 token，
#    一旦泄露可随时在 GitHub 吊销并重新打包。切勿使用账号级 classic token。
_EMBEDDED_TOKEN = ""


def _get_token() -> str:
    """返回更新所用 token（环境变量优先，其次内置常量），均无则空串。"""
    return (os.environ.get("WECHAT_UPDATER_TOKEN", "").strip() or _EMBEDDED_TOKEN.strip())


def _api_headers(accept: str = "application/vnd.github.v3+json") -> dict:
    """构造带鉴权的 GitHub API 请求头；无 token 时退化为匿名请求。"""
    headers = {
        "Accept": accept,
        "User-Agent": f"wechat-mp-tools/{APP_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

# 下载状态（进程内单例）
_download_state = {
    "status": "idle",      # idle / downloading / done / error
    "progress": 0,         # 0-100
    "total_size": 0,
    "downloaded": 0,
    "save_path": "",
    "error": "",
}
_download_lock = threading.Lock()


def _compare_versions(current: str, latest: str) -> bool:
    """返回 True 如果 latest 比 current 更新"""
    try:
        cur_parts = [int(x) for x in current.strip().lstrip("v").split('.')]
        lat_parts = [int(x) for x in latest.strip().lstrip("v").split('.')]
        # 补齐长度以实现鲁棒的语义化版本号对比
        max_len = max(len(cur_parts), len(lat_parts))
        cur_parts += [0] * (max_len - len(cur_parts))
        lat_parts += [0] * (max_len - len(lat_parts))
        return lat_parts > cur_parts
    except (ValueError, AttributeError):
        return False


def _get_platform_keyword() -> str:
    """根据当前平台返回对应的 asset 文件名关键词"""
    if sys.platform == "darwin":
        return "macOS"
    elif sys.platform == "win32":
        return "Windows"
    else:
        return "Linux"


def _get_variant_keyword() -> str:
    """根据是否内置浏览器返回 Full / Lite"""
    from backend.runtime import bundled_browsers_available
    return "Full" if bundled_browsers_available() else "Lite"


def _updates_dir() -> Path:
    """更新包下载目录"""
    d = app_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


@updater_bp.route('/api/version/check', methods=['GET'])
def check_update():
    """检查是否有新版本"""
    try:
        resp = requests.get(GITHUB_API_URL, timeout=10, headers=_api_headers(), proxies=get_proxies_dict())
        resp.raise_for_status()
        data = resp.json()

        tag = data.get("tag_name", "").lstrip("v").strip()
        has_update = _compare_versions(APP_VERSION, tag)

        # 匹配当前平台的下载链接。
        # 私有仓库 asset 不能用 browser_download_url（需浏览器会话），改用 asset 的 API url，
        # 配合 Accept: application/octet-stream + 鉴权头由后端代下载（见 start_download）。
        platform_kw = _get_platform_keyword()
        variant_kw = _get_variant_keyword()
        download_url = ""
        asset_size = 0

        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if platform_kw in name and variant_kw in name:
                download_url = asset.get("url", "")
                asset_size = asset.get("size", 0)
                break

        # 如果没找到精确匹配，尝试只按平台匹配
        if not download_url:
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if platform_kw in name:
                    download_url = asset.get("url", "")
                    asset_size = asset.get("size", 0)
                    break

        return jsonify({
            "has_update": has_update,
            "current_version": APP_VERSION,
            "latest_version": tag,
            "release_url": data.get("html_url", ""),
            "download_url": download_url,
            "asset_size": asset_size,
            "release_notes": data.get("body", ""),
        })

    except requests.RequestException as e:
        error_msg = f"检查更新失败: {str(e)}"
        if e.response is not None:
            if e.response.status_code in (403, 429):
                error_msg = "检查更新过于频繁或被 GitHub 限制 (403/429)，请稍后再试或配置代理网络。"
        
        return jsonify({
            "has_update": False,
            "current_version": APP_VERSION,
            "latest_version": APP_VERSION,
            "error": error_msg,
        })


@updater_bp.route('/api/version/download', methods=['POST'])
def start_download():
    """开始下载更新包"""
    global _download_state

    with _download_lock:
        if _download_state["status"] == "downloading":
            return jsonify({"error": "已有下载任务进行中"}), 400

    body = request.get_json() or {}
    url = body.get("url", "")
    if not url:
        return jsonify({"error": "缺少下载地址"}), 400

    # 私有 asset 的 API url 末段是数字 id，无法作为文件名；先用占位名，
    # 真正的文件名在下载时从响应头 Content-Disposition 解析后回填。
    fallback_name = url.split("/")[-1] or "update.zip"
    if not fallback_name.lower().endswith((".zip", ".dmg", ".exe")):
        fallback_name = "update.zip"
    save_path = str(_updates_dir() / fallback_name)

    with _download_lock:
        _download_state = {
            "status": "downloading",
            "progress": 0,
            "total_size": 0,
            "downloaded": 0,
            "save_path": save_path,
            "error": "",
        }

    def _do_download():
        global _download_state
        nonlocal save_path
        try:
            # 鉴权 + octet-stream：GitHub API 会 302 跳到签名下载地址，
            # requests 跨主机重定向时会自动剥离 Authorization 头，token 不会泄给 S3。
            resp = requests.get(
                url, stream=True, timeout=300,
                headers=_api_headers("application/octet-stream"),
                proxies=get_proxies_dict(),
            )
            resp.raise_for_status()

            # 从响应头解析真实文件名，回填 save_path
            cd = resp.headers.get("Content-Disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
            if m:
                save_path = str(_updates_dir() / os.path.basename(m.group(1).strip()))
                with _download_lock:
                    _download_state["save_path"] = save_path

            total = int(resp.headers.get("content-length", 0))

            with _download_lock:
                _download_state["total_size"] = total

            downloaded = 0
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        pct = int(downloaded * 100 / total) if total else 0
                        with _download_lock:
                            _download_state["downloaded"] = downloaded
                            _download_state["progress"] = pct

            with _download_lock:
                _download_state["status"] = "done"
                _download_state["progress"] = 100

        except Exception as e:
            with _download_lock:
                _download_state["status"] = "error"
                _download_state["error"] = str(e)

    threading.Thread(target=_do_download, daemon=True).start()
    return jsonify({"success": True, "save_path": save_path})


@updater_bp.route('/api/version/download-progress', methods=['GET'])
def download_progress():
    """获取下载进度"""
    with _download_lock:
        return jsonify(dict(_download_state))


@updater_bp.route('/api/version/open-update-folder', methods=['POST'])
def open_update_folder():
    """打开更新包所在目录"""
    folder = str(_updates_dir())

    # 如果有已下载的文件，打开文件所在目录
    with _download_lock:
        save_path = _download_state.get("save_path", "")

    if save_path and os.path.isfile(save_path):
        folder = os.path.dirname(save_path)

    if sys.platform == "darwin":
        os.system(f'open "{folder}"')
    elif sys.platform == "win32":
        os.startfile(folder)
    else:
        os.system(f'xdg-open "{folder}"')

    return jsonify({"success": True, "path": folder})
