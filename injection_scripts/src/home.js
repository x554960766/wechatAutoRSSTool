/**
 * @file 首页推荐
 */

/**
 * 创建下载按钮并插入到指定父节点
 * @param {HTMLElement} $parent
 */
var __home_page_mounted = false;

function __wx_insert_download_btn($parent) {
  if (!$parent || $parent.querySelector(".download-icon")) {
    return;
  }
  var $btn = download_btn2();
  __wx_attach_download_dropdown_menu($btn);
  $btn.onclick = __wx_download_btn_handler;
  $parent.appendChild($btn);
}

/**
 * 在首页右侧添加悬浮下载按钮
 */
function __wx_render_sider_tools() {
  if (document.querySelector(".wx-sider-tools")) {
    return;
  }
  const $fixed_sider = document.createElement("div");
  $fixed_sider.className = "wx-sider";
  const $sider_bg = document.createElement("div");
  $sider_bg.className = "wx-sider-bg";
  const $tools = document.createElement("div");
  $tools.className = "wx-sider-tools";
  const $btn = document.createElement("div");
  $btn.className = "wx-sider-tools-btn";
  $btn.innerHTML = download_icon1;
  $btn.onclick = __wx_download_btn_handler;
  __wx_attach_download_dropdown_menu($btn);
  document.body.appendChild($fixed_sider);
  $fixed_sider.appendChild($sider_bg);
  $fixed_sider.appendChild($tools);
  $tools.appendChild($btn);
}

function __wx_resolve_interact_container($opItem) {
  if (!$opItem) return null;
  return $opItem.closest(".feed-interact") || $opItem.parentElement;
}

async function __wx_insert_download_btn_to_home_page() {
  var $operations_parents = [];
  var $op_items = Array.from(
    document.querySelectorAll(".slides-item .click-box.op-item"),
  );
  for (let i = 0; i < $op_items.length; i += 1) {
    var $op_item = $op_items[i];
    var $slide = $op_item.closest(".slides-item");
    if ($slide && $slide.querySelector(".download-icon")) {
      continue;
    }
    var $parent = __wx_resolve_interact_container($op_item);
    if ($parent && !$operations_parents.includes($parent)) {
      $operations_parents.push($parent);
    }
  }
  for (let i = 0; i < $operations_parents.length; i += 1) {
    __wx_insert_download_btn($operations_parents[i]);
  }
}

WXU.observe_node(
  ".slides-item",
  function () {
    __home_page_mounted = true;
    __wx_insert_download_btn_to_home_page();
  },
  function () {
    if (!__home_page_mounted) {
      __wx_render_sider_tools();
    }
  },
);

WXU.observe_node(".slides-scroll", function ($scroll_view) {
  var observer = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      if (mutation.type !== "childList") return;
      mutation.addedNodes.forEach(function (node) {
        if (node.nodeType !== 1 || !node.matches) return;
        if (node.matches(".click-box.op-item")) {
          handleOpItem(node);
          return;
        }
        if (node.querySelectorAll) {
          var $opItems = node.querySelectorAll(".click-box.op-item");
          if ($opItems.length > 0) {
            $opItems.forEach(function ($opItem) {
              handleOpItem($opItem);
            });
          }
        }
      });
    });
    function handleOpItem($opItem) {
      var $slide = $opItem.closest(".slides-item");
      if (!$slide) {
        return;
      }
      if ($slide.querySelector(".download-icon")) {
        return;
      }
      var $parent = __wx_resolve_interact_container($opItem);
      if ($parent) {
        __wx_insert_download_btn($parent);
      }
    }
  });
  observer.observe($scroll_view, { childList: true, subtree: true });
});

var error_tip_timer = setTimeout(() => {
  WXU.error({ msg: "没有获取到视频详情", alert: 0 });
}, 5000);

function clear_error_timer() {
  if (error_tip_timer) {
    clearTimeout(error_tip_timer);
    error_tip_timer = null;
  }
}

function __wx_apply_home_feed(feed, onlyWhenEmpty = false) {
  if (!feed) {
    return;
  }
  if (onlyWhenEmpty && __wx_channels_store__.profile) {
    clear_error_timer();
    return;
  }
  WXU.set_cur_video();
  WXU.set_feed(feed);
  WXU.emit(WXE.Events.Feed, feed);
  clear_error_timer();
}

WXU.onFetchFeedProfile((feed) => {
  __wx_apply_home_feed(feed, false);
});

WXU.onPCFlowLoaded((feeds) => {
  if (feeds && feeds.length) {
    __wx_apply_home_feed(feeds[0], true);
  }
});

WXU.onPreloadFeeds((feeds) => {
  if (feeds && feeds.length) {
    __wx_apply_home_feed(feeds[0], true);
  }
});

WXU.onGotoNextFeed((feed) => {
  __wx_apply_home_feed(feed, false);
});

WXU.onGotoPrevFeed((feed) => {
  __wx_apply_home_feed(feed, false);
});

WXU.onHomeFeedChanged((feed) => {
  __wx_apply_home_feed(feed, false);
});

WXU.onDOMContentLoaded(function () {
  insert_channels_style();
});
