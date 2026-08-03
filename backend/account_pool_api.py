"""
账号池 API 蓝图
风格对齐 backend/proxy.py（蓝图 + jsonify）
"""

from flask import Blueprint, jsonify

from backend.account_pool import account_pool

account_pool_bp = Blueprint("account_pool", __name__, url_prefix="/api/account-pool")


@account_pool_bp.route("", methods=["GET"])
def list_accounts():
    """列出所有账号（脱敏）"""
    accounts = account_pool.list_accounts()
    return jsonify({"accounts": accounts, "total": len(accounts)})


@account_pool_bp.route("/summary", methods=["GET"])
def get_summary():
    """概要统计"""
    return jsonify(account_pool.get_summary())


@account_pool_bp.route("/<account_id>", methods=["DELETE"])
def remove_account(account_id):
    """从池中删除账号"""
    removed = account_pool.remove(account_id)
    if not removed:
        return jsonify({"error": "未找到该账号"}), 404
    return jsonify({"message": "已删除"})


@account_pool_bp.route("/events", methods=["GET"])
def get_events():
    """取走踢出事件队列"""
    events = account_pool.pop_kick_events()
    return jsonify({"events": events})


@account_pool_bp.route("/import-cookie", methods=["POST"])
def import_cookie():
    """手动导入 Web Cookie 或官方 API Key 到账号池"""
    from flask import request
    import requests as req
    import time
    
    data = request.json or {}
    raw_input = data.get("cookie_str", "").strip()
    nickname = data.get("nickname", "").strip()
    
    if not raw_input:
        return jsonify({"error": "输入不能为空"}), 400

    # 情况 A：官方 API Key (以 wrk- 开头)
    if raw_input.startswith("wrk-"):
        cred = {
            "token": raw_input,
            "cookie_str": "",
            "nickname": nickname or f"OfficialKey_{raw_input[:8]}",
            "vid": "wrk-" + raw_input[:8],
            "save_time": time.time(),
            "status": "active"
        }
        account_pool.add_or_update(cred)
        return jsonify({"message": f"成功导入官方 API Key: {cred['nickname']}"})

    # 情况 B：网页端 Cookie
    # 提取 wr_vid 和 wr_skey
    wr_vid = None
    wr_skey = None
    
    for p in raw_input.split(";"):
        p = p.strip()
        if not p:
            continue
        if p.startswith("wr_vid="):
            try:
                wr_vid = p.split("wr_vid=")[1].split(";")[0].strip()
            except IndexError:
                pass
        elif p.startswith("wr_skey="):
            try:
                wr_skey = p.split("wr_skey=")[1].split(";")[0].strip()
            except IndexError:
                pass

    if not wr_vid or not wr_skey:
        return jsonify({"error": "Cookie 格式错误，必须包含 wr_vid= 和 wr_skey="}), 400

    # 规范化 cookie 字符串，确保格式整洁
    cookie_str = f"wr_vid={wr_vid}; wr_skey={wr_skey}"

    # 验证 Cookie 有效性
    url = "https://weread.qq.com/web/shelf/bookIds"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://weread.qq.com/",
        "Cookie": cookie_str
    }
    
    try:
        resp = req.get(url, headers=headers, timeout=15)
        if resp.status_code != 200 or resp.json().get("errCode") == -2012:
            return jsonify({"error": "验证失败：该 Cookie 已失效或不正确，请从微信读书网页版重新获取"}), 401
    except Exception as e:
        return jsonify({"error": f"网络验证异常: {str(e)}"}), 500

    # 成功添加
    cred = {
        "token": wr_skey,
        "cookie_str": cookie_str,
        "nickname": nickname or f"WebCookie_{wr_vid}",
        "vid": wr_vid,
        "save_time": time.time(),
        "status": "active"
    }
    account_pool.add_or_update(cred)
    return jsonify({"message": f"成功导入 Web Cookie: {cred['nickname']}"})
