"""
登录认证模块
管理微信读书平台扫码登录、凭证验证和状态查询
"""

import json
import time
import threading
import requests as req
from pathlib import Path
from flask import Blueprint, jsonify, request

from backend.config import (
    CONFIG_FILE, DATA_DIR, load_json, save_json,
    get_settings, get_proxies_dict, report_proxy_status
)
from backend.account_pool import account_pool, LOGIN_VALID_SECONDS as POOL_LOGIN_VALID_SECONDS

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

LOGIN_VALID_SECONDS = 30 * 24 * 60 * 60  # 凭证延长保存

# 登录状态管理
_login_state = {
    "status": "idle",       # idle / scanning / success / failed
    "message": "",
    "progress": 0,
    "qrcode": "",           # scanUrl
    "uuid": "",
}
_login_lock = threading.Lock()


def _set_login_state(status: str, message: str = "", progress: int = 0, qrcode: str = "", uuid: str = ""):
    with _login_lock:
        _login_state["status"] = status
        _login_state["message"] = message
        _login_state["progress"] = progress
        if qrcode:
            _login_state["qrcode"] = qrcode
        if uuid:
            _login_state["uuid"] = uuid


@auth_bp.route("/status", methods=["GET"])
def get_status():
    """获取登录状态（聚合账号池中第一个 active 账号）"""
    accounts = account_pool.list_accounts()
    active_acc = None
    for acc in accounts:
        if acc["status"] == "active":
            active_acc = acc
            break

    if not active_acc:
        if accounts:
            last_acc = accounts[0]
            return jsonify({
                "logged_in": False,
                "login_state": _login_state,
                "token_preview": last_acc.get("token_preview", ""),
                "save_time": last_acc.get("save_time", 0),
                "message": "暂无可用微信读书账号，请重新登录",
                "account_info": {
                    "nickname": last_acc.get("nickname", ""),
                    "avatar": last_acc.get("avatar", ""),
                },
                "pool_summary": account_pool.get_summary(),
            })
        return jsonify({
            "logged_in": False,
            "login_state": _login_state,
            "message": "未登录，请先在账号池页面添加微信读书账号"
        })

    save_time = active_acc.get("save_time", 0)

    return jsonify({
        "logged_in": True,
        "login_state": _login_state,
        "token_preview": active_acc.get("token_preview", ""),
        "save_time": save_time,
        "message": "登录有效",
        "account_info": {
            "nickname": active_acc.get("nickname", ""),
            "avatar": active_acc.get("avatar", ""),
        },
        "pool_summary": account_pool.get_summary(),
    })


@auth_bp.route("/login", methods=["POST"])
def start_login():
    """启动微信读书扫码登录（无页面弹窗，仅生成二维码数据）"""
    with _login_lock:
        if _login_state["status"] == "scanning":
            return jsonify({"message": "正在登录中，请扫码", "login_state": _login_state})

    thread = threading.Thread(target=_do_login, daemon=True)
    thread.start()

    return jsonify({"message": "已请求微信读书扫码二维码"})


@auth_bp.route("/login-browser", methods=["POST"])
def start_browser_login():
    """兼容旧前端方法"""
    return start_login()


def _do_login():
    """配置提醒说明"""
    _set_login_state("failed", "已弃用第三方 WeRead 服务。请使用 mp.weixin.qq.com 后台 Token/Cookie 或 PC 微信代理进行凭证配置。")


@auth_bp.route("/cancel", methods=["POST"])
def cancel_login():
    """取消扫码登录"""
    _set_login_state("idle", "登录已取消")
    return jsonify({"message": "登录已取消"})


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """清除登录凭据"""
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
        except Exception:
            pass
    _set_login_state("idle", "已退出登录")
    return jsonify({"message": "已退出登录"})


@auth_bp.route("/check-credentials", methods=["GET"])
def check_credentials():
    """验证账号池凭证有效性"""
    from backend.account_pool import borrow_session

    try:
        acc_id, token, cookie_str = borrow_session()
    except RuntimeError:
        return jsonify({"valid": False, "message": "账号池中无可用账号，请在 PC 微信中打开任意文章捕获凭证"})

    return jsonify({"valid": True, "message": f"账号 (ID: {acc_id}) 凭证正常可用"})


@auth_bp.route("/mp-relay-url", methods=["GET"])
def get_mp_relay_url():
    """获取 PC 微信免证书凭证中转链接"""
    host = request.host
    relay_url = f"http://{host}/api/auth/mp-relay"
    return jsonify({
        "relay_url": relay_url,
        "message": "请在 PC 微信客户端发送并打开此链接，系统将自动静默捕获凭证！"
    })


@auth_bp.route("/mp-relay", methods=["GET"])
def handle_mp_relay():
    """PC 微信免证书凭证中转与静默捕获页面 (参考 qiye45/wechatDownload 原理)"""
    import urllib.parse
    import re
    parsed = urllib.parse.urlparse(request.url)
    qs = urllib.parse.parse_qs(parsed.query)

    token = (qs.get("appmsg_token") or [""])[0]
    key = (qs.get("key") or [""])[0]
    pass_ticket = (qs.get("pass_ticket") or [""])[0]
    uin = (qs.get("uin") or [""])[0]
    cookie_str = request.headers.get("Cookie", "")

    if not token and cookie_str:
        m = re.search(r'appmsg_token=([^;,\s]+)', cookie_str)
        if m:
            token = urllib.parse.unquote(m.group(1))

    if not pass_ticket and cookie_str:
        m = re.search(r'pass_ticket=([^;,\s]+)', cookie_str)
        if m:
            pass_ticket = urllib.parse.unquote(m.group(1))

    if not uin and cookie_str:
        m = re.search(r'wxuin=([^;,\s]+)', cookie_str) or re.search(r'uin=([^;,\s]+)', cookie_str)
        if m:
            uin = urllib.parse.unquote(m.group(1))

    captured = False
    if (token or pass_ticket) and cookie_str:
        account_pool.add_or_update({
            "token": token,
            "appmsg_token": token,
            "key": key,
            "pass_ticket": pass_ticket,
            "uin": uin,
            "cookie_str": cookie_str,
            "nickname": "PC微信动态凭证",
            "save_time": time.time(),
        })
        captured = True

    status_icon = "✅" if captured else "📱"
    title_text = "凭证捕获成功！" if captured else "PC 微信凭证授权中转页"
    sub_text = "公众号凭证已成功保存入账号池，现在可以返回软件开始导出文章！" if captured else "请点击下方按钮在微信内置浏览器中打开文章以完成授权捕获："

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title_text}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #f4f6f8; color: #333; }}
        .card {{ background: white; border-radius: 16px; padding: 36px; box-shadow: 0 10px 30px rgba(0,0,0,0.08); text-align: center; max-width: 400px; width: 90%; }}
        .icon {{ font-size: 54px; margin-bottom: 12px; }}
        h2 {{ margin: 0 0 12px 0; color: #111; font-size: 1.4rem; }}
        p {{ color: #666; font-size: 0.95rem; line-height: 1.6; margin-bottom: 24px; }}
        .btn {{ display: inline-block; background: #07c160; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600; transition: background 0.2s; }}
        .btn:hover {{ background: #06ad56; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">{status_icon}</div>
        <h2>{title_text}</h2>
        <p>{sub_text}</p>
        <a class="btn" href="https://mp.weixin.qq.com/s?__biz=Mjk0MDY5NjMyMA==&mid=2650298868&idx=1&sn=67626575992e06cecc1129e9ee16d414" target="_self">打开公众号文章完成授权</a>
    </div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}
