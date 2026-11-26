# -*- coding: utf-8 -*-
"""
微信视频号优化专项测试（涵盖 Bundle Hook、LiteApp、并发安全、CDN IPv4 回退、解析容错与前端 JS 逻辑）
"""

import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import requests
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestBundleHooksAndLiteApp(unittest.TestCase):
    def test_hook_real_virtual_svg_and_apply_mic_bundles(self):
        from backend.mitm_proxy import _hook_wx_bundle

        sample_v = """
const svc = {
  async finderInit() { return { data: {} }; },
  async finderPcFlow(p) { return { data: { object: [] } }; },
  async finderGetRecommend(p) { return { data: { object: [] } }; },
  async getRecommendTabsFromService() { return { tagItem: [] }; },
  async finderGetCommentDetail(p) { return { data: { object: {} } }; },
  async finderGetCommentList(p) { return { data: {} }; },
  async finderPCSearch(p) { return { data: {} }; },
  async finderSearch(p) { return { data: {} }; },
  async finderGetInteractionedFeedList(p) { return { data: { object: [] } }; },
  async finderGetPlayHistory(p) { return { data: { object: [] } }; },
  async finderGetFeedH5Url(p) { return { data: {} }; },
  async finderGetFollowList(p) { return { data: {} }; },
  async finderUserPage(p) { return { data: { object: [], lastBuffer: "" } }; },
  async finderLiveUserPage(p) { return { data: { object: [] } }; },
  async finderGetLiveInfo(p) { return { data: {} }; },
  async joinLive(p) { return { data: {} }; },
  async preload(x) { const {data:d,code:c}=await x.invoke(x);(()=>{return d;})(); }
};
const ft = { value: { feeds: [], currentFeedIndex: 0 } };
const lt = { value: { feeds: [], currentFeedIndex: 0 } };
function nextFn() {}
function prevFn() {}
function localFn() {}
const ctrl = {flowTab:ft,goToNextFlowFeed:nextFn,goToPrevFlowFeed:prevFn,localFlowTab:lt,loadLocalPlaylist:localFn};
export { svc, ctrl };
"""
        sample_a = """
const ft = { value: { feeds: [], currentFeedIndex: 0 } };
const lt = { value: { feeds: [], currentFeedIndex: 0 } };
async function workerCall(w, arg) {
  const {data:resData,code:resCode}=await w.invoke(arg);(()=>{return resData;})();
}
const nav = {flowTab:ft,goToNextFlowFeed:nextFn,goToPrevFlowFeed:prevFn,localFlowTab:lt,loadLocalPlaylist:localFn};
function nextFn() {}
function prevFn() {}
function localFn() {}
export { nav, workerCall };
"""

        samples = [
            ("synthetic_v", "/t/wx_fed/finder/web/web-finder/res/js/virtual_svg-icons-register.publish.js", sample_v),
            ("synthetic_a", "/t/wx_fed/finder/web/web-finder/res/js/applyMic.publish.js", sample_a),
        ]
        raw_v = PROJECT_ROOT / "data" / "diag_bundles" / "virtual_svg-icons-register_publish_raw.js"
        raw_a = PROJECT_ROOT / "data" / "diag_bundles" / "applyMic_publish_raw.js"
        if raw_v.exists() and raw_a.exists():
            samples.extend([
                ("real_v", "/t/wx_fed/finder/web/web-finder/res/js/virtual_svg-icons-register.publish.js", raw_v.read_text("utf-8")),
                ("real_a", "/t/wx_fed/finder/web/web-finder/res/js/applyMic.publish.js", raw_a.read_text("utf-8")),
            ])

        node = shutil.which("node")
        for tag, path, content in samples:
            hooked = _hook_wx_bundle(path, content)
            self.assertIn("WXU.Events.PreloadFeeds", hooked)
            self.assertIn("WXU.Events.GotoNextFeed", hooked)
            self.assertIn("WXU.Events.HomeFeedChanged", hooked)
            self.assertIn('typeof decodeBase64ToUint64String!=="undefined"', hooked)
            if "virtual_svg" in path:
                for marker in (
                    "WXU.Events.RecommendTabsLoaded",
                    "WXU.Events.GetFeedH5Url",
                    "WXU.Events.PlayHistoryFeedsLoaded",
                    "WXU.Events.FollowListLoaded",
                    'typeof WXU!=="undefined"',
                ):
                    self.assertIn(marker, hooked)
            if node:
                with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as f:
                    f.write(hooked)
                    tmp = Path(f.name)
                try:
                    res = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
                    self.assertEqual(res.returncode, 0, f"{tag} node --check failed: {res.stderr}")
                finally:
                    tmp.unlink(missing_ok=True)

    def test_liteapp_libpag_bootstrap_idempotent_and_valid(self):
        from backend.mitm_proxy import _inject_liteapp_bootstrap

        once = _inject_liteapp_bootstrap("console.log('libpag');")
        twice = _inject_liteapp_bootstrap(once)
        self.assertEqual(once, twice)
        self.assertIn("https://liteapp.weixin.qq.com", once)
        self.assertIn("https://channels.weixin.qq.com", once)
        self.assertIn("src/liteapp_runtime.js", once)

        node = shutil.which("node")
        if node:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(once)
                tmp = Path(f.name)
            try:
                res = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)
            finally:
                tmp.unlink(missing_ok=True)


class TestBackendPersistenceAndRoutes(unittest.TestCase):
    def test_atomic_save_json_and_history_kwargs(self):
        from backend.config import load_json, save_json
        import backend.channels as ch

        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            p = tmp_dir / "sub" / "test.json"
            save_json(p, {"hello": "world", "n": 1})
            self.assertEqual(load_json(p), {"hello": "world", "n": 1})
            self.assertEqual([x.name for x in p.parent.iterdir()], ["test.json"])

            hist_file = tmp_dir / "history.json"
            with mock.patch.object(ch, "CHANNELS_HISTORY_FILE", hist_file):
                ch.add_channels_history_item("基础标题", "视频", "/tmp/a.mp4", 1048576)
                ch.add_channels_history_item(
                    "上传标题",
                    "视频(自动上传)",
                    "https://cdn.example.com/1.mp4",
                    2097152,
                    feed_id="123456",
                    uploaded=True,
                    cos_url="https://cdn.example.com/1.mp4",
                    upload_error=None,
                )
            items = load_json(hist_file, [])
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["feed_id"], "123456")
            self.assertTrue(items[0]["uploaded"])
            self.assertEqual(items[1]["title"], "基础标题")

    def test_save_synced_feeds_highest_spec_and_rss_route(self):
        from backend.mitm_proxy import save_synced_feeds
        from backend.config import load_json
        import backend.channels as ch

        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            feeds_file = tmp_dir / "feeds.json"
            favs_file = tmp_dir / "favs.json"
            settings_mock = {"channels_upload_enabled": False}

            sample_feeds = [
                {
                    "id": "feed_001",
                    "createtime": 1700000000,
                    "contact": {
                        "username": "v2_author@finder",
                        "nickname": "测试作者",
                        "headUrl": "https://img.example.com/avatar.jpg",
                    },
                    "objectDesc": {
                        "mediaType": 4,
                        "flowCardDesc": {"cardDesc": "回退卡片描述]]>测试"},
                        "media": [
                            {
                                "url": "https://cdn.example.com/video",
                                "urlToken": "?token=abc",
                                "coverUrl": "https://cdn.example.com/cover.jpg",
                                "decodeKey": "123456789",
                                "spec": [
                                    {"fileFormat": "xWt7", "codingFormat": 2, "levelOrder": 10, "videoBitrate": 800},
                                    {"fileFormat": "xWt9", "codingFormat": 2, "levelOrder": 40, "videoBitrate": 3200},
                                    {"fileFormat": "xWt5", "codingFormat": 1, "levelOrder": 20, "videoBitrate": 1500},
                                ],
                            }
                        ],
                    },
                }
            ]

            with (
                mock.patch.object(ch, "CHANNELS_FEEDS_FILE", feeds_file),
                mock.patch.object(ch, "CHANNELS_FAVORITES_FILE", favs_file),
                mock.patch("backend.config.get_settings", return_value=settings_mock),
            ):
                save_synced_feeds("v2_author@finder", sample_feeds)

                db = load_json(feeds_file, {})
                self.assertIn("v2_author@finder", db)
                item = db["v2_author@finder"][0]
                self.assertIn("X-snsvideoflag=xWt9", item["video_url_h265"])
                self.assertIn("X-snsvideoflag=xWt5", item["video_url_h264"])
                self.assertEqual(item["description"], "回退卡片描述]]>测试")
                self.assertTrue(item["needs_upload"])
                self.assertEqual(len(item["specs"]), 3)

                app = Flask(__name__)
                app.register_blueprint(ch.channels_bp)
                client = app.test_client()

                resp_rss = client.get("/api/channels/rss/v2_author%40finder")
                self.assertEqual(resp_rss.status_code, 200)
                root = ET.fromstring(resp_rss.data)
                titles = [el.text for el in root.findall("./channel/item/title")]
                self.assertTrue(titles and "回退卡片描述" in titles[0])

                resp_sync = client.post(
                    "/api/channels/sync-feed",
                    json={"username": "v2_author@finder", "feeds": sample_feeds},
                )
                self.assertEqual(resp_sync.status_code, 200)
                self.assertTrue(resp_sync.get_json()["success"])

                with (
                    mock.patch("backend.channels.is_wechat_running", return_value=False),
                    mock.patch("os.path.exists", return_value=True),
                    mock.patch("os.listdir", side_effect=PermissionError(1, "Operation not permitted")),
                ):
                    resp_cache = client.post("/api/channels/clear-cache")
                    self.assertEqual(resp_cache.status_code, 400)
                    err_txt = resp_cache.get_json()["error"]
                    self.assertTrue("沙盒" in err_txt or "无法完全删除" in err_txt)

    def test_windows_simple_cache_eviction_and_dynamic_flask_url(self):
        import os
        import struct
        import backend.channels as ch
        from backend.mitm_proxy import ChannelsAddon

        with tempfile.TemporaryDirectory() as td:
            prof = Path(td) / "Tencent" / "xwechat" / "radium" / "web" / "profiles" / "Default"
            cache_data = prof / "Cache" / "Cache_Data"
            cache_data.mkdir(parents=True)

            # 创建命中 libpag 的 Simple Cache 条目与无关条目（Cookies 应保留）
            key_hit = b"1/0/https://res.wx.qq.com/t/wx_fed/cdn_libs/res/libpag/4.3.48/libpag.min.js"
            hdr_hit = struct.pack("<QIIIQ", 0xFCFB6D1BA7725C30, 5, len(key_hit), 0, 0)
            (cache_data / "a1b2c3d4e5f60001_0").write_bytes(hdr_hit + key_hit + b"payload")
            (cache_data / "a1b2c3d4e5f60001_s").write_bytes(b"meta")

            key_keep = b"1/0/https://example.com/other.png"
            hdr_keep = struct.pack("<QIIIQ", 0xFCFB6D1BA7725C30, 5, len(key_keep), 0, 0)
            (cache_data / "a1b2c3d4e5f60002_0").write_bytes(hdr_keep + key_keep + b"keep")

            with (
                mock.patch("backend.channels.sys.platform", "win32"),
                mock.patch("backend.channels.is_wechat_running", return_value=False),
                mock.patch.dict(os.environ, {"APPDATA": td, "LOCALAPPDATA": str(Path(td) / "empty")}),
            ):
                removed = ch.evict_windows_channels_cache_if_idle()
                self.assertEqual(removed, 2)
                self.assertFalse((cache_data / "a1b2c3d4e5f60001_0").exists())
                self.assertFalse((cache_data / "a1b2c3d4e5f60001_s").exists())
                self.assertTrue((cache_data / "a1b2c3d4e5f60002_0").exists())

            with mock.patch.dict(os.environ, {"WECHAT_MP_PORT": "5207"}):
                self.assertEqual(ChannelsAddon._flask_url("/api/channels/download"), "http://127.0.0.1:5207/api/channels/download")


class TestCDNFallbackParserAndDecrypt(unittest.TestCase):
    def test_open_cdn_response_retries_ipv4_on_ssl_error(self):
        from backend.channels import open_cdn_response, _IPv4HTTPAdapter

        calls = []

        def fake_get(self, url, **kwargs):
            has_ipv4_adapter = isinstance(self.adapters.get("https://"), _IPv4HTTPAdapter)
            calls.append((has_ipv4_adapter, self.trust_env, kwargs.get("proxies")))
            if not has_ipv4_adapter:
                raise requests.exceptions.SSLError("TLSV1_UNRECOGNIZED_NAME over IPv6")
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b"ok-data"
            return resp

        with mock.patch.object(requests.Session, "get", fake_get):
            sess, resp = open_cdn_response("https://finder.video.qq.com/test.mp4", stream=False)
            try:
                self.assertEqual(resp.content, b"ok-data")
            finally:
                sess.close()

        self.assertEqual(calls, [
            (False, False, {"http": None, "https": None}),
            (True, False, {"http": None, "https": None}),
        ])

    def test_extract_share_url_and_yuanbao_html_error_stripping(self):
        from backend.channels import _extract_channels_share_url, local_parse_with_yuanbao

        self.assertEqual(
            _extract_channels_share_url("快来看视频 https://channels.weixin.qq.com/finder-preview/pages/sph?id=abc&x=1 精彩"),
            "https://channels.weixin.qq.com/finder-preview/pages/sph?id=abc&x=1",
        )
        self.assertEqual(
            _extract_channels_share_url("https://weixin.qq.com/sph/Abc123!"),
            "https://weixin.qq.com/sph/Abc123",
        )

        r1 = mock.Mock(status_code=200)
        r1.json.return_value = {"code": 0, "data": {"playable_url": "https://example.com/?token=t1&eid=e1"}}
        r2 = mock.Mock(status_code=200)
        r2.json.return_value = {"errCode": -200, "errMsg": "<p>视频<b>不存在</b>或已删除</p>"}

        with (
            mock.patch("backend.channels.requests.post", side_effect=[r1, r2]),
            tempfile.TemporaryDirectory() as td,
            mock.patch("backend.config.DATA_DIR", Path(td)),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                local_parse_with_yuanbao("https://weixin.qq.com/sph/Abc123", "cookie=1")
            self.assertEqual(str(ctx.exception), "视频 不存在 或已删除")

    def test_isaac64_decrypt_round_trip(self):
        from backend.channels import decrypt_channels_data

        plain = bytearray(range(256)) * 4
        buf = bytearray(plain)
        decrypt_channels_data(buf, 9876543210, enc_len=len(buf))
        self.assertNotEqual(buf, plain)
        decrypt_channels_data(buf, 9876543210, enc_len=len(buf))
        self.assertEqual(buf, plain)


class TestFrontendInjectionLogicInNode(unittest.TestCase):
    def test_utils_format_feed_api_binding_and_request_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 未安装")

        js_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function createContext() {
  const xhrCalls = [];
  class FakeXHR {
    open(m, u) { this.method = m; this.url = u; }
    setRequestHeader() {}
    send(body) {
      xhrCalls.push({ method: this.method, url: this.url, body: body ? JSON.parse(body) : null });
      if (this.url.endsWith("/__wx_channels_api/download")) {
        this.status = 200;
        this.responseText = JSON.stringify({ success: true, filename: "ok.mp4" });
      } else {
        this.status = 200;
        this.responseText = JSON.stringify({ code: 0, data: { enabled: true } });
      }
      this.onload();
    }
  }
  const ctx = {
    window: {
      location: { origin: "https://channels.weixin.qq.com", href: "https://channels.weixin.qq.com/web/pages/home" },
      addEventListener() {},
      __wx_channels_api_base__: "https://channels.weixin.qq.com",
    },
    document: {
      addEventListener() {},
      querySelector() { return null; },
      getElementById() { return null; },
      head: { appendChild() {} },
    },
    location: { origin: "https://channels.weixin.qq.com", href: "https://channels.weixin.qq.com/web/pages/home" },
    XMLHttpRequest: FakeXHR,
    fetch: () => Promise.resolve({ ok: true }),
    MutationObserver: class { observe() {} disconnect() {} },
    URL,
    console,
    setTimeout,
    clearTimeout,
    weui: { topTips() {}, toast() {} },
    WUI: {},
  };
  ctx.window.document = ctx.document;
  ctx.globalThis = ctx;
  return { ctx, xhrCalls };
}

const { ctx, xhrCalls } = createContext();
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("injection_scripts/lib/mitt.umd.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("injection_scripts/src/eventbus.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("injection_scripts/src/utils.js", "utf8"), ctx);

// 1. Timeless bug guard: should NOT throw or define window.h when Timeless is undefined
assert.strictEqual(ctx.window.h, undefined);

// 2. Per-method WXAPI4 binding across separate service objects
const followObj = { tag: "acct", finderGetFollowList(x) { return this.tag + ":" + x; }, finderGetPlayHistory() { return "hist"; } };
const h5Obj = { tag: "feedSvc", finderGetFeedH5Url(x) { return this.tag + ":" + x; } };
ctx.WXE.emit(ctx.WXE.Events.APILoaded, { a: followObj, b: h5Obj });
assert.strictEqual(ctx.WXU.API4.finderGetFollowList(1), "acct:1");
assert.strictEqual(ctx.WXU.API4.finderGetFeedH5Url(2), "feedSvc:2");

// 3. format_feed BGM & flowCardDesc fallback
const pic = ctx.WXU.format_feed({
  id: "p1",
  objectNonceId: "n1",
  contact: { username: "u1", nickname: "nick", headUrl: "h" },
  objectDesc: {
    mediaType: 2,
    flowCardDesc: { cardDesc: "卡片图文标题" },
    musicInfo: { musicPlayUrl: "https://cdn.example.com/bgm.mp3", songName: "Song" },
    media: [{ url: "https://img.example.com/1", urlToken: "?t=1", coverUrl: "https://img.example.com/c.jpg" }],
  },
});
assert.strictEqual(pic.title, "卡片图文标题");
assert.strictEqual(pic.bgm.url, "https://cdn.example.com/bgm.mp3");

// 4. WXU.request handles {success: true} WITHOUT code field and supports cross-origin prefix
(async () => {
  const [err, res] = await ctx.WXU.downloader.create(pic);
  assert.strictEqual(err, null);
  assert.strictEqual(res.success, true);
  assert.strictEqual(xhrCalls[0].url, "https://channels.weixin.qq.com/__wx_channels_api/download");
  assert.strictEqual(xhrCalls[0].body.type, "picture");
  assert.deepStrictEqual(xhrCalls[0].body.files, ["https://img.example.com/1?t=1"]);
  assert.strictEqual(xhrCalls[0].body.bgm_url, "https://cdn.example.com/bgm.mp3");

  const [err2, cfg] = await ctx.WXU.request({ method: "GET", url: "/__wx_channels_api/harvest-config" });
  assert.strictEqual(err2, null);
  assert.strictEqual(cfg.enabled, true);
  console.log("NODE_JS_HARNESS_OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""
        res = subprocess.run([node, "-e", js_harness], cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("NODE_JS_HARNESS_OK", res.stdout)

    def test_liteapp_runtime_webpack_pinia_and_button_mount(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 未安装")

        liteapp_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

function makeNode(tag, cls = "") {
  const children = [];
  return {
    tagName: tag.toUpperCase(),
    className: cls,
    nodeType: 1,
    children,
    style: {},
    innerHTML: "",
    onclick: null,
    parentElement: null,
    appendChild(ch) { ch.parentElement = this; children.push(ch); return ch; },
    insertAdjacentHTML() {},
    addEventListener() {},
    setAttribute() {},
    closest(sel) {
      let cur = this;
      while (cur) {
        if (sel === ".feed-interact" && cur.className.includes("feed-interact")) return cur;
        if (sel === ".slides-item" && cur.className.includes("slides-item")) return cur;
        cur = cur.parentElement;
      }
      return null;
    },
    matches(sel) {
      const classes = String(this.className || "").split(/\s+/).filter(Boolean);
      return sel.split(",").some((part) => {
        const last = part.trim().split(/\s+/).pop() || "";
        const req = last.split(".").filter(Boolean);
        return req.length > 0 && req.every((c) => classes.includes(c));
      });
    },
    querySelector(sel) {
      for (const ch of children) {
        if (ch.matches && ch.matches(sel)) return ch;
        const deep = ch.querySelector && ch.querySelector(sel);
        if (deep) return deep;
      }
      return null;
    },
    querySelectorAll(sel) {
      const out = [];
      for (const ch of children) {
        if (ch.matches && ch.matches(sel)) out.push(ch);
        if (ch.querySelectorAll) out.push(...ch.querySelectorAll(sel));
      }
      return out;
    },
  };
}

const slide = makeNode("div", "slides-item");
const interact = makeNode("div", "feed-interact");
const opItem = makeNode("div", "click-box op-item");
interact.appendChild(opItem);
slide.appendChild(interact);

const sampleFeed = {
  id: "f_lite_1",
  objectNonceId: "nonce_1",
  createtime: 1700000001,
  contact: { username: "lite_author@finder", nickname: "Lite作者", headUrl: "https://h" },
  objectDesc: {
    mediaType: 4,
    description: "LiteApp测试视频",
    media: [{ url: "https://cdn.example.com/v", urlToken: "?tk=1", coverUrl: "https://c", decodeKey: "888", spec: [] }],
  },
};

class FinderApi {
  async finderInit() { return { data: { ok: true } }; }
  async finderGetCommentDetail(p) { return { data: { object: { ...sampleFeed, detailLoaded: p.objectid } } }; }
}
const homeStore = {
  flowCurrentFeed: sampleFeed,
  _handlers: [],
  $onAction(cb) { this._handlers.push(cb); return () => {}; },
  triggerNext(nextFeed) {
    this._handlers.forEach((h) => h({
      name: "goToNextFlowFeed",
      after: (fn) => {
        this.flowCurrentFeed = nextFeed;
        fn();
      },
    }));
  },
};
const appEl = makeNode("div", "");
appEl.id = "app";
appEl.__vue_app__ = {
  config: {
    globalProperties: {
      $pinia: {
        _s: new Map([
          ["feed", { feed: sampleFeed }],
          ["home", homeStore],
        ]),
      },
    },
  },
};
const headEl = makeNode("head");
const bodyEl = makeNode("body");
bodyEl.appendChild(appEl);
bodyEl.appendChild(slide);

function wpRequire(id) {
  if (id === "api") return { FinderApi };
  if (id === "svc") return { svc: { finderGetCommentDetail: FinderApi.prototype.finderGetCommentDetail, finderInit: FinderApi.prototype.finderInit, finderPcFlow: async () => ({}), getRecommendTabsFromService: async () => ({}) } };
  return {};
}
wpRequire.m = {
  api: { toString: () => "async finderGetCommentDetail(){} async finderInit(){}" },
  svc: { toString: () => "async finderPcFlow(){} getRecommendTabsFromService(){}" },
};

const ctx = {
  window: {
    location: { origin: "https://liteapp.weixin.qq.com", href: "https://liteapp.weixin.qq.com/a/finder/wxalite28e098851253e34f4777f7ca024236fd/index.html" },
    addEventListener() {},
    __wx_channels_api_base__: "https://channels.weixin.qq.com",
    webpackChunk: { push(entry) { entry[2](wpRequire); } },
  },
  document: {
    readyState: "complete",
    head: headEl,
    body: bodyEl,
    addEventListener() {},
    createElement(tag) {
      const n = makeNode(tag);
      let _html = "";
      Object.defineProperty(n, "innerHTML", {
        get() { return _html; },
        set(v) {
          _html = v;
          if (String(v).includes("download-icon")) {
            const box = makeNode("div", "click-box op-item");
            const ic = makeNode("div", "op-icon download-icon");
            box.appendChild(ic);
            n.firstChild = makeNode("div");
            n.firstChild.appendChild(box);
          }
        },
      });
      return n;
    },
    getElementById(id) { return id === "app" ? appEl : null; },
    querySelector(sel) {
      if (sel === "#app") return appEl;
      return bodyEl.querySelector(sel);
    },
    querySelectorAll(sel) { return bodyEl.querySelectorAll(sel); },
  },
  XMLHttpRequest: class { open() {} setRequestHeader() {} send() {} },
  fetch: () => Promise.resolve({ ok: true }),
  MutationObserver: class { observe() {} disconnect() {} },
  URL,
  URLSearchParams,
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  weui: { topTips() {}, toast() {} },
  WUI: {
    DropdownMenu: () => ({ show() {}, hide() {}, ui: { $trigger: { onMouseEnter() {}, onMouseLeave() {} } } }),
    Menu: Object.assign(() => ({ setChildren() {} }), { setTemplate() {} }),
    MenuItem: Object.assign(() => ({}), { setTemplate() {}, setIndicatorTemplate() {} }),
  },
};
ctx.location = ctx.window.location;
ctx.window.document = ctx.document;
ctx.globalThis = ctx;

vm.createContext(ctx);
vm.runInContext(fs.readFileSync("injection_scripts/lib/mitt.umd.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("injection_scripts/src/eventbus.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("injection_scripts/src/utils.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("injection_scripts/src/components.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("injection_scripts/src/liteapp_runtime.js", "utf8"), ctx);

setTimeout(() => {
  assert.ok(ctx.__wx_channels_store__.profile, "profile must be populated from LiteApp pinia/api");
  assert.strictEqual(ctx.__wx_channels_store__.profile.id, "f_lite_1");
  assert.ok(interact.querySelector(".download-icon"), "download button must mount inside .feed-interact");
  homeStore.triggerNext({ ...sampleFeed, id: "f_lite_2", objectDesc: { ...sampleFeed.objectDesc, description: "下一个视频" } });
  assert.strictEqual(ctx.__wx_channels_store__.profile.id, "f_lite_2");
  console.log("LITEAPP_RUNTIME_OK");
}, 150);
"""
        res = subprocess.run([node, "-e", liteapp_harness], cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("LITEAPP_RUNTIME_OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
