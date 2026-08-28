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


@account_pool_bp.route("/auto-refresh-config", methods=["GET"])
def get_auto_refresh_cfg():
    """获取定时同步配置"""
    try:
        from scripts.auto_refresh_pc_wechat import get_auto_refresh_config
        return jsonify(get_auto_refresh_config())
    except Exception as e:
        return jsonify({"enabled": True, "interval_minutes": 5, "error": str(e)})


@account_pool_bp.route("/auto-refresh-toggle", methods=["POST"])
def toggle_auto_refresh():
    """切换定时同步开关"""
    try:
        from flask import request
        from scripts.auto_refresh_pc_wechat import set_auto_refresh_enabled
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled", True)
        res = set_auto_refresh_enabled(enabled)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@account_pool_bp.route("/sync-manual", methods=["POST"])
def sync_manual():
    """手动执行批量同步"""
    try:
        from scripts.auto_refresh_pc_wechat import trigger_pc_wechat_refresh
        success = trigger_pc_wechat_refresh(force=True)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

