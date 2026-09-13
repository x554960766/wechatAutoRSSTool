/**
 * 快手下载历史组件 - 按博主归类展示，支持展开/折叠、搜索博主、定位打开与单项删除
 */
const KsDownloadsPage = {
    history: [],
    loading: false,
    expandedAuthor: null,  // 当前展开的博主
    searchKeyword: '',     // 搜索关键词

    render() {
        return `
            <div class="page-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                    <div>
                        <h2 class="page-title">下载历史</h2>
                        <p class="page-description">查看已下载的快手视频和图集历史记录，支持按博主归类管理与快速定位。</p>
                    </div>
                    <div class="btn-group" style="display: flex; gap: 8px;">
                        <button class="btn btn-secondary" onclick="KsDownloadsPage.openFolder()">
                            <svg viewBox="0 0 24 24" fill="none" style="width: 16px; height: 16px; margin-right: 6px;">
                                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                            打开下载根目录
                        </button>
                        <button class="btn btn-secondary" onclick="KsDownloadsPage.refresh()">
                            <svg viewBox="0 0 24 24" fill="none" style="width: 16px; height: 16px; margin-right: 6px;">
                                <polyline points="23 4 23 10 17 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                            刷新
                        </button>
                        <button class="btn btn-error" onclick="KsDownloadsPage.clearHistory()">
                            <svg viewBox="0 0 24 24" fill="none" style="width: 16px; height: 16px; margin-right: 6px; color: var(--error);">
                                <polyline points="3 6 5 6 21 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                            </svg>
                            清空历史
                        </button>
                    </div>
                </div>
            </div>

            <div id="ks-downloads-container">
                <!-- 统计栏与搜索 -->
                <div class="card" style="margin-bottom: var(--spacing-lg);">
                    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: var(--spacing-md);">
                        <div style="display: flex; gap: var(--spacing-2xl); flex-wrap: wrap;">
                            <div>
                                <span style="color: var(--text-muted); font-size: 0.85rem; display: block; margin-bottom: 4px;">已下载作品</span>
                                <strong style="font-size: 1.8rem; color: #ff5000;" id="ks-dl-stat-count">0 个</strong>
                            </div>
                            <div style="border-left: 1px solid var(--border-color); padding-left: var(--spacing-2xl);">
                                <span style="color: var(--text-muted); font-size: 0.85rem; display: block; margin-bottom: 4px;">归属博主</span>
                                <strong style="font-size: 1.8rem; color: var(--text-primary);" id="ks-dl-stat-authors">0 个</strong>
                            </div>
                            <div style="border-left: 1px solid var(--border-color); padding-left: var(--spacing-2xl);">
                                <span style="color: var(--text-muted); font-size: 0.85rem; display: block; margin-bottom: 4px;">视频文件</span>
                                <strong style="font-size: 1.8rem; color: var(--text-primary);" id="ks-dl-stat-videos">0 个</strong>
                            </div>
                            <div style="border-left: 1px solid var(--border-color); padding-left: var(--spacing-2xl);">
                                <span style="color: var(--text-muted); font-size: 0.85rem; display: block; margin-bottom: 4px;">图集文件夹</span>
                                <strong style="font-size: 1.8rem; color: var(--text-primary);" id="ks-dl-stat-images">0 个</strong>
                            </div>
                        </div>

                        <div style="display: flex; gap: 8px; align-items: center;">
                            <div style="position: relative;">
                                <svg viewBox="0 0 24 24" fill="none" style="width: 16px; height: 16px; position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--text-muted);">
                                    <circle cx="11" cy="11" r="8" stroke="currentColor" stroke-width="2"/>
                                    <line x1="21" y1="21" x2="16.65" y2="16.65" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                                </svg>
                                <input type="text" id="ks-dl-search" class="form-input" placeholder="搜索博主..." oninput="KsDownloadsPage.onSearch(this.value)" style="padding-left: 32px; width: 190px; height: 36px; font-size: 0.85rem; border-radius: 8px;">
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 加载中状态 -->
                <div id="ks-downloads-loading" style="text-align: center; padding: var(--spacing-2xl);">
                    <div class="spinner"></div>
                    <p style="margin-top: var(--spacing-md); color: var(--text-muted);">加载中...</p>
                </div>

                <!-- 空数据状态 -->
                <div id="ks-downloads-empty" style="display: none; text-align: center; padding: var(--spacing-2xl);">
                    <div style="width: 64px; height: 64px; margin: 0 auto var(--spacing-md); background: rgba(255, 80, 0, 0.08); border-radius: 20px; display: flex; align-items: center; justify-content: center;">
                        <svg viewBox="0 0 24 24" fill="none" style="width: 32px; height: 32px; color: #ff5000;">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                            <polyline points="7 10 12 15 17 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                            <line x1="12" y1="15" x2="12" y2="3" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                    </div>
                    <p style="font-size: 1.1rem; margin-bottom: 8px; color: var(--text-primary);">暂无下载历史记录</p>
                    <p style="color: var(--text-muted);">您可以在「用户主页」或「解析链接」中下载快手视频与图集</p>
                </div>

                <!-- 按博主归类的分组卡片容器 -->
                <div id="ks-downloads-groups" style="display: none; flex-direction: column; gap: var(--spacing-md);"></div>
            </div>
        `;
    },

    async init() {
        await this.loadHistory();
    },

    onShow() {
        this.loadHistory();
    },

    async loadHistory() {
        this.loading = true;
        this.showLoading();
        try {
            const data = await API.kuaishou.getHistory();
            this.history = data || [];
            this.renderHistory();
        } catch (err) {
            Toast.show('加载历史记录失败: ' + err.message, 'error');
            this.showEmpty();
        } finally {
            this.loading = false;
            this.hideLoading();
        }
    },

    /**
     * 从记录中提取博主名称（兼容旧记录从 path 中推导）
     */
    extractAuthor(item) {
        if (item.author && item.author !== '未知博主' && item.author !== '快手用户') {
            return item.author;
        }
        const path = item.path || '';
        const marker = 'kuaishou_downloads/';
        const idx = path.indexOf(marker);
        if (idx >= 0) {
            const rest = path.substring(idx + marker.length);
            const parts = rest.split('/');
            if (parts.length > 1 && parts[0]) {
                return parts[0];
            }
        }
        return item.author || '快手用户';
    },

    /**
     * 按博主归类数据
     */
    groupByAuthor() {
        const groups = {};
        this.history.forEach((item, originalIndex) => {
            const author = this.extractAuthor(item);
            if (!groups[author]) {
                groups[author] = {
                    name: author,
                    items: [],
                    lastTime: '',
                    totalBytes: 0,
                    videosCount: 0,
                    imagesCount: 0,
                };
            }
            // 附带原始 index 便于删除
            groups[author].items.push({ ...item, _origIndex: originalIndex });
            if (item.type === '视频') groups[author].videosCount++;
            else groups[author].imagesCount++;

            if (!groups[author].lastTime) {
                groups[author].lastTime = item.time;
            }
        });
        return groups;
    },

    getRelativeTime(timeStr) {
        if (!timeStr) return '';
        try {
            const date = new Date(timeStr.replace(/-/g, '/'));
            const now = new Date();
            const diff = now - date;
            const minutes = Math.floor(diff / 60000);
            const hours = Math.floor(diff / 3600000);
            const days = Math.floor(diff / 86400000);

            if (minutes < 1) return '刚刚';
            if (minutes < 60) return `${minutes} 分钟前`;
            if (hours < 24) return `${hours} 小时前`;
            if (days < 7) return `${days} 天前`;
            return timeStr.split(' ')[0];
        } catch (e) {
            return timeStr;
        }
    },

    renderHistory() {
        const empty = document.getElementById('ks-downloads-empty');
        const groupsContainer = document.getElementById('ks-downloads-groups');
        if (!empty || !groupsContainer) return;

        const total = this.history.length;
        const videos = this.history.filter(item => item.type === '视频').length;
        const images = this.history.filter(item => item.type === '图文').length;
        const groups = this.groupByAuthor();
        const authorCount = Object.keys(groups).length;

        document.getElementById('ks-dl-stat-count').textContent = total + ' 个';
        document.getElementById('ks-dl-stat-authors').textContent = authorCount + ' 个';
        document.getElementById('ks-dl-stat-videos').textContent = videos + ' 个';
        document.getElementById('ks-dl-stat-images').textContent = images + ' 个';

        if (total === 0) {
            empty.style.display = 'block';
            groupsContainer.style.display = 'none';
            return;
        }

        empty.style.display = 'none';
        groupsContainer.style.display = 'flex';

        // 搜索过滤
        const keyword = this.searchKeyword.toLowerCase().trim();
        const filteredGroups = {};
        Object.keys(groups).forEach(key => {
            if (!keyword || key.toLowerCase().includes(keyword)) {
                filteredGroups[key] = groups[key];
            }
        });

        // 按最新下载时间倒序
        const sortedKeys = Object.keys(filteredGroups).sort((a, b) => {
            const timeA = filteredGroups[a].lastTime || '';
            const timeB = filteredGroups[b].lastTime || '';
            return timeB.localeCompare(timeA);
        });

        if (sortedKeys.length === 0) {
            groupsContainer.innerHTML = `
                <div class="card" style="text-align: center; padding: var(--spacing-2xl); color: var(--text-muted);">
                    未找到匹配「${this._esc(keyword)}」的博主历史记录
                </div>
            `;
            return;
        }

        // 如果用户尚未展开任何博主，默认展开第一个
        if (!this.expandedAuthor && sortedKeys.length > 0) {
            this.expandedAuthor = sortedKeys[0];
        }

        groupsContainer.innerHTML = sortedKeys.map(key => {
            const group = filteredGroups[key];
            const isExpanded = this.expandedAuthor === key;
            const itemCount = group.items.length;
            const relTime = this.getRelativeTime(group.lastTime);
            const initial = (group.name || '?').charAt(0);

            return `
                <div class="card ks-dl-author-card" style="overflow: hidden; padding: 16px 20px; transition: box-shadow 0.2s ease, transform 0.2s ease; margin-bottom: 12px; border: 1px solid var(--border-color); border-radius: 12px; background: var(--bg-card);"
                     onmouseenter="this.style.boxShadow='0 4px 20px rgba(0,0,0,0.06)'" 
                     onmouseleave="this.style.boxShadow=''">
                    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
                        
                        <!-- 博主头像与基础信息 -->
                        <div style="display: flex; align-items: center; gap: var(--spacing-md); min-width: 0;">
                            <div style="width: 46px; height: 46px; border-radius: 50%; background: #ff5000; color: white; display: flex; align-items: center; justify-content: center; font-size: 1.2rem; font-weight: 700; flex-shrink: 0; box-shadow: 0 2px 8px rgba(255,80,0,0.25);">
                                ${initial}
                            </div>

                            <div style="min-width: 0;">
                                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap;">
                                    <span style="font-weight: 700; font-size: 1.05rem; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                                        ${this._esc(group.name)}
                                    </span>
                                    <span style="font-size: 0.76rem; color: var(--text-muted); background: var(--bg-tertiary); padding: 2px 8px; border-radius: 12px;">
                                        已下载 ${itemCount} 项
                                    </span>
                                </div>
                                <div style="display: flex; align-items: center; gap: 10px; font-size: 0.82rem;">
                                    <span style="cursor: pointer; color: #ff5000; font-weight: 500;" onclick="KsDownloadsPage.toggleExpand('${this._esc(key).replace(/'/g, "\\'")}')">
                                        ${isExpanded ? '收起详情列表 ▲' : `查看该博主 ${itemCount} 个下载作品 ▼`}
                                    </span>
                                    <span style="color: var(--text-muted); opacity: 0.6;">|</span>
                                    <span style="cursor: pointer; color: var(--text-muted);" onclick="Router.navigate('ks_accounts')">
                                        前往博主管理 ➜
                                    </span>
                                </div>
                            </div>
                        </div>

                        <!-- 时间与快捷操作 -->
                        <div style="display: flex; align-items: center; gap: 14px;">
                            <span style="font-size: 0.8rem; color: var(--text-muted);">${relTime}</span>
                            <div style="display: flex; gap: 8px;">
                                <button class="btn btn-secondary btn-sm" onclick="KsDownloadsPage.openAuthorFolder('${this._esc(key).replace(/'/g, "\\'")}')" style="padding: 6px 12px; font-size: 0.82rem; white-space: nowrap;">
                                    📂 打开博主文件夹
                                </button>
                                <button class="btn btn-secondary btn-sm" onclick="KsDownloadsPage.toggleExpand('${this._esc(key).replace(/'/g, "\\'")}')" style="padding: 6px 12px; font-size: 0.82rem; white-space: nowrap;">
                                    ${isExpanded ? '收起' : '展开'}
                                </button>
                            </div>
                        </div>
                    </div>

                    <!-- 展开的博主作品明细列表 -->
                    ${isExpanded ? this.renderExpandedItems(group) : ''}
                </div>
            `;
        }).join('');
    },

    renderExpandedItems(group) {
        return `
            <div style="margin-top: 16px; border-top: 1px solid var(--border-color); padding-top: 14px; overflow-x: auto;">
                <table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 0.88rem;">
                    <thead>
                        <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted); font-size: 0.82rem;">
                            <th style="padding: 8px 12px; font-weight: 600;">作品标题</th>
                            <th style="padding: 8px 12px; font-weight: 600; width: 80px;">类型</th>
                            <th style="padding: 8px 12px; font-weight: 600; width: 90px;">文件大小</th>
                            <th style="padding: 8px 12px; font-weight: 600; width: 160px;">下载时间</th>
                            <th style="padding: 8px 12px; font-weight: 600; width: 170px; text-align: right;">操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${group.items.map(item => {
                            const typeStyle = item.type === '视频'
                                ? 'background: rgba(255, 80, 0, 0.1); color: #ff5000; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 500;'
                                : 'background: rgba(76, 175, 80, 0.1); color: #4caf50; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 500;';

                            return `
                                <tr style="border-bottom: 1px solid var(--border-color); vertical-align: middle; transition: background 0.2s;" onmouseenter="this.style.background='var(--bg-tertiary)'" onmouseleave="this.style.background='transparent'">
                                    <td style="padding: 10px 12px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                                        <span style="font-weight: 500; color: var(--text-primary);" title="${this._esc(item.title)}">
                                            ${this._esc(item.title)}
                                        </span>
                                    </td>
                                    <td style="padding: 10px 12px;"><span style="${typeStyle}">${item.type}</span></td>
                                    <td style="padding: 10px 12px; color: var(--text-muted); font-size: 0.85rem;">${item.size || '未知'}</td>
                                    <td style="padding: 10px 12px; color: var(--text-muted); font-size: 0.85rem;">${item.time}</td>
                                    <td style="padding: 10px 12px; text-align: right; white-space: nowrap;">
                                        <button class="btn btn-secondary btn-sm" onclick="KsDownloadsPage.openFile('${item._origIndex}')" style="padding: 3px 8px; font-size: 0.78rem; margin-right: 4px;">播放/打开</button>
                                        <button class="btn btn-secondary btn-sm" onclick="KsDownloadsPage.openParent('${item._origIndex}')" style="padding: 3px 8px; font-size: 0.78rem; margin-right: 4px;">📂 定位</button>
                                        <button class="btn btn-danger btn-sm" onclick="KsDownloadsPage.deleteItem(${item._origIndex}, '${this._esc(item.title).replace(/'/g, "\\'")}')" style="padding: 3px 8px; font-size: 0.78rem;">删除</button>
                                    </td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            </div>
        `;
    },

    toggleExpand(authorKey) {
        if (this.expandedAuthor === authorKey) {
            this.expandedAuthor = null;
        } else {
            this.expandedAuthor = authorKey;
        }
        this.renderHistory();
    },

    onSearch(value) {
        this.searchKeyword = value;
        this.renderHistory();
    },

    async openFolder() {
        try {
            await API.kuaishou.openFolder();
            Toast.show('已打开快手下载根目录', 'success');
        } catch (err) {
            Toast.show('打开失败: ' + err.message, 'error');
        }
    },

    async openAuthorFolder(authorName) {
        try {
            await API.kuaishou.openAuthorFolder(authorName);
            Toast.show(`已打开博主「${authorName}」的目录`, 'success');
        } catch (err) {
            Toast.show('打开失败: ' + err.message, 'error');
        }
    },

    async openFile(origIndex) {
        const item = this.history[origIndex];
        if (!item || !item.path) {
            Toast.show('无效的下载记录', 'error');
            return;
        }
        try {
            await API.kuaishou.openFile(item.path);
            Toast.show('正在打开文件...', 'info');
        } catch (err) {
            Toast.show(err.message, 'error');
        }
    },

    async openParent(origIndex) {
        const item = this.history[origIndex];
        if (!item || !item.path) {
            Toast.show('无效的下载记录', 'error');
            return;
        }
        try {
            await API.kuaishou.openParent(item.path);
            Toast.show('正在定位文件...', 'info');
        } catch (err) {
            Toast.show(err.message, 'error');
        }
    },

    deleteItem(origIndex, title) {
        Modal.confirm('删除下载记录', `确定要从历史记录中移除「${title}」吗？（本地已下载文件不会被删除）`, async () => {
            const item = this.history[origIndex];
            try {
                await API.kuaishou.deleteHistoryItem(origIndex, item ? item.path : null);
                Toast.show('已删除记录', 'success');
                await this.loadHistory();
            } catch (err) {
                Toast.show('删除失败: ' + err.message, 'error');
            }
        });
    },

    async clearHistory() {
        Modal.confirm('清空下载历史', '您确定要清空全部快手下载历史记录吗？（注意：这不会删除您本地已下载的视频和图片文件）', async () => {
            try {
                await API.kuaishou.clearHistory();
                Toast.show('历史记录已清空', 'success');
                await this.refresh();
            } catch (err) {
                Toast.show('清空失败: ' + err.message, 'error');
            }
        });
    },

    async refresh() {
        await this.loadHistory();
    },

    showLoading() {
        const loading = document.getElementById('ks-downloads-loading');
        if (loading) loading.style.display = 'block';
        const empty = document.getElementById('ks-downloads-empty');
        if (empty) empty.style.display = 'none';
        const groups = document.getElementById('ks-downloads-groups');
        if (groups) groups.style.display = 'none';
    },

    hideLoading() {
        const loading = document.getElementById('ks-downloads-loading');
        if (loading) loading.style.display = 'none';
    },

    showEmpty() {
        const empty = document.getElementById('ks-downloads-empty');
        if (empty) empty.style.display = 'block';
        const groups = document.getElementById('ks-downloads-groups');
        if (groups) groups.style.display = 'none';
    },

    _esc(s) {
        if (!s) return '';
        const div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    },

    destroy() {
        this.history = [];
        this.expandedAuthor = null;
        this.searchKeyword = '';
    }
};
