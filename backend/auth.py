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
    """获取 PC 微信免证书凭证中转链接（支持指定 biz）"""
    host = request.host
    biz = request.args.get("biz", "").strip()
    if biz:
        import urllib.parse
        relay_url = f"http://{host}/api/auth/mp-relay?biz={urllib.parse.quote(biz)}"
    else:
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
    poc_token = (qs.get("poc_token") or [""])[0]
    wxtoken = (qs.get("wxtoken") or ["777"])[0]
    biz = (qs.get("__biz") or qs.get("biz") or [""])[0]
    cookie_str = request.headers.get("Cookie", "")

    if token:
        token = urllib.parse.unquote(token)
    if key:
        key = urllib.parse.unquote(key)
    if pass_ticket:
        pass_ticket = urllib.parse.unquote(pass_ticket)
    if poc_token:
        poc_token = urllib.parse.unquote(poc_token)
    if biz:
        biz = urllib.parse.unquote(biz)

    poc_sid = ""
    if cookie_str:
        m = re.search(r'(?:^|;\s*)poc_sid=([^;,\s]+)', cookie_str)
        if m:
            poc_sid = urllib.parse.unquote(m.group(1))

    if not token and cookie_str:
        m = re.search(r'(?:^|;\s*)appmsg_token=([^;,\s]+)', cookie_str) or re.search(r'(?:^|;\s*)token=([^;,\s]+)', cookie_str)
        if m:
            token = urllib.parse.unquote(m.group(1))

    if not pass_ticket and cookie_str:
        m = re.search(r'(?:^|;\s*)pass_ticket=([^;,\s]+)', cookie_str)
        if m:
            pass_ticket = urllib.parse.unquote(m.group(1))

    if not key and cookie_str:
        m = re.search(r'(?:^|;\s*)key=([^;,\s]+)', cookie_str)
        if m:
            key = urllib.parse.unquote(m.group(1))

    if not uin and cookie_str:
        m = re.search(r'wxuin=([^;,\s]+)', cookie_str) or re.search(r'(?:^|;\s*)uin=([^;,\s]+)', cookie_str)
        if m:
            uin = urllib.parse.unquote(m.group(1))

    captured = False
    if (token or pass_ticket or key) and (cookie_str or key):
        from backend.account_pool import _normalize_uin
        account_pool.add_or_update({
            "token": token,
            "appmsg_token": token,
            "key": key,
            "pass_ticket": pass_ticket,
            "poc_token": poc_token,
            "poc_sid": poc_sid,
            "wxtoken": wxtoken,
            "uin": _normalize_uin(uin),
            "biz": biz,
            "biz_source": "profile_ext" if key else "article",
            "cookie_str": cookie_str,
            "nickname": "PC微信动态凭证",
            "save_time": time.time(),
            "is_explicit_login": True,
        })
        captured = True

    status_icon = "✅" if captured else "📱"
    title_text = "凭证捕获成功！" if captured else "PC 微信公众号主页授权中转页"
    sub_text = "公众号主页凭证已成功保存入账号池，现在可以返回软件开始拉取文章列表！" if captured else "请点击下方按钮在微信内置浏览器中打开公众号主页建立列表会话："

    target_biz = biz or "Mzg4Mjk3ODcxNA=="
    target_url = f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={target_biz}&scene=124#wechat_redirect"
    btn_label = "打开公众号主页完成授权"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title_text}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #f4f6f8; color: #333; }}
        .card {{ background: white; border-radius: 16px; padding: 36px; box-shadow: 0 10px 30px rgba(0,0,0,0.08); text-align: center; max-width: 420px; width: 90%; }}
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
        <a class="btn" href="{target_url}" target="_self">{btn_label}</a>
    </div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@auth_bp.route("/mp-batch-status", methods=["GET"])
def get_mp_batch_status():
    """获取所有已订阅公众号的主页会话凭证就绪状态"""
    from backend.config import ACCOUNTS_FILE, load_json
    from backend.account_pool import AccountPool
    accounts_sub = load_json(ACCOUNTS_FILE, [])
    pool_acc = account_pool.acquire() or {}
    
    results = []
    for acc in accounts_sub:
        fakeid = acc.get("fakeid", "")
        nickname = acc.get("nickname", "未命名公众号")
        cred = AccountPool.get_biz_credential(pool_acc, fakeid) if pool_acc else {}
        has_key = bool(cred.get("key"))
        ready = bool(cred.get("getmsg_ready") and has_key)
        results.append({
            "fakeid": fakeid,
            "nickname": nickname,
            "round_head_img": acc.get("round_head_img", ""),
            "has_key": has_key,
            "ready": ready,
            "updated_at": cred.get("updated_at", 0),
        })
    return jsonify({"accounts": results})


@auth_bp.route("/mp-batch-next", methods=["GET"])
def handle_mp_batch_next():
    """供微信 WebView 内部在捕获完当前公众号凭证后，平滑获取下一个公众号的流转 URL。
    使用 scene=124 且绝不携带 #wechat_redirect，保证微信保持在 WebView 内部流转而不触发 Native 窗口关闭！
    """
    from backend.config import ACCOUNTS_FILE, load_json
    import urllib.parse
    accounts_sub = load_json(ACCOUNTS_FILE, [])
    if not accounts_sub:
        return jsonify({"completed": True, "total": 0})

    current_biz = request.args.get("current_biz", "")
    if current_biz:
        current_biz = urllib.parse.unquote(current_biz)

    # 找到 current_biz 在关注列表中的索引
    cur_idx = -1
    for i, a in enumerate(accounts_sub):
        fid = a.get("fakeid", "")
        if fid == current_biz or urllib.parse.unquote(fid) == current_biz:
            cur_idx = i
            break

    next_idx = cur_idx + 1
    if 0 <= next_idx < len(accounts_sub):
        next_acc = accounts_sub[next_idx]
        fakeid = next_acc.get("fakeid", "")
        # 使用 action=getmsg 而非 action=home，彻底杜绝微信 Native 原生窗口拦截与关闭，留在 Webview 内部连续流转！
        next_url = f"https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz={urllib.parse.quote(fakeid)}&f=json&offset=0&count=1&scene=126&batch_mode=1&_t={int(time.time()*1000)}"
        return jsonify({
            "completed": False,
            "next_url": next_url,
            "next_name": next_acc.get("nickname", "公众号"),
            "step": next_idx,
            "total": len(accounts_sub),
        })
    else:
        return jsonify({
            "completed": True,
            "total": len(accounts_sub),
        })


@auth_bp.route("/mp-batch-portal", methods=["GET"])
def handle_mp_batch_portal():
    """公众号主页批量授权中转聚合中心 (参考 wechatDownload 官方主页授权体系)
    1. 展示全部关注/订阅的公众号清单与实时凭证就绪状态（单一事实来源，绝不误报假就绪）；
    2. 每个卡片直连纯净原生官方主页 URL (profile_ext?action=home&__biz=...#wechat_redirect)；
    3. 用户在微信内置浏览器中点击对应公众号卡片，微信在发起主页请求时由 MITM 毫秒级捕获 key/pass_ticket；
    4. 返回聚合页后自动实时更新就绪标记与进度。
    """
    from backend.config import ACCOUNTS_FILE, load_json
    from backend.account_pool import AccountPool
    import json, urllib.parse
    accounts_sub = load_json(ACCOUNTS_FILE, [])
    pool_acc = account_pool.acquire() or {}
    total = len(accounts_sub)

    accounts_data = []
    ready_count = 0
    now = time.time()
    for i, acc in enumerate(accounts_sub):
        fakeid = acc.get("fakeid", "")
        nickname = acc.get("nickname", "未命名公众号")
        cred = AccountPool.get_biz_credential(pool_acc, fakeid) if pool_acc else {}
        has_key = bool(cred.get("key"))
        updated_at = cred.get("updated_at", 0)
        # 凭证新鲜度判断（5分钟内为新鲜，超过仍有效但提示）
        is_fresh = has_key and (now - updated_at < 3600)
        is_ready = has_key
        if is_ready:
            ready_count += 1

        profile_url = f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={urllib.parse.quote(fakeid)}#wechat_redirect"
        accounts_data.append({
            "index": i,
            "fakeid": fakeid,
            "nickname": nickname,
            "round_head_img": acc.get("round_head_img", ""),
            "has_key": has_key,
            "ready": is_ready,
            "fresh": is_fresh,
            "updated_at": updated_at,
            "profile_url": profile_url,
        })

    accounts_json = json.dumps(accounts_data, ensure_ascii=False)
    progress_pct = int((ready_count / max(1, total)) * 100) if total > 0 else 0

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>公众号批量授权聚合中心</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="referrer" content="no-referrer">
    <meta name="referrer" content="never">
    <style>
        :root {{
            --primary: #07c160;
            --primary-hover: #06ad56;
            --bg: #f7f8fa;
            --card: #ffffff;
            --text: #191919;
            --text-sub: #7f7f7f;
            --border: #ebebeb;
            --warning: #ff9800;
            --warning-bg: rgba(255, 152, 0, 0.08);
            --info-bg: rgba(7, 193, 96, 0.08);
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            margin: 0;
            padding: 20px 16px;
            color: var(--text);
            display: flex;
            justify-content: center;
        }}
        .container {{
            max-width: 480px;
            width: 100%;
        }}
        .card {{
            background: var(--card);
            border-radius: 16px;
            padding: 22px 18px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.05);
            margin-bottom: 14px;
        }}
        .header {{
            text-align: center;
            margin-bottom: 14px;
        }}
        .header h1 {{
            font-size: 1.22rem;
            margin: 0 0 6px 0;
            font-weight: 700;
        }}
        .header p {{
            font-size: 0.83rem;
            color: var(--text-sub);
            margin: 0;
            line-height: 1.5;
        }}
        .risk-banner {{
            background: var(--warning-bg);
            border: 1px solid rgba(255, 152, 0, 0.25);
            border-radius: 12px;
            padding: 12px 14px;
            margin-bottom: 16px;
            font-size: 0.78rem;
            line-height: 1.55;
            color: #b26a00;
        }}
        .risk-banner strong {{
            color: #d84315;
        }}
        .progress-bar-bg {{
            background: #eee;
            border-radius: 10px;
            height: 10px;
            overflow: hidden;
            margin: 14px 0 8px 0;
        }}
        .progress-bar-fill {{
            background: var(--primary);
            height: 100%;
            width: {progress_pct}%;
            transition: width 0.3s ease;
        }}
        .progress-stats {{
            display: flex;
            justify-content: space-between;
            font-size: 0.8rem;
            color: var(--text-sub);
            margin-bottom: 14px;
        }}
        .btn-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }}
        .btn {{
            box-sizing: border-box;
            background: var(--primary);
            color: white;
            border: none;
            padding: 10px 8px;
            border-radius: 10px;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
            transition: all 0.2s;
        }}
        .btn-outline {{
            background: #f0fdf4;
            color: var(--primary);
            border: 1px solid rgba(7, 193, 96, 0.4);
        }}
        .btn-warning {{
            background: #fff8f0;
            color: #e65100;
            border: 1px solid rgba(255, 152, 0, 0.4);
        }}
        .filter-row {{
            display: flex;
            gap: 8px;
            margin-top: 14px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 10px;
        }}
        .filter-btn {{
            background: none;
            border: none;
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--text-sub);
            padding: 4px 10px;
            border-radius: 14px;
            cursor: pointer;
        }}
        .filter-btn.active {{
            background: var(--info-bg);
            color: var(--primary);
        }}
        .account-list {{
            display: flex;
            flex-direction: column;
            gap: 9px;
            margin-top: 12px;
        }}
        .account-item {{
            display: flex;
            align-items: center;
            padding: 11px 13px;
            background: #fafafa;
            border-radius: 12px;
            border: 1px solid var(--border);
            gap: 12px;
            transition: all 0.2s;
            text-decoration: none;
            color: inherit;
        }}
        .account-item:hover {{
            background: #f0fdf4;
            border-color: var(--primary);
        }}
        .account-item.ready {{
            border-color: rgba(7, 193, 96, 0.3);
            background: #fcfdfd;
        }}
        .account-item.pending {{
            border-color: rgba(255, 152, 0, 0.3);
            background: #fffefb;
        }}
        .avatar {{
            width: 36px;
            height: 36px;
            border-radius: 50%;
            object-fit: cover;
            background: #ddd;
        }}
        .account-info {{
            flex: 1;
            min-width: 0;
        }}
        .account-name {{
            font-size: 0.88rem;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .account-biz {{
            font-size: 0.7rem;
            color: var(--text-sub);
            font-family: monospace;
        }}
        .status-tag {{
            font-size: 0.72rem;
            padding: 3px 9px;
            border-radius: 20px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 3px;
        }}
        .tag-ready {{ background: rgba(7, 193, 96, 0.15); color: var(--primary); }}
        .tag-pending {{ background: rgba(255, 152, 0, 0.15); color: #e65100; }}
        .toast {{
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(0,0,0,0.85);
            color: white;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 0.82rem;
            display: none;
            z-index: 1000;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="header">
                <div style="font-size: 38px; margin-bottom: 4px;">{'🎉' if ready_count == total and total > 0 else '🛡️'}</div>
                <h1>{'全部公众号授权就绪！' if ready_count == total and total > 0 else '公众号主页批量授权中心'}</h1>
                <p>{'所有公众号主页凭证均已就绪，已可开始导出文章！' if ready_count == total and total > 0 else '在微信内置浏览器中逐个打开主页建立安全会话，支持低风控流转'}</p>
            </div>

            <!-- 风控与安全流转说明 -->
            <div class="risk-banner">
                🛡️ <strong>低风控安全建议：</strong><br>
                1. <strong>增量优先</strong>：已标记「✅ 会话已就绪」的号无需重复打开；<br>
                2. <strong>防频控间隔</strong>：每打开一个公众号主页建议停留 <strong>2~4 秒</strong>，切忌瞬间连击；<br>
                3. <strong>单批次配额</strong>：建议单次连续激活不超过 <strong>15 个</strong>公众号，多于 15 个建议间隔 2 分钟后再激活下一批。
            </div>

            <div class="progress-bar-bg">
                <div id="progress-fill" class="progress-bar-fill"></div>
            </div>
            <div class="progress-stats">
                <span id="progress-stat-text">已就绪: {ready_count} / {total} 个公众号</span>
                <span id="progress-stat-pct">{progress_pct}%</span>
            </div>

            <button class="btn" style="width: 100%; margin-bottom: 10px; background: var(--primary); font-size: 0.92rem; padding: 12px;" onclick="startAutoBatch()">🚀 一键开始全自动流转授权</button>

            <div class="btn-grid">
                <button class="btn btn-warning" onclick="copyPendingLinks()">🎯 仅复制待授权链接 ({total - ready_count}个)</button>
                <button class="btn btn-outline" onclick="copyAllLinks()">📋 复制全部主页链接</button>
            </div>
            <button class="btn btn-outline" style="width: 100%; margin-top: 8px;" onclick="refreshStatus()">🔄 刷新当前就绪状态</button>
        </div>

        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="font-weight: 600; font-size: 0.92rem;">公众号清单 ({total} 个)</div>
                <div style="font-size: 0.75rem; color: var(--text-sub);">点击卡片在微信中打开主页</div>
            </div>

            <div class="filter-row">
                <button class="filter-btn active" id="f-all" onclick="filterList('all')">全部 ({total})</button>
                <button class="filter-btn" id="f-pending" onclick="filterList('pending')">待授权 ({total - ready_count})</button>
                <button class="filter-btn" id="f-ready" onclick="filterList('ready')">已就绪 ({ready_count})</button>
            </div>

            <div class="account-list" id="account-list-container">
                {''.join([
                    f'''<a class="account-item {'ready' if acc['ready'] else 'pending'}" href="{acc['profile_url']}" target="_self" data-fakeid="{acc['fakeid']}" data-ready="{'1' if acc['ready'] else '0'}">
                        <img class="avatar" src="{acc['round_head_img'] or 'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📰</text></svg>'}" />
                        <div class="account-info">
                            <div class="account-name">{acc['nickname']}</div>
                            <div class="account-biz">{acc['fakeid']}</div>
                        </div>
                        <div class="status-tag {'tag-ready' if acc['ready'] else 'tag-pending'}">
                            {'✅ 会话已就绪' if acc['ready'] else '📱 点击授权'}
                        </div>
                    </a>'''
                    for acc in accounts_data
                ])}
            </div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        const accounts = {accounts_json};
        let curFilter = 'all';

        function showToast(msg) {{
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.display = 'block';
            setTimeout(() => {{ t.style.display = 'none'; }}, 2200);
        }}

        function startAutoBatch() {{
            if (!accounts || accounts.length === 0) {{
                showToast('暂无公众号可授权！');
                return;
            }}
            const pendingAcc = accounts.find(a => !a.ready) || accounts[0];
            try {{ sessionStorage.setItem('mp_batch_mode', 'true'); }} catch(e) {{}}
            showToast('正在启动自动连续流转授权...');
            setTimeout(() => {{
                // 使用 action=getmsg 保证留在 Webview 窗口内连续流转，绝不触发微信 Native 原生窗口拦截与关闭
                const startUrl = 'https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz=' + encodeURIComponent(pendingAcc.fakeid) + '&f=json&offset=0&count=1&scene=126&batch_mode=1&_t=' + Date.now();
                window.location.href = startUrl;
            }}, 400);
        }}

        function filterList(type) {{
            curFilter = type;
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            const fb = document.getElementById('f-' + type);
            if (fb) fb.classList.add('active');

            document.querySelectorAll('.account-item').forEach(item => {{
                const isReady = item.getAttribute('data-ready') === '1';
                if (type === 'all') {{
                    item.style.display = 'flex';
                }} else if (type === 'pending') {{
                    item.style.display = isReady ? 'none' : 'flex';
                }} else if (type === 'ready') {{
                    item.style.display = isReady ? 'flex' : 'none';
                }}
            }});
        }}

        function copyPendingLinks() {{
            const pendingAccs = accounts.filter(a => !a.ready);
            if (pendingAccs.length === 0) {{
                showToast('全部公众号会话均已就绪，无需补采！');
                return;
            }}
            const links = pendingAccs.map(a => a.nickname + ':\\n' + a.profile_url).join('\\n\\n');
            doCopy(links, '已复制 ' + pendingAccs.length + ' 个待授权公众号链接！');
        }}

        function copyAllLinks() {{
            const links = accounts.map(a => a.nickname + ':\\n' + a.profile_url).join('\\n\\n');
            doCopy(links, '已复制全部 ' + accounts.length + ' 个公众号主页链接！');
        }}

        function doCopy(text, successMsg) {{
            if (navigator.clipboard) {{
                navigator.clipboard.writeText(text).then(() => {{
                    showToast(successMsg);
                }}).catch(() => fallbackCopy(text, successMsg));
            }} else {{
                fallbackCopy(text, successMsg);
            }}
        }}

        function fallbackCopy(text, successMsg) {{
            const ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            showToast(successMsg);
        }}

        async function refreshStatus() {{
            try {{
                const resp = await fetch('/api/auth/mp-batch-status');
                const data = await resp.json();
                if (data && data.accounts) {{
                    let readyCnt = 0;
                    const total = data.accounts.length;
                    data.accounts.forEach(acc => {{
                        const el = document.querySelector('[data-fakeid="' + acc.fakeid + '"]');
                        const isReady = !!(acc.ready || acc.has_key);
                        if (isReady) readyCnt++;
                        if (el) {{
                            el.setAttribute('data-ready', isReady ? '1' : '0');
                            const tag = el.querySelector('.status-tag');
                            if (isReady) {{
                                el.className = 'account-item ready';
                                tag.className = 'status-tag tag-ready';
                                tag.innerText = '✅ 会话已就绪';
                            }} else {{
                                el.className = 'account-item pending';
                                tag.className = 'status-tag tag-pending';
                                tag.innerText = '📱 点击授权';
                            }}
                        }}
                    }});
                    const pendingCnt = total - readyCnt;
                    const pct = total === 0 ? 0 : Math.round((readyCnt / total) * 100);
                    document.getElementById('progress-fill').style.width = pct + '%';
                    document.getElementById('progress-stat-pct').innerText = pct + '%';
                    document.getElementById('progress-stat-text').innerText = '已就绪: ' + readyCnt + ' / ' + total + ' 个公众号';
                    
                    document.getElementById('f-all').innerText = '全部 (' + total + ')';
                    document.getElementById('f-pending').innerText = '待授权 (' + pendingCnt + ')';
                    document.getElementById('f-ready').innerText = '已就绪 (' + readyCnt + ')';

                    filterList(curFilter);
                    showToast('状态已同步: ' + readyCnt + '/' + total + ' 已就绪');
                }}
            }} catch(e) {{
                showToast('刷新状态失败: ' + e.message);
            }}
        }}

        // 用户在微信中打开主页返回聚合页后自动平滑感知最新就绪态
        document.addEventListener('visibilitychange', () => {{
            if (!document.hidden) refreshStatus();
        }});
    </script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}
