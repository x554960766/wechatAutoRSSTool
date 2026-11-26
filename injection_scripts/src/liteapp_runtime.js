/**
 * @file 微信 4.x LiteApp 运行时适配 (https://liteapp.weixin.qq.com)
 *       结合 Webpack 模块/Pinia Store 劫持与 JSBridge 兜底，
 *       完整驱动视频元数据采集、上下切换、浮窗按钮与批量采集能力。
 */
(() => {
  if (typeof window === "undefined" || !window.location) return;
  if (window.location.origin !== "https://liteapp.weixin.qq.com") return;
  if (window.__wx_channels_liteapp_runtime_init__) return;
  window.__wx_channels_liteapp_runtime_init__ = true;

  if (typeof window.__wx_username === "undefined") {
    window.__wx_username = "";
  }

  function applyLiteAppFeed(feed, onlyWhenEmpty = false) {
    if (!feed) return;
    if (onlyWhenEmpty && __wx_channels_store__.profile) return;
    WXU.set_cur_video();
    WXU.set_feed(feed);
    WXU.emit(WXE.Events.Feed, feed);
    mountLiteAppButtons();
  }

  // 订阅事件总线，保持与 home.js / feed.js 一致的行为
  WXU.onFetchFeedProfile((feed) => applyLiteAppFeed(feed, false));
  WXU.onPCFlowLoaded((feeds) => {
    if (Array.isArray(feeds) && feeds.length > 0) applyLiteAppFeed(feeds[0], true);
  });
  WXU.onRecommendFeedsLoaded((feeds) => {
    if (Array.isArray(feeds) && feeds.length > 0) applyLiteAppFeed(feeds[0], true);
  });
  WXU.onPreloadFeeds((feeds) => {
    if (Array.isArray(feeds) && feeds.length > 0) applyLiteAppFeed(feeds[0], true);
  });
  WXU.onGotoNextFeed((feed) => applyLiteAppFeed(feed, false));
  WXU.onGotoPrevFeed((feed) => applyLiteAppFeed(feed, false));
  WXU.onHomeFeedChanged((feed) => applyLiteAppFeed(feed, false));

  // ── 1. Webpack + Vue + Pinia 核心适配（微信 4.2.0 LiteApp）────────────────
  const webpackHook = {
    app: null,
    require: null,
    wrapped: new WeakSet(),
    navigationUnsub: null,

    capture() {
      const chunks = window.webpackChunk;
      const root = document.querySelector("#app");
      if (!chunks || !root || !root.__vue_app__) {
        return false;
      }
      if (!this.require) {
        chunks.push([
          ["wxchannels_liteapp_" + Date.now()],
          {},
          (require) => {
            this.require = require;
          },
        ]);
      }
      return !!(this.require && this.require.m);
    },

    findApiModules() {
      const mods = [];
      for (const [id, factory] of Object.entries(this.require.m)) {
        const source = factory.toString();
        if (!source.includes("async finderGetCommentDetail(") || source.includes("new class")) {
          continue;
        }
        try {
          const exports = this.require(id);
          if (
            exports &&
            Object.values(exports).some(
              (v) => typeof v === "function" && v.prototype && v.prototype.finderGetCommentDetail,
            )
          ) {
            mods.push({ id, exports });
          }
        } catch (_) {}
      }
      return mods;
    },

    installPrototypeHooks(exports) {
      const handleData = (res) => res && res.data;
      const handleObject = (res) => res && res.data && res.data.object;
      const events = {
        finderInit: [WXE.Events.Init, handleData],
        finderGetCommentDetail: [WXE.Events.FeedProfileLoaded, handleObject],
        finderGetCommentList: [WXE.Events.FeedCommentListLoaded, handleData],
        finderPcFlow: [WXE.Events.PCFlowLoaded, handleObject],
        finderGetRecommend: [WXE.Events.RecommendFeedsLoaded, handleObject],
        getRecommendTabsFromService: [
          WXE.Events.RecommendTabsLoaded,
          (res) => (res && res.tagItem) || [],
        ],
        finderGetInteractionedFeedList: [WXE.Events.InteractionedFeedsLoaded, handleObject],
        finderGetPlayHistory: [WXE.Events.PlayHistoryFeedsLoaded, handleObject],
        finderGetFollowList: [WXE.Events.FollowListLoaded, handleData],
        finderGetFeedH5Url: [WXE.Events.GetFeedH5Url, handleData],
        finderUserPage: [
          WXE.Events.UserFeedsLoaded,
          (res) => ({
            feeds: res && res.data && res.data.object,
            lastBuffer: res && res.data && res.data.lastBuffer,
            raw: res && res.data,
          }),
        ],
        finderLiveUserPage: [WXE.Events.LiveUserFeedsLoaded, handleObject],
        finderGetLiveInfo: [WXE.Events.LiveProfileLoaded, handleData],
        joinLive: [WXE.Events.JoinLive, handleData],
      };

      const targets = new Set();
      let wrappedCount = 0;
      for (const value of Object.values(exports)) {
        if (typeof value !== "function" || !value.prototype || !value.prototype.finderGetCommentDetail) {
          continue;
        }
        const target = value.prototype;
        if (targets.has(target)) continue;
        targets.add(target);

        for (const [name, [event, extract]] of Object.entries(events)) {
          const prev = Object.getOwnPropertyDescriptor(target, name);
          if (!prev || !prev.configurable || typeof prev.value !== "function") continue;
          if (this.wrapped.has(prev.value)) continue;
          const original = prev.value;
          const desc = {
            ...prev,
            async value(...args) {
              const result = await Reflect.apply(original, this, args);
              try {
                WXU.emit(event, extract(result));
              } catch (e) {
                console.warn("[LiteAppRuntime] emit failed:", name, e);
              }
              return result;
            },
          };
          Object.defineProperty(target, name, desc);
          this.wrapped.add(desc.value);
          wrappedCount += 1;
        }
      }
      return wrappedCount;
    },

    installNavigation() {
      const pinia = this.app && this.app.config && this.app.config.globalProperties && this.app.config.globalProperties.$pinia;
      const store = pinia && pinia._s && pinia._s.get("home");
      if (store && !this.navigationUnsub) {
        if (typeof store.$onAction === "function") {
          const events = {
            goToPrevFlowFeed: WXE.Events.GotoPrevFeed,
            goToNextFlowFeed: WXE.Events.GotoNextFeed,
            loadLocalPlaylist: WXE.Events.HomeFeedChanged,
          };
          this.navigationUnsub = store.$onAction(({ name, after }) => {
            const event = events[name];
            if (!event) return;
            const prevId = store.flowCurrentFeed && store.flowCurrentFeed.id;
            after(() => {
              const feed = store.flowCurrentFeed;
              if (!feed || !feed.id || feed.id === "0" || feed.id === prevId) return;
              WXU.emit(event, feed);
            });
          }, true);
        }
        if (typeof store.$subscribe === "function") {
          store.$subscribe(() => {
            const feed = store.flowCurrentFeed;
            if (feed && feed.id && (!__wx_channels_store__.profile || __wx_channels_store__.profile.id !== feed.id)) {
              applyLiteAppFeed(feed, false);
            }
          });
        }
      }
      const feedStore = pinia && pinia._s && pinia._s.get("feed");
      if (feedStore && typeof feedStore.$subscribe === "function") {
        feedStore.$subscribe(() => {
          const feed = feedStore.feed;
          if (feed && feed.id && (!__wx_channels_store__.profile || __wx_channels_store__.profile.id !== feed.id)) {
            applyLiteAppFeed(feed, false);
          }
        });
      }
    },

    findServices() {
      const match = Object.entries(this.require.m).find(([, factory]) => {
        const src = factory.toString();
        return src.includes("async finderPcFlow(") && src.includes("getRecommendTabsFromService(");
      });
      return match ? this.require(match[0]) : null;
    },

    async replayInitialFeed(services) {
      const api = services
        ? Object.values(services).find((v) => v && typeof v.finderGetCommentDetail === "function")
        : null;
      const props = this.app && this.app.config && this.app.config.globalProperties;
      const pinia = props && props.$pinia;
      const feedStore = pinia && pinia._s && pinia._s.get("feed");
      const homeStore = pinia && pinia._s && pinia._s.get("home");
      const existingFeed = (feedStore && feedStore.feed) || (homeStore && homeStore.flowCurrentFeed);
      if (existingFeed) {
        applyLiteAppFeed(existingFeed, true);
      }
      if (!api) return;
      try {
        await api.finderInit();
      } catch (_) {}
      if (existingFeed && existingFeed.id && existingFeed.objectNonceId) {
        try {
          await api.finderGetCommentDetail({
            needObject: 1,
            lastBuffer: "",
            scene: 146,
            direction: 2,
            identityScene: 2,
            pullScene: 6,
            objectid: existingFeed.id,
            objectNonceId: existingFeed.objectNonceId,
          });
        } catch (e) {
          console.warn("[LiteAppRuntime] replay finderGetCommentDetail failed:", e);
        }
      }
    },

    async start() {
      for (let attempt = 0; !this.capture(); attempt++) {
        if (attempt >= 300) {
          console.warn("[LiteAppRuntime] webpackChunk / Vue runtime not found after timeout");
          return;
        }
        await new Promise((r) => setTimeout(r, 100));
      }
      this.app = document.querySelector("#app").__vue_app__;
      if (document.readyState !== "loading") {
        WXE.emit(WXE.Events.DOMContentLoaded, { href: location.href });
      }
      const apiMods = this.findApiModules();
      apiMods.forEach((mod) => {
        if (mod && mod.exports) {
          this.installPrototypeHooks(mod.exports);
        }
      });
      this.installNavigation();
      const services = this.findServices();
      if (services) {
        WXU.emit(WXE.Events.APILoaded, services);
      }
      await this.replayInitialFeed(services);
    },
  };

  // ── 2. JSBridge 数据回调兜底 ───────────────────────────────────────────
  function toCamelObj(val) {
    if (Array.isArray(val)) return val.map(toCamelObj);
    if (!val || typeof val !== "object") return val;
    const out = {};
    for (const k of Object.keys(val)) {
      const camel = k.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
      out[camel] = toCamelObj(val[k]);
    }
    return out;
  }

  function dispatchBridgePayload(urlHint, respJson) {
    try {
      const raw = typeof respJson === "string" ? JSON.parse(respJson) : respJson;
      if (!raw || typeof raw !== "object") return;
      const data = raw.object || raw.objects ? raw : toCamelObj(raw);
      const hint = String(urlHint || "").toLowerCase();
      if (hint.includes("findergetcommentdetail") && data.object) {
        WXU.emit(WXE.Events.FeedProfileLoaded, data.object);
      } else if ((hint.includes("finderpcflow") || hint.includes("finderstream")) && Array.isArray(data.object)) {
        WXU.emit(WXE.Events.PCFlowLoaded, data.object);
      } else if (hint.includes("findergetrecommend") && Array.isArray(data.object)) {
        WXU.emit(WXE.Events.RecommendFeedsLoaded, data.object);
      } else if (hint.includes("finderuserpage") && Array.isArray(data.object)) {
        WXU.emit(WXE.Events.UserFeedsLoaded, {
          feeds: data.object,
          lastBuffer: data.lastBuffer || data.last_buffer || "",
          raw: data,
        });
      }
    } catch (_) {}
  }

  function wrapBridge(bridge) {
    if (!bridge || typeof bridge.invoke !== "function" || bridge.__wx_wrapped__) return false;
    const origInvoke = bridge.invoke.bind(bridge);
    bridge.invoke = function (apiName, params, callback) {
      const wrappedCb = typeof callback === "function"
        ? function (res) {
            try {
              const respJson = res && ((res.jsapi_resp && res.jsapi_resp.resp_json) || res.resp_json);
              if (respJson) {
                dispatchBridgePayload((params && (params.url || params.name)) || apiName, respJson);
              }
            } catch (_) {}
            return callback.apply(this, arguments);
          }
        : callback;
      return origInvoke(apiName, params, wrappedCb);
    };
    bridge.__wx_wrapped__ = true;
    return true;
  }

  function tryHookBridges() {
    wrapBridge(window.WeixinJSBridge);
    wrapBridge(window.IlinkJSBridge);
  }
  tryHookBridges();
  const bridgeTimer = setInterval(() => {
    tryHookBridges();
    if (
      (window.WeixinJSBridge && window.WeixinJSBridge.__wx_wrapped__) ||
      (window.IlinkJSBridge && window.IlinkJSBridge.__wx_wrapped__)
    ) {
      clearInterval(bridgeTimer);
    }
  }, 300);
  setTimeout(() => clearInterval(bridgeTimer), 15000);

  // ── 3. DOM 交互按钮挂载与增量监听 ──────────────────────────────────────
  function mountLiteAppButtons() {
    const opItems = document.querySelectorAll(".slides-item .click-box.op-item, .feed-interact .click-box.op-item, .click-box.op-item");
    if (opItems.length === 0) return false;
    let added = false;
    opItems.forEach((opItem) => {
      const parent = opItem.closest(".feed-interact") || opItem.parentElement;
      const scope = opItem.closest(".slides-item") || parent;
      if (parent && scope && !scope.querySelector(".download-icon")) {
        const btn = download_btn2();
        __wx_attach_download_dropdown_menu(btn);
        btn.onclick = __wx_download_btn_handler;
        parent.appendChild(btn);
        added = true;
      }
    });
    return added || opItems.length > 0;
  }

  function renderLiteAppSider() {
    if (document.querySelector(".wx-sider")) return;
    const $fixed = document.createElement("div");
    $fixed.className = "wx-sider";
    const $bg = document.createElement("div");
    $bg.className = "wx-sider-bg";
    const $tools = document.createElement("div");
    $tools.className = "wx-sider-tools";
    const $btn = document.createElement("div");
    $btn.className = "wx-sider-tools-btn";
    $btn.innerHTML = download_icon1;
    $btn.onclick = __wx_download_btn_handler;
    __wx_attach_download_dropdown_menu($btn);
    (document.body || document.documentElement).appendChild($fixed);
    $fixed.appendChild($bg);
    $fixed.appendChild($tools);
    $tools.appendChild($btn);
  }

  if (typeof insert_channels_style === "function") {
    insert_channels_style();
  }
  WXU.observe_node(".click-box.op-item", mountLiteAppButtons, renderLiteAppSider);

  const rootObserver = new MutationObserver(() => {
    mountLiteAppButtons();
  });
  const startRootObserve = () => {
    const root = document.getElementById("app") || document.body || document.documentElement;
    if (root) {
      rootObserver.observe(root, { childList: true, subtree: true });
    }
  };
  if (document.body || document.getElementById("app")) {
    startRootObserve();
  } else {
    WXU.onDOMContentLoaded(startRootObserve);
  }

  void webpackHook.start();
})();
