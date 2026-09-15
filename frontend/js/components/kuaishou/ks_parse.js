const KsParsePage = {
    currentItem: null,
    pollTimer: null,

    render() {
        return `
            <div class="page-header">
                <h2 class="page-title">解析链接</h2>
                <p class="page-description">粘贴单个快手作品链接，先解析预览作品信息（标题、作者、封面等），再一键下载高清无水印视频或图集</p>
            </div>

            <!-- 输入卡片 -->
            <div class="card" style="margin-bottom: var(--spacing-lg);">
                <div class="form-group" style="margin-bottom: var(--spacing-md);">
                    <label class="form-label" style="font-weight: 600;">快手作品链接</label>
                    <div style="display: flex; gap: var(--spacing-md);">
                        <input type="text" id="ks-url-input" class="form-input" 
                               placeholder="请粘贴快手作品链接 (如 https://v.kuaishou.com/... 或 https://live.kuaishou.com/u/...)" 
                               style="flex: 1;" onkeydown="if(event.key==='Enter') KsParsePage.parseUrl()">
                        <button class="btn btn-primary" onclick="KsParsePage.parseUrl()" id="ks-parse-btn" style="min-width: 110px;">
                            🔍 解析链接
                        </button>
                    </div>
                    <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px;">
                        <span style="display: inline-flex; align-items: center; gap: 4px; background: rgba(59, 130, 246, 0.1); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.25); padding: 4px 10px; border-radius: 16px; font-size: 0.78rem; font-weight: 500;">
                            📹 短视频 (MP4)
                        </span>
                        <span style="display: inline-flex; align-items: center; gap: 4px; background: rgba(16, 185, 129, 0.1); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.25); padding: 4px 10px; border-radius: 16px; font-size: 0.78rem; font-weight: 500;">
                            🖼️ 图集 (JPG)
                        </span>
                        <span style="display: inline-flex; align-items: center; gap: 4px; background: rgba(139, 92, 246, 0.1); color: #8b5cf6; border: 1px solid rgba(139, 92, 246, 0.25); padding: 4px 10px; border-radius: 16px; font-size: 0.78rem; font-weight: 500;">
                            🔗 移动端分享短链 / 电脑网页端
                        </span>
                    </div>
                </div>
            </div>

            <!-- 作品预览卡片（参考抖音解析样式） -->
            <div id="ks-media-preview-card" style="display: none; padding: 20px; background: var(--bg-card); border-radius: var(--radius-md); margin-bottom: var(--spacing-lg); border: 1px solid var(--border-color); box-shadow: var(--shadow-sm);">
                <div style="display: flex; gap: 20px; flex-direction: row; align-items: flex-start;" class="ks-preview-layout">
                    <!-- 封面大图与类型徽章 -->
                    <div style="position: relative; width: 150px; min-width: 150px; height: 200px; border-radius: var(--radius-sm); overflow: hidden; background: var(--bg-input); flex-shrink: 0; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                        <img id="ks-preview-cover" src="" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22150%22 height=%22200%22%3E%3Crect fill=%22%23333%22 width=%22150%22 height=%22200%22/%3E%3Ctext fill=%22%23fff%22 font-size=%2216%22 dy=%22.3em%22 x=%2250%25%22 y=%2250%25%22 text-anchor=%22middle%22%3E无封面%3C/text%3E%3C/svg%3E'" />
                        <span id="ks-preview-badge" style="position: absolute; top: 8px; left: 8px; font-size: 0.75rem; padding: 3px 8px; border-radius: 4px; font-weight: 600; background: rgba(0,0,0,0.7); color: #fff; backdrop-filter: blur(4px);"></span>
                    </div>

                    <!-- 作品元数据 -->
                    <div style="flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: space-between; min-height: 200px;">
                        <div>
                            <!-- 作者信息 -->
                            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                                <img id="ks-preview-avatar" src="" style="width: 38px; height: 38px; border-radius: 50%; object-fit: cover; border: 1px solid var(--border-color); background: var(--bg-input);" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2238%22 height=%2238%22%3E%3Ccircle fill=%22%23ccc%22 cx=%2219%22 cy=%2219%22 r=%2219%22/%3E%3C/svg%3E'" />
                                <div style="min-width: 0;">
                                    <div id="ks-preview-author" style="font-weight: 600; font-size: 1.05rem; color: var(--text-primary); line-height: 1.2;"></div>
                                    <div id="ks-preview-id" style="font-size: 0.78rem; color: var(--text-muted); margin-top: 2px;"></div>
                                </div>
                            </div>

                            <!-- 标题/文案 -->
                            <div id="ks-preview-title" style="font-size: 0.95rem; color: var(--text-primary); line-height: 1.5; margin: 12px 0; word-break: break-all; max-height: 72px; overflow-y: auto;"></div>

                            <!-- 统计信息/资源状态 -->
                            <div style="display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap;">
                                <span id="ks-preview-count-tag" style="background: rgba(102, 126, 234, 0.1); color: var(--primary); padding: 3px 10px; border-radius: 4px; font-size: 0.8rem; font-weight: 500;"></span>
                                <span style="background: rgba(16, 185, 129, 0.1); color: #10b981; padding: 3px 10px; border-radius: 4px; font-size: 0.8rem; font-weight: 500;">最高画质直链</span>
                            </div>
                        </div>

                        <!-- 操作按钮 -->
                        <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap; border-top: 1px solid var(--border-color); padding-top: 14px;">
                            <button class="btn btn-primary" onclick="KsParsePage.startDownload()" id="ks-download-btn" style="display: inline-flex; align-items: center; gap: 6px; padding: 7px 18px;">
                                📥 立即下载
                            </button>
                            <button class="btn btn-secondary btn-sm" onclick="KsParsePage.openOriginalUrl()" id="ks-open-web-btn" style="padding: 7px 14px; font-size: 0.85rem;">
                                🔗 浏览器打开原作品
                            </button>
                            <button class="btn btn-secondary btn-sm" onclick="KsParsePage.clearPreview()" style="padding: 7px 14px; font-size: 0.85rem; color: var(--text-muted);">
                                清空
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 下载结果与操作提示 -->
            <div id="ks-download-result" style="display: none; padding: 16px 20px; background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: var(--radius-md); margin-bottom: var(--spacing-lg);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span style="font-size: 1.5rem;">🎉</span>
                        <div>
                            <div id="ks-result-title" style="font-weight: 600; color: var(--text-primary); font-size: 0.95rem;">下载完成</div>
                            <div id="ks-result-info" style="font-size: 0.82rem; color: var(--text-muted); margin-top: 2px;"></div>
                        </div>
                    </div>
                    <div style="display: flex; gap: 8px;">
                        <button class="btn btn-secondary btn-sm" onclick="KsParsePage.openFolder()" style="display: inline-flex; align-items: center; gap: 4px;">
                            📂 打开所在目录
                        </button>
                        <button class="btn btn-primary btn-sm" id="ks-open-file-btn" onclick="KsParsePage.openFile()" style="display: none; align-items: center; gap: 4px;">
                            📄 查看文件
                        </button>
                    </div>
                </div>
            </div>

            <!-- 下载进度卡片 -->
            <div class="card" id="ks-download-status" style="display: none;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--spacing-md);">
                    <h3 style="margin: 0; font-size: 1.05rem; font-weight: 600;">下载进度</h3>
                </div>
                <div style="display: flex; align-items: center; gap: var(--spacing-md); margin-bottom: var(--spacing-md); flex-wrap: wrap;">
                    <div style="flex: 1; min-width: 200px; height: 8px; background: var(--bg-input); border-radius: 4px; overflow: hidden;">
                        <div id="ks-progress-bar" style="width: 0%; height: 100%; background: var(--gradient-primary); transition: width 0.3s ease;"></div>
                    </div>
                    <span id="ks-progress-text" style="font-variant-numeric: tabular-nums; font-weight: 600; min-width: 45px;">0%</span>
                    <button class="btn btn-secondary btn-sm" onclick="KsParsePage.cancelDownload()" id="ks-cancel-btn" style="padding: 4px 12px; font-size: 0.85rem; height: 32px; display: none; align-items: center; gap: 4px;">
                        取消下载
                    </button>
                </div>
                <div id="ks-log-container" style="background: var(--bg-body); border-radius: var(--radius-sm); padding: var(--spacing-sm); height: 160px; overflow-y: auto; font-family: monospace; font-size: 0.82rem; color: var(--text-muted);">
                </div>
            </div>
        `;
    },

    lastSavedPath: "",

    async init() {
        try {
            const data = await API.kuaishou.progress();
            if (data && data.status === 'running') {
                document.getElementById('ks-download-status').style.display = 'block';
                this.updateProgressUI(data);
                document.getElementById('ks-cancel-btn').style.display = 'flex';
                this.startProgressPolling();
            }
        } catch (e) {
            console.error('检查下载进度失败:', e);
        }
    },

    onShow() {
        this.init();
    },

    async parseUrl() {
        const urlInput = document.getElementById('ks-url-input');
        const url = (urlInput ? urlInput.value : '').trim();
        if (!url) {
            Toast.show('请先粘贴快手作品链接', 'warning');
            return;
        }

        const parseBtn = document.getElementById('ks-parse-btn');
        parseBtn.disabled = true;
        parseBtn.textContent = '解析中...';
        document.getElementById('ks-download-result').style.display = 'none';

        try {
            const res = await API.kuaishou.parseSingle(url);
            if (res.error) throw new Error(res.error);
            const item = res.item || {};
            this.currentItem = item;
            this.renderPreview(item);
            Toast.show('解析成功！请确认作品信息后下载', 'success');
        } catch (err) {
            Toast.show(err.message || '解析链接失败，请检查链接有效性', 'error');
            this.clearPreview();
        } finally {
            parseBtn.disabled = false;
            parseBtn.textContent = '🔍 解析链接';
        }
    },

    renderPreview(item) {
        const card = document.getElementById('ks-media-preview-card');
        if (!card) return;

        const coverImg = document.getElementById('ks-preview-cover');
        const badge = document.getElementById('ks-preview-badge');
        const avatarImg = document.getElementById('ks-preview-avatar');
        const authorEl = document.getElementById('ks-preview-author');
        const idEl = document.getElementById('ks-preview-id');
        const titleEl = document.getElementById('ks-preview-title');
        const countTag = document.getElementById('ks-preview-count-tag');

        coverImg.src = item.cover || '';
        const isImage = item.type === 'image';
        badge.textContent = isImage ? `🖼️ 图集 (${item.count || (item.urls ? item.urls.length : 0)}张)` : '📹 视频';
        badge.style.background = isImage ? 'rgba(16, 185, 129, 0.85)' : 'rgba(59, 130, 246, 0.85)';

        avatarImg.src = item.avatar || '';
        authorEl.textContent = item.author || item.nickname || '快手用户';
        idEl.textContent = `作品ID: ${item.photo_id || '未知'}`;
        titleEl.textContent = item.title || '（该作品无文案标题）';

        if (isImage) {
            countTag.textContent = `共 ${item.count || item.urls.length} 张高清图片`;
        } else {
            countTag.textContent = '高清 MP4 视频直链';
        }

        card.style.display = 'block';
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    clearPreview() {
        this.currentItem = null;
        const card = document.getElementById('ks-media-preview-card');
        if (card) card.style.display = 'none';
        const resultBox = document.getElementById('ks-download-result');
        if (resultBox) resultBox.style.display = 'none';
    },

    openOriginalUrl() {
        if (this.currentItem && this.currentItem.raw_url) {
            window.open(this.currentItem.raw_url, '_blank');
        }
    },

    async startDownload() {
        let payload;
        if (this.currentItem && this.currentItem.urls && this.currentItem.urls.length > 0) {
            payload = this.currentItem;
        } else {
            const urlInput = document.getElementById('ks-url-input');
            const url = (urlInput ? urlInput.value : '').trim();
            if (!url) {
                Toast.show('请先粘贴或解析快手作品链接', 'warning');
                return;
            }
            payload = url;
        }

        const btn = document.getElementById('ks-download-btn') || document.getElementById('ks-parse-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = '下载中...';
        }
        document.getElementById('ks-download-result').style.display = 'none';

        try {
            const res = await API.kuaishou.downloadSingle(payload);
            if (res.error) throw new Error(res.error);

            const title = res.title || (res.data ? res.data.title : '快手作品');
            const size = res.data && res.data.size_bytes ? `${(res.data.size_bytes / (1024 * 1024)).toFixed(2)} MB` : '';
            this.lastSavedPath = res.data ? (res.data.path || '') : '';

            // 显示成功卡片
            const resultBox = document.getElementById('ks-download-result');
            if (resultBox) {
                document.getElementById('ks-result-title').textContent = `✅ 下载完成: ${title}`;
                document.getElementById('ks-result-info').textContent = size ? `文件大小: ${size} | 已保存至快手下载目录` : '已保存至快手下载目录';
                const openFileBtn = document.getElementById('ks-open-file-btn');
                if (openFileBtn && this.lastSavedPath) {
                    openFileBtn.style.display = 'inline-flex';
                }
                resultBox.style.display = 'block';
            }

            Toast.show(`下载成功: ${title}`, 'success');
        } catch (err) {
            Toast.show(err.message || '下载失败', 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '📥 立即下载';
            }
        }
    },

    async openFolder() {
        try {
            await API.kuaishou.openFolder();
        } catch (e) {
            Toast.show(e.message || '打开目录失败', 'error');
        }
    },

    async openFile() {
        if (!this.lastSavedPath) {
            this.openFolder();
            return;
        }
        try {
            await API.kuaishou.openFile(this.lastSavedPath);
        } catch (e) {
            this.openFolder();
        }
    },

    updateProgressUI(data) {
        const logContainer = document.getElementById('ks-log-container');
        if (logContainer && data.logs) {
            logContainer.innerHTML = data.logs.map(l => `<div style="margin-bottom: 4px;">${l}</div>`).join('');
            logContainer.scrollTop = logContainer.scrollHeight;
        }

        let pct = 0;
        let processed = (data.downloaded_count || 0) + (data.failed_count || 0);
        if (data.total > 0) {
            pct = Math.floor((processed / data.total) * 100);
        } else if (data.status === 'completed') {
            pct = 100;
        }
        const bar = document.getElementById('ks-progress-bar');
        if (bar) bar.style.width = pct + '%';

        const progressText = document.getElementById('ks-progress-text');
        if (progressText) {
            if (data.total > 1) {
                progressText.textContent = `${data.downloaded_count || 0}/${data.total}`;
            } else {
                progressText.textContent = pct + '%';
            }
        }
    },

    startProgressPolling() {
        document.getElementById('ks-download-status').style.display = 'block';
        const cancelBtn = document.getElementById('ks-cancel-btn');

        if (this.pollTimer) clearInterval(this.pollTimer);

        this.pollTimer = setInterval(async () => {
            try {
                const data = await API.kuaishou.progress();
                this.updateProgressUI(data);

                if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled' || data.status === 'idle') {
                    clearInterval(this.pollTimer);
                    this.pollTimer = null;
                    if (cancelBtn) cancelBtn.style.display = 'none';

                    if (data.status === 'completed') {
                        Toast.show('批量下载完成！', 'success');
                    } else if (data.status === 'cancelled') {
                        Toast.show('下载已取消', 'info');
                    } else if (data.status === 'failed') {
                        Toast.show('下载失败', 'error');
                    }
                } else {
                    if (cancelBtn) cancelBtn.style.display = 'flex';
                }
            } catch (e) {}
        }, 1000);
    },

    async cancelDownload() {
        const cancelBtn = document.getElementById('ks-cancel-btn');
        if (cancelBtn) cancelBtn.style.display = 'none';

        try {
            const res = await API.kuaishou.cancelDownload();
            Toast.show(res.message, 'info');
        } catch (err) {
            Toast.show(err.message, 'error');
        }
    },

    destroy() {
        if (this.pollTimer) clearInterval(this.pollTimer);
    }
};
