"""
文章获取与管理模块
获取文章列表、搜索文章、管理下载任务
"""

import json
import time
import threading
import requests as req
import shutil
from flask import Blueprint, jsonify, request, Response
from datetime import datetime
from pathlib import Path

from backend.config import (
    CONFIG_FILE, BASE_URL, DEFAULT_HEADERS, OUTPUT_DIR,
    DOWNLOAD_HISTORY_FILE,
    load_json, save_json, get_settings, get_proxies_dict, report_proxy_status
)
from backend.account_pool import borrow_session, account_pool

articles_bp = Blueprint("articles", __name__, url_prefix="/api/articles")

# 下载进度管理
_download_tasks = {}
_download_lock = threading.Lock()


def _get_session():
    """获取凭证（通过账号池）"""
    account_id, token, cookie_str = borrow_session()
    return token, cookie_str


def fetch_article_detail_content(url: str) -> str:
    """抓取微信公众号文章网页并提取 HTML 正文（.rich_media_content），还原图片 data-src"""
    import bs4
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = req.get(url, headers=headers, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    soup = bs4.BeautifulSoup(resp.text, "html.parser")
    content_node = soup.select_one(".rich_media_content") or soup.select_one("#js_content")
    if not content_node:
        return resp.text

    for img in content_node.find_all("img"):
        if img.get("data-src"):
            img["src"] = img["data-src"]
            del img["data-src"]

def _fetch_articles_via_appmsg_fallback(fakeid: str, begin: int, count: int, keyword: str, token: str, cookie_str: str):
    """当 PC 微信 key 失效或缺失时，使用 Playwright/Web 获取到的 Web Token 自动走 /cgi-bin/appmsg 备用通道获取文章列表"""
    if not token:
        return None
    headers = {**DEFAULT_HEADERS, "Cookie": cookie_str}
    proxies = get_proxies_dict()
    try:
        resp = req.get(
            f"{BASE_URL}/cgi-bin/appmsg",
            params={
                "action": "list_ex",
                "token": token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
                "type": "9",
                "query": keyword,
                "fakeid": fakeid,
                "begin": str(begin),
                "count": str(count),
            },
            headers=headers,
            proxies=proxies,
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("base_resp", {}).get("ret") == 0:
                articles = []
                for item in data.get("app_msg_list", []):
                    articles.append({
                        "title": item.get("title", ""),
                        "link": item.get("link", ""),
                        "cover": item.get("cover", ""),
                        "digest": item.get("digest", ""),
                        "author": item.get("author_name", ""),
                        "update_time": item.get("update_time", item.get("create_time", 0)),
                        "is_original": False,
                        "item_show_type": item.get("item_show_type", 0),
                        "id": str(item.get("aid", "")),
                    })
                total_cnt = data.get("app_msg_cnt", len(articles))
                can_continue = 1 if (begin + len(articles)) < total_cnt else 0
                return articles, total_cnt, can_continue
    except Exception as e:
        print(f"appmsg fallback 尝试失败: {e}")
    return None


def _fetch_articles_page(fakeid: str, begin: int, count: int, keyword: str = "") -> tuple:
    """使用微信客户端历史消息原生接口 (profile_ext?action=getmsg) 获取文章列表 (articles, total_count)
    支持从账号池提取 appmsg_token, key, pass_ticket, uin 与 Cookie 进行翻页抓取"""
    max_switch = 3
    last_exc = None

    for _ in range(max_switch):
        try:
            account_id, token, cookie_str = borrow_session()
        except RuntimeError as e:
            raise RuntimeError(str(e))

        # 尝试从账号中提取三方库需要的客户端参数
        account_data = {}
        with account_pool._lock:
            for acc in account_pool._load():
                if acc.get("id") == account_id:
                    account_data = acc
                    break

        appmsg_token = account_data.get("appmsg_token", token)
        pass_ticket = account_data.get("pass_ticket", "")
        key = account_data.get("key", "")
        uin = account_data.get("uin", "")

        import urllib.parse, re
        appmsg_token = urllib.parse.unquote(appmsg_token) if appmsg_token else ""
        pass_ticket = urllib.parse.unquote(pass_ticket) if pass_ticket else ""
        key = urllib.parse.unquote(key) if key else ""
        uin = urllib.parse.unquote(uin) if uin else ""

        cookie_str = urllib.parse.unquote(cookie_str.replace(", ", "; "))

        if not uin:
            m = re.search(r'wxuin=([^;,\s]+)', cookie_str) or re.search(r'(?:^|;\s*)uin=([^;,\s]+)', cookie_str)
            if m: uin = m.group(1)

        if not key:
            m = re.search(r'(?:^|;\s*)key=([^;,\s]+)', cookie_str)
            if m: key = m.group(1)

        if not pass_ticket:
            m = re.search(r'pass_ticket=([^;,\s]+)', cookie_str)
            if m: pass_ticket = m.group(1)

        ua = account_data.get("user_agent") or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.5304.110 Safari/537.36 NetType/WIFI MicroMessenger/6.8.0(0x16080000) MacWechat/store ClientCanvas/1.0.0"
        headers = {
            "User-Agent": ua,
            "Cookie": cookie_str,
            "Accept": "application/json, text/plain, */*",
        }
        proxies = get_proxies_dict()
        proxy_url = proxies.get("http") if proxies else None

        params = {
            "action": "getmsg",
            "__biz": fakeid,
            "f": "json",
            "offset": str(begin),
            "count": str(count),
            "is_ok": "1",
            "scene": "126",
            "uin": "",
            "key": "",
            "pass_ticket": "",
            "appmsg_token": appmsg_token,
            "x5": "0",
        }

        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            try:
                resp = req.get(
                    f"{BASE_URL}/mp/profile_ext",
                    params=params,
                    headers=headers,
                    proxies={"http": None, "https": None},
                    timeout=25,
                    verify=False,
                )
            except Exception:
                from curl_cffi import requests as c_req
                resp = c_req.get(
                    f"{BASE_URL}/mp/profile_ext",
                    params=params,
                    headers=headers,
                    proxies={"http": None, "https": None},
                    timeout=25,
                    impersonate="chrome",
                    verify=False,
                )
        except Exception as exc:
            report_proxy_status(proxy_url, success=False)
            account_pool.report(account_id, http_ok=False, error=str(exc))
            last_exc = exc
            continue

        if resp.status_code != 200:
            report_proxy_status(proxy_url, success=False)
            account_pool.report(account_id, http_ok=False, error=f"HTTP {resp.status_code}")
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            continue

        report_proxy_status(proxy_url, success=True)
        try:
            data = resp.json()
        except Exception:
            last_exc = RuntimeError("返回数据非 JSON 格式（可能需要重新在微信电脑版打开历史消息更新 key/token）")
            continue

        ret = data.get("ret", 0)
        account_pool.report(account_id, ret=ret)

        if ret != 0:
            errmsg = data.get("errmsg", f"ret={ret}")

            # 如果客户端接口失败，且拥有可用 Web Token，尝试通过 /cgi-bin/appmsg 备用通道拉取
            if appmsg_token or token:
                try:
                    fallback_res = _fetch_articles_via_appmsg_fallback(fakeid, begin, count, keyword, appmsg_token or token, cookie_str)
                    if fallback_res is not None:
                        articles, total_cnt, can_continue = fallback_res
                        return articles, total_cnt, can_continue
                except Exception as fb_err:
                    print(f"Appmsg 备用通道尝试失败: {fb_err}")

            if ret in (-3, -4, -5, -6, 200003):
                # 凭证失效，尝试发起探针请求触发 mitm_proxy 静默刷新
                account_pool.try_refresh_account(account_id)
                last_exc = PermissionError(f"客户端凭证已失效 ({errmsg})，请在 PC 微信或浏览器中刷新页面更新 key/token！")
            elif ret == 200013:
                last_exc = RuntimeError("触发微信频次控制(200013)，正在切换账号重试...")
            else:
                last_exc = RuntimeError(f"微信历史消息接口错误: {errmsg}")
            continue

        articles = []
        msg_list_str = data.get("general_msg_list", "")
        if msg_list_str:
            try:
                msg_data = json.loads(msg_list_str)
                for msg in msg_data.get("list", []):
                    comm_info = msg.get("comm_msg_info", {})
                    pub_time = comm_info.get("datetime", 0)
                    msg_id = str(comm_info.get("id", ""))

                    app_msg = msg.get("app_msg_ext_info", {})
                    if app_msg and app_msg.get("title"):
                        link = app_msg.get("content_url", "").replace("\\/", "/")
                        if link.startswith("//"):
                            link = "https:" + link

                        articles.append({
                            "title": app_msg.get("title", ""),
                            "link": link,
                            "cover": app_msg.get("cover", ""),
                            "digest": app_msg.get("digest", ""),
                            "author": app_msg.get("author", ""),
                            "update_time": pub_time,
                            "is_original": False,
                            "item_show_type": 0,
                            "id": msg_id,
                        })

                        # 多图文处理
                        for sub in app_msg.get("multi_app_msg_item_list", []):
                            if sub.get("title"):
                                sub_link = sub.get("content_url", "").replace("\\/", "/")
                                if sub_link.startswith("//"):
                                    sub_link = "https:" + sub_link
                                articles.append({
                                    "title": sub.get("title", ""),
                                    "link": sub_link,
                                    "cover": sub.get("cover", ""),
                                    "digest": sub.get("digest", ""),
                                    "author": sub.get("author", ""),
                                    "update_time": pub_time,
                                    "is_original": False,
                                    "item_show_type": 0,
                                    "id": msg_id,
                                })
            except Exception as parse_err:
                print(f"解析 general_msg_list 异常: {parse_err}")

        total_cnt = data.get("total_count", len(articles))
        can_continue = data.get("can_msg_continue", 1) if isinstance(data, dict) else (1 if len(articles) > 0 else 0)
        return articles, total_cnt, can_continue

    if isinstance(last_exc, PermissionError):
        raise last_exc
    raise last_exc or RuntimeError("账号池所有账号均不可用")


@articles_bp.route("/list/<fakeid>", methods=["GET"])
def get_articles(fakeid):
    """获取指定公众号的文章列表"""
    begin = request.args.get("begin", 0, type=int)
    count = request.args.get("count", 10, type=int)
    keyword = request.args.get("keyword", "").strip()

    try:
        articles, total_count, can_continue = _fetch_articles_page(fakeid, begin, count, keyword)

        return jsonify({
            "articles": articles,
            "total": total_count,
            "can_msg_continue": can_continue,
            "begin": begin,
            "count": len(articles),
        })

    except PermissionError as e:
        return jsonify({"error": str(e)}), 401
    except (RuntimeError, req.RequestException) as e:
        return jsonify({"error": f"网络请求失败: {str(e)}"}), 500


@articles_bp.route("/list-via-appmsg/<fakeid>", methods=["GET"])
def get_articles_appmsg(fakeid):
    """使用 appmsg 接口获取文章列表（备选方案）"""
    begin = request.args.get("begin", 0, type=int)
    count = request.args.get("count", 10, type=int)

    try:
        account_id, token, cookie_str = borrow_session()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    proxy_url = None
    try:
        headers = {**DEFAULT_HEADERS, "Cookie": cookie_str}
        proxies = get_proxies_dict()
        if proxies:
            proxy_url = proxies.get("http")

        resp = req.get(
            f"{BASE_URL}/cgi-bin/appmsg",
            params={
                "action": "list_ex",
                "token": token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
                "type": "9",
                "query": "",
                "fakeid": fakeid,
                "begin": str(begin),
                "count": str(count),
            },
            headers=headers,
            proxies=proxies,
            timeout=25,
        )

        if resp.status_code != 200:
            report_proxy_status(proxy_url, success=False)
            account_pool.report(account_id, http_ok=False, error=f"HTTP {resp.status_code}")
            return jsonify({"error": f"HTTP {resp.status_code}"}), 500

        report_proxy_status(proxy_url, success=True)
        data = resp.json()
        ret = data.get("base_resp", {}).get("ret", 0)

        # 上报账号池
        account_pool.report(account_id, ret=ret)

        if ret == 200003:
            return jsonify({"error": "登录已过期"}), 401
        if ret != 0:
            return jsonify({"error": f"API错误 (ret={ret})"}), 500

        articles = []
        for item in data.get("app_msg_list", []):
            articles.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "cover": item.get("cover", ""),
                "digest": item.get("digest", ""),
                "author": item.get("author_name", ""),
                "update_time": item.get("update_time", item.get("create_time", 0)),
                "aid": item.get("aid", ""),
                "item_show_type": item.get("item_show_type", 0),
            })

        return jsonify({
            "articles": articles,
            "total": data.get("app_msg_cnt", 0),
            "begin": begin,
            "count": len(articles),
        })

    except req.RequestException as e:
        report_proxy_status(proxy_url, success=False)
        account_pool.report(account_id, http_ok=False, error=str(e))
        return jsonify({"error": f"网络请求失败: {str(e)}"}), 500


def _parse_publish_response(data: dict) -> tuple:
    """解析 appmsgpublish 返回的数据"""
    publish_page_str = data.get("publish_page", "")
    if not publish_page_str:
        return [], 0

    publish_page = json.loads(publish_page_str)
    publish_list = publish_page.get("publish_list", [])
    total_count = publish_page.get("total_count", 0)

    articles = []
    for item in publish_list:
        publish_info_str = item.get("publish_info", "")
        if not publish_info_str:
            continue
        publish_info = json.loads(publish_info_str)
        appmsgex = publish_info.get("appmsgex", [])
        for a in appmsgex:
            articles.append({
                "title": a.get("title", ""),
                "link": a.get("link", ""),
                "cover": a.get("cover", ""),
                "digest": a.get("digest", ""),
                "author": a.get("author", ""),
                "update_time": a.get("update_time", a.get("create_time", 0)),
                "is_original": a.get("copyright_type", "0") != "0",
                "item_show_type": a.get("item_show_type", 0),
            })

    return articles, total_count


@articles_bp.route("/download", methods=["POST"])
def start_download():
    """批量下载文章"""
    data = request.get_json() or {}
    articles = data.get("articles", [])
    account_name = data.get("account_name", "unknown")

    if not articles:
        return jsonify({"error": "没有选择要下载的文章"}), 400

    task_id = f"batch_{int(time.time())}"

    with _download_lock:
        _download_tasks[task_id] = {
            "status": "running",
            "total": len(articles),
            "completed": 0,
            "failed": 0,
            "current": "",
            "results": [],
            "start_time": time.time(),
        }

    thread = threading.Thread(
        target=_do_batch_download,
        args=(task_id, articles, account_name),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id, "message": f"已启动下载任务，共 {len(articles)} 篇"})


@articles_bp.route("/download-range", methods=["POST"])
def start_range_download():
    """按时间范围分页拉取文章并逐篇下载。"""
    data = request.get_json() or {}
    fakeid = data.get("fakeid", "")
    account_name = data.get("account_name", "unknown")
    start_time = data.get("start_time", 0)
    end_time = data.get("end_time", 0)
    keyword = data.get("keyword", "").strip()
    page_size = int(data.get("page_size", 10) or 10)

    if not fakeid:
        return jsonify({"error": "缺少公众号 fakeid"}), 400
    if not start_time or not end_time:
        return jsonify({"error": "请选择完整的开始和结束日期"}), 400
    if start_time > end_time:
        return jsonify({"error": "开始日期不能晚于结束日期"}), 400

    task_id = f"range_{int(time.time())}"
    with _download_lock:
        _download_tasks[task_id] = {
            "status": "running",
            "mode": "range",
            "total": 0,
            "completed": 0,
            "failed": 0,
            "scanned": 0,
            "current": "",
            "results": [],
            "start_time": time.time(),
            "cancel_requested": False,
            "stop_reason": "",
        }

    thread = threading.Thread(
        target=_do_range_download,
        args=(task_id, fakeid, account_name, start_time, end_time, keyword, page_size),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id, "message": "已启动按时间范围下载任务"})


@articles_bp.route("/download-cancel/<task_id>", methods=["POST"])
def cancel_download(task_id):
    """请求停止下载任务。"""
    with _download_lock:
        task = _download_tasks.get(task_id)
        if not task:
            return jsonify({"error": "任务不存在"}), 404
        if task["status"] not in ("running",):
            return jsonify({"message": "任务已结束"})
        task["cancel_requested"] = True
        task["status"] = "cancelling"
        task["stop_reason"] = "用户请求停止"
    return jsonify({"message": "正在停止下载任务"})


@articles_bp.route("/download-url", methods=["POST"])
def download_by_url():
    """通过 URL 下载单篇文章"""
    data = request.get_json() or {}
    urls = data.get("urls", [])
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split("\n") if u.strip()]

    if not urls:
        return jsonify({"error": "请输入文章 URL"}), 400

    # 构造文章列表
    articles = [{"title": f"article_{i+1}", "link": url} for i, url in enumerate(urls)]

    task_id = f"url_{int(time.time())}"

    with _download_lock:
        _download_tasks[task_id] = {
            "status": "running",
            "total": len(articles),
            "completed": 0,
            "failed": 0,
            "current": "",
            "results": [],
            "start_time": time.time(),
        }

    thread = threading.Thread(
        target=_do_batch_download,
        args=(task_id, articles, "url_download"),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id, "message": f"已启动下载任务，共 {len(urls)} 篇"})


@articles_bp.route("/download-progress/<task_id>", methods=["GET"])
def get_download_progress(task_id):
    """获取下载进度（SSE）"""
    def generate():
        while True:
            with _download_lock:
                task = _download_tasks.get(task_id)

            if not task:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                break

            yield f"data: {json.dumps(task, ensure_ascii=False)}\n\n"

            if task["status"] in ("completed", "failed"):
                break

            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@articles_bp.route("/download-status/<task_id>", methods=["GET"])
def get_download_status(task_id):
    """获取下载任务状态（普通 HTTP）"""
    with _download_lock:
        task = _download_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


def _sync_history_from_disk(history: list) -> bool:
    """自动扫描本地下载文件夹，同步历史记录中缺失的条目"""
    settings = get_settings()
    download_dir = Path(settings.get("download_dir") or str(OUTPUT_DIR))
    if not download_dir.exists():
        return False

    existing_paths = {item.get("path") for item in history if isinstance(item, dict) and item.get("path")}
    changed = False

    try:
        for account_dir in download_dir.iterdir():
            if not account_dir.is_dir() or account_dir.name in ("channels", "douyin_downloads", "xhs_downloads", "temp_uploads"):
                continue

            for art_dir in account_dir.iterdir():
                if not art_dir.is_dir():
                    continue

                art_path_str = str(art_dir)
                if art_path_str in existing_paths:
                    continue

                meta_file = art_dir / "metadata.json"
                if not meta_file.exists():
                    continue

                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    link = meta.get("url", "")
                    mtime = art_dir.stat().st_mtime
                    time_val = mtime
                    if meta.get("time"):
                        try:
                            # 尝试解析 2026-06-17T08:12:44 等标准 ISO 格式时间
                            time_val = datetime.fromisoformat(meta["time"]).timestamp()
                        except Exception:
                            pass

                    new_item = {
                        "title": meta.get("title") or art_dir.name,
                        "link": link,
                        "account": account_dir.name,
                        "success": True,
                        "time": time_val,
                        "error": None,
                        "path": art_path_str,
                        "cover_url": meta.get("cover_url", ""),
                        "digest": meta.get("digest", ""),
                        "publish_time": meta.get("publish_time") or int(time_val),
                    }
                    history.append(new_item)
                    existing_paths.add(art_path_str)
                    changed = True
                except Exception:
                    pass
    except Exception:
        pass

    return changed


@articles_bp.route("/history", methods=["GET"])
def get_history():
    """获取下载历史"""
    history = load_json(DOWNLOAD_HISTORY_FILE, [])
    
    # 自动从磁盘同步丢失的历史记录
    if _sync_history_from_disk(history):
        save_json(DOWNLOAD_HISTORY_FILE, history)

    indexed_history = []
    for index, item in enumerate(history):
        if isinstance(item, dict):
            if item.get("account") == "微信视频号":
                continue
            indexed = dict(item)
            indexed["_index"] = index
            indexed_history.append(indexed)
    indexed_history.sort(key=lambda x: x.get("time", 0), reverse=True)
    # 限制返回数量
    limit = request.args.get("limit", 50, type=int)
    sliced_history = indexed_history if limit <= 0 else indexed_history[:limit]
    return jsonify({"history": sliced_history, "total": len(indexed_history)})


@articles_bp.route("/history", methods=["DELETE"])
def clear_history():
    """清空下载历史，并删除对应的本地下载文件/目录"""
    history = load_json(DOWNLOAD_HISTORY_FILE, [])
    deleted_dirs_count = 0
    remaining_history = []

    for item in history:
        if not isinstance(item, dict):
            continue

        # 保留微信视频号的历史记录不作处理（视频号有自己的 channels.py 清空接口）
        if item.get("account") == "微信视频号":
            remaining_history.append(item)
            continue

        path_str = item.get("path", "")
        if path_str:
            try:
                path = Path(path_str)
                if path.exists():
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    deleted_dirs_count += 1
            except Exception:
                pass

    save_json(DOWNLOAD_HISTORY_FILE, remaining_history)

    # 自动清理空出来的公众号目录
    try:
        settings = get_settings()
        download_dir = Path(settings.get("download_dir") or str(OUTPUT_DIR))
        if download_dir.exists():
            for account_dir in download_dir.iterdir():
                if account_dir.is_dir() and account_dir.name not in ("channels", "douyin_downloads", "xhs_downloads", "temp_uploads"):
                    # 如果该公众号目录没有任何文件/文件夹，则将其删除
                    if not any(account_dir.iterdir()):
                        account_dir.rmdir()
    except Exception:
        pass

    return jsonify({
        "message": f"历史已清空，成功删除 {deleted_dirs_count} 个本地下载内容。"
    })


@articles_bp.route("/history/<int:index>", methods=["DELETE"])
def delete_history_item(index):
    """删除单条下载历史，并删除对应下载文件夹。"""
    history = load_json(DOWNLOAD_HISTORY_FILE, [])
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
            # 文件被占用或删除失败时不阻塞历史记录本身的删除
            file_status = f"error: {str(e)}"

    history.pop(index)
    save_json(DOWNLOAD_HISTORY_FILE, history)

    messages = {
        "deleted": "已删除下载文件和记录",
        "missing": "下载文件已不存在，已删除记录",
        "no_path": "记录没有文件路径，已删除记录",
    }
    msg = messages.get(file_status, f"下载文件清理失败（{file_status}），但已清除历史记录")
    return jsonify({"message": msg, "file_status": file_status})


def _do_batch_download(task_id: str, articles: list, account_name: str):
    """执行批量下载（后台线程）"""
    from backend.downloader import download_single_article

    settings = get_settings()
    delay = settings.get("request_delay", 0.8)
    max_retries = settings.get("max_retries", 3)

    out_dir = OUTPUT_DIR / account_name
    out_dir.mkdir(parents=True, exist_ok=True)

    history = load_json(DOWNLOAD_HISTORY_FILE, [])

    try:
        for i, article in enumerate(articles):
            with _download_lock:
                task = _download_tasks.get(task_id, {})
                if task.get("cancel_requested") or task.get("status") == "cancelling":
                    task["status"] = "cancelled"
                    task["current"] = ""
                    task["end_time"] = time.time()
                    task["stop_reason"] = task.get("stop_reason") or "用户请求停止"
                    save_json(DOWNLOAD_HISTORY_FILE, history)
                    return

            link = article.get("link", "")
            title = article.get("title", f"article_{i+1}")

            with _download_lock:
                _download_tasks[task_id]["current"] = f"正在下载第 {i+1} 篇..."

            if not link:
                with _download_lock:
                    _download_tasks[task_id]["failed"] += 1
                    _download_tasks[task_id]["results"].append({
                        "title": title, "success": False, "error": "无链接"
                    })
                continue

            success = False
            error_msg = ""
            result = {}

            for attempt in range(1, max_retries + 1):
                try:
                    result = download_single_article(link, out_dir, title)
                    if result.get("success"):
                        success = True
                        break
                    error_msg = result.get("error", "未知错误")
                except Exception as e:
                    error_msg = str(e)
                time.sleep(1)

            if result.get("title"):
                title = result["title"]

            downloaded_path = result.get("path") if success else None

            with _download_lock:
                if success:
                    _download_tasks[task_id]["completed"] += 1
                    _download_tasks[task_id]["current"] = f"已完成：{title}"
                    _download_tasks[task_id]["results"].append({
                        "title": title, "success": True, "path": downloaded_path or str(out_dir / title)
                    })
                else:
                    _download_tasks[task_id]["failed"] += 1
                    _download_tasks[task_id]["current"] = f"下载失败：{title}"
                    _download_tasks[task_id]["results"].append({
                        "title": title, "success": False, "error": error_msg
                    })

            # 记录到下载历史
            history.append({
                "title": title,
                "link": link,
                "account": account_name,
                "success": success,
                "time": time.time(),
                "error": error_msg if not success else None,
                "path": downloaded_path,
                "cover_url": result.get("cover_url", ""),
                "digest": result.get("digest", ""),
                "publish_time": result.get("publish_time", int(time.time())),
            })

            if i < len(articles) - 1:
                time.sleep(delay)

        # 保存历史
        save_json(DOWNLOAD_HISTORY_FILE, history)

        with _download_lock:
            task = _download_tasks[task_id]
            if task.get("status") == "cancelling":
                task["status"] = "cancelled"
                task["stop_reason"] = task.get("stop_reason") or "用户请求停止"
            else:
                task["status"] = "completed"
            task["current"] = ""
            task["end_time"] = time.time()
    finally:
        try:
            if settings.get("rss_upload_enabled", False):
                from backend.rss_scheduler import rss_scheduler
                rss_scheduler.force_upload_all(account_name)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("下载完成后自动上传失败 [%s]: %s", account_name, e)


def _download_article_into_task(task_id: str, article: dict, account_name: str, history: list, index: int = 0):
    """Download one article and update task progress."""
    from backend.downloader import download_single_article

    settings = get_settings()
    max_retries = settings.get("max_retries", 3)
    out_dir = OUTPUT_DIR / account_name
    out_dir.mkdir(parents=True, exist_ok=True)

    link = article.get("link", "")
    title = article.get("title", f"article_{index + 1}")

    with _download_lock:
        _download_tasks[task_id]["current"] = title

    if not link:
        with _download_lock:
            _download_tasks[task_id]["failed"] += 1
            _download_tasks[task_id]["results"].append({
                "title": title, "success": False, "error": "无链接"
            })
        return

    success = False
    error_msg = ""
    result = {}
    for _ in range(max_retries):
        try:
            result = download_single_article(link, out_dir, title)
            if result.get("success"):
                success = True
                break
            error_msg = result.get("error", "未知错误")
        except Exception as e:
            error_msg = str(e)
        time.sleep(1)

    if result.get("title"):
        title = result["title"]
    downloaded_path = result.get("path") if success else None

    with _download_lock:
        if success:
            _download_tasks[task_id]["completed"] += 1
            _download_tasks[task_id]["results"].append({
                "title": title, "success": True, "path": downloaded_path or str(out_dir / title)
            })
        else:
            _download_tasks[task_id]["failed"] += 1
            _download_tasks[task_id]["results"].append({
                "title": title, "success": False, "error": error_msg
            })

    history.append({
        "title": title,
        "link": link,
        "account": account_name,
        "success": success,
        "time": time.time(),
        "error": error_msg if not success else None,
        "path": downloaded_path,
        "cover_url": result.get("cover_url", ""),
        "digest": result.get("digest", ""),
        "publish_time": result.get("publish_time", int(time.time())),
    })


def _do_range_download(
    task_id: str,
    fakeid: str,
    account_name: str,
    start_time: int,
    end_time: int,
    keyword: str,
    page_size: int,
):
    """分页拉取文章，下载时间范围内文章，遇到更早文章后停止。"""
    settings = get_settings()
    delay = settings.get("request_delay", 0.8)
    history = load_json(DOWNLOAD_HISTORY_FILE, [])
    begin = 0
    downloaded_index = 0
    stop = False

    try:
        try:
            while not stop:
                with _download_lock:
                    task = _download_tasks.get(task_id, {})
                    if task.get("cancel_requested") or task.get("status") == "cancelling":
                        task["status"] = "cancelled"
                        task["current"] = ""
                        task["end_time"] = time.time()
                        task["stop_reason"] = task.get("stop_reason") or "用户请求停止"
                        save_json(DOWNLOAD_HISTORY_FILE, history)
                        return
                    task["current"] = f"正在获取第 {begin // page_size + 1} 页"

                res = _fetch_articles_page(fakeid, begin, page_size, keyword)
                articles, total_count = res[0], res[1]
                if not articles:
                    stop = True
                    with _download_lock:
                        _download_tasks[task_id]["stop_reason"] = "没有更多文章"
                    break

                with _download_lock:
                    _download_tasks[task_id]["scanned"] += len(articles)

                out_of_range_count = 0
                for article in articles:
                    article_time = article.get("update_time") or 0
                    # update_time 为 0 表示时间未知，不做时间过滤（宁多勿漏）
                    if article_time > 0:
                        if article_time > end_time:
                            continue
                        if article_time < start_time:
                            out_of_range_count += 1
                            continue

                    with _download_lock:
                        task = _download_tasks.get(task_id, {})
                        if task.get("cancel_requested") or task.get("status") == "cancelling":
                            task["status"] = "cancelled"
                            task["current"] = ""
                            task["end_time"] = time.time()
                            task["stop_reason"] = task.get("stop_reason") or "用户请求停止"
                            save_json(DOWNLOAD_HISTORY_FILE, history)
                            return
                        task["total"] += 1

                    _download_article_into_task(task_id, article, account_name, history, downloaded_index)
                    downloaded_index += 1
                    # 增加防风控随机抖动延迟 (1.2s - 2.5s) 模拟人类请求
                    import random
                    sleep_time = max(1.0, delay) + random.uniform(0.3, 1.2)
                    time.sleep(sleep_time)

                # 当前页所有文章都早于 start_time，说明后续页也不会有范围内的文章了
                if out_of_range_count > 0 and out_of_range_count >= len(articles):
                    stop = True
                    with _download_lock:
                        _download_tasks[task_id]["stop_reason"] = "已到达所选时间范围之前的文章"

                begin += page_size
                if total_count and begin >= total_count:
                    with _download_lock:
                        _download_tasks[task_id]["stop_reason"] = "已扫描全部文章"
                    break

            save_json(DOWNLOAD_HISTORY_FILE, history)
            with _download_lock:
                task = _download_tasks[task_id]
                if task["status"] not in ("cancelled",):
                    task["status"] = "completed"
                task["current"] = ""
                task["end_time"] = time.time()

        except PermissionError as e:
            save_json(DOWNLOAD_HISTORY_FILE, history)
            with _download_lock:
                task = _download_tasks[task_id]
                task["status"] = "failed"
                task["current"] = ""
                task["stop_reason"] = str(e)
                task["end_time"] = time.time()
        except Exception as e:
            save_json(DOWNLOAD_HISTORY_FILE, history)
            with _download_lock:
                task = _download_tasks[task_id]
                task["status"] = "failed"
                task["current"] = ""
                task["stop_reason"] = str(e)
                task["end_time"] = time.time()
    finally:
        try:
            if settings.get("rss_upload_enabled", False):
                from backend.rss_scheduler import rss_scheduler
                rss_scheduler.force_upload_all(account_name)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("下载完成后自动上传失败 [%s]: %s", account_name, e)


@articles_bp.route("/open-folder", methods=["POST"])
def open_folder():
    """在系统文件管理器中打开微信下载目录"""
    import subprocess
    import sys
    
    data = request.get_json() or {}
    account = data.get("account", "").strip()
    
    settings = get_settings()
    download_dir_str = settings.get("download_dir") or str(OUTPUT_DIR)
    path = Path(download_dir_str)
    
    if account:
        path = path / account
    
    try:
        path.mkdir(parents=True, exist_ok=True)
        resolved_path = str(path.resolve())
        if sys.platform == "darwin":
            subprocess.run(["open", resolved_path])
        elif sys.platform == "win32":
            os.startfile(resolved_path)
        else:
            subprocess.run(["xdg-open", resolved_path])
        return jsonify({"message": "文件夹已打开"})
    except Exception as e:
        return jsonify({"error": f"打开文件夹失败: {str(e)}"}), 500


@articles_bp.route("/open-file", methods=["POST"])
def open_file():
    """在系统默认程序中打开特定的文件或文件夹"""
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

        resolved_path = str(path.resolve())
        if sys.platform == "darwin":
            subprocess.run(["open", resolved_path])
        elif sys.platform == "win32":
            os.startfile(resolved_path)
        else:
            subprocess.run(["xdg-open", resolved_path])
        return jsonify({"message": "已打开"})
    except Exception as e:
        return jsonify({"error": f"打开失败: {str(e)}"}), 500


@articles_bp.route("/open-parent", methods=["POST"])
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
                os.startfile(resolved_path)
            else:
                subprocess.run(["xdg-open", resolved_path])
        return jsonify({"message": "已打开"})
    except Exception as e:
        return jsonify({"error": f"打开失败: {str(e)}"}), 500


@articles_bp.route("/serve-file/<path:filepath>", methods=["GET"])
def serve_file(filepath):
    """
    Serve a file from the configured download directory.
    """
    from flask import send_from_directory
    from urllib.parse import unquote
    settings = get_settings()
    download_dir = Path(settings.get("download_dir") or str(OUTPUT_DIR))
    
    try:
        # Resolve target path safely
        target_path = (download_dir / unquote(filepath)).resolve()
        
        # Check exists
        if not target_path.exists():
            return "File not found", 404
            
        # Security check: ensure path is within download_dir
        if not str(target_path).startswith(str(download_dir.resolve())):
            return "Access denied", 403
            
        return send_from_directory(target_path.parent, target_path.name)
    except Exception as e:
        return f"Error serving file: {str(e)}", 500


@articles_bp.route("/rss", methods=["GET"])
@articles_bp.route("/rss/<account>", methods=["GET"])
def get_rss(account=None):
    """
    Generate an RSS 2.0 feed of successfully downloaded articles.
    Merges manually downloaded articles with auto-fetched RSS subscription articles.
    """
    import email.utils
    from urllib.parse import quote
    import html

    history = load_json(DOWNLOAD_HISTORY_FILE, [])
    # Filter successful downloads and exclude channels
    items = [item for item in history if isinstance(item, dict) and item.get("success") and item.get("account") != "微信视频号"]

    # Filter by account if specified
    if account:
        items = [item for item in items if item.get("account") == account]
        feed_title = f"微信公众号 - {account} RSS"
        feed_desc = f"{account} 微信公众号文章订阅"
    else:
        feed_title = "微信公众号 RSS"
        feed_desc = "微信公众号文章订阅"

    # Merge auto-fetched RSS articles
    try:
        from backend.rss_scheduler import rss_scheduler
        if account:
            rss_arts = rss_scheduler.get_articles(account)
        else:
            # Get articles from all subscriptions
            rss_arts = []
            for sub in rss_scheduler.get_subscriptions():
                rss_arts.extend(rss_scheduler.get_articles(sub.get("nickname", "")))

        # Convert auto-fetched articles to the same format, dedup by link
        existing_links = {item.get("link") for item in items if item.get("link")}
        for art in rss_arts:
            link = art.get("link", "")
            if link and link not in existing_links:
                items.append({
                    "title": art.get("title", ""),
                    "link": link,
                    "account": account or art.get("author", ""),
                    "success": True,
                    "time": art.get("update_time", 0),
                    "publish_time": art.get("update_time", 0),
                    "cover_url": art.get("cover", ""),
                    "digest": art.get("digest", ""),
                    "path": art.get("path", ""),
                })
                existing_links.add(link)
    except Exception:
        pass  # If scheduler not available, just use download history
        
    # Get server host/port dynamically to construct local URLs
    host_url = request.host_url
    
    xml_items = []
    for item in items:
        # Defensively convert all values to strings to prevent AttributeError on html.escape(None)
        title_val = item.get("title") or ""
        title = html.escape(str(title_val))
        
        acc_name_val = item.get("account") or "unknown"
        acc_name = str(acc_name_val)
        
        safe_title = item.get("title") or ""
        
        # Check if local HTML file exists
        local_exists = False
        path_str = item.get("path")
        if path_str:
            try:
                local_path = Path(path_str)
                if local_path.exists():
                    local_exists = True
            except Exception:
                pass

        orig_url_val = item.get("link") or ""
        orig_url = html.escape(str(orig_url_val))

        if local_exists:
            p_obj = Path(path_str)
            local_url = f"{host_url}api/articles/serve-file/{quote(p_obj.parent.name)}/{quote(p_obj.name)}/{quote(p_obj.name)}.html"
            xml_link = html.escape(local_url)
        else:
            xml_link = orig_url
        
        pub_time_val = item.get("publish_time") or item.get("time") or time.time()
        try:
            pub_time = float(pub_time_val)
        except Exception:
            pub_time = time.time()
        pub_date = email.utils.formatdate(pub_time, usegmt=True)
        
        digest_val = item.get("digest") or ""
        digest = html.escape(str(digest_val))
        
        cover_url_val = item.get("cover_url") or ""
        cover_url = html.escape(str(cover_url_val))
        
        # Read content.txt if available for full text
        content_encoded = ""
        if path_str:
            try:
                txt_path = Path(path_str) / "content.txt"
                if txt_path.exists():
                    clean_text = txt_path.read_text(encoding="utf-8")
                    clean_text = clean_text.replace("]]>", "]]&gt;")
                    content_encoded = f"<content:encoded><![CDATA[{clean_text}]]></content:encoded>"
            except Exception:
                pass
                
        enclosure = f'<enclosure url="{cover_url}" type="image/jpeg" length="0"/>' if cover_url else ""
        
        xml_items.append(f"""    <item>
      <title>{title}</title>
      <link>{xml_link}</link>
      <guid isPermaLink="false">{orig_url or xml_link}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>{digest}</description>
      {enclosure}
      {content_encoded}
    </item>""")
    
    items_str = "\n".join(xml_items)
    
    rss_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{html.escape(feed_title)}</title>
    <link>{host_url}</link>
    <description>{html.escape(feed_desc)}</description>
    <language>zh-CN</language>
    <lastBuildDate>{email.utils.formatdate(time.time(), usegmt=True)}</lastBuildDate>
{items_str}
  </channel>
</rss>"""

    return Response(rss_xml, mimetype="application/xml")
