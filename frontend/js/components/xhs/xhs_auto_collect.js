/**
 * 小红书自动化采集管理组件
 */
const XhsAutoCollectPage = {
    _status: null,
    _logs: [],
    _timer: null,
    _loading: false,

    render() {
        return `
            <div class="page-header">
                <div>
                    <h2 class="page-title">小红书自动采集</h2>
                    <p class="page-description">后台定时自动化轮询关注博主的最新作品，支持对数正态随机延迟、风控自动冷却与时间窗口控制。</p>
                </div>
                <div id="xhs-header-actions" style="display: flex; gap: 10px;">
                    <button class="btn btn-secondary" onclick="XhsAutoCollectPage.refreshStatus()">🔄 刷新状态</button>
                    <button class="btn btn-primary" onclick="XhsAutoCollectPage.triggerAll()">⚡ 立即采集全部</button>
                </div>
            </div>

            <!-- 全局状态与配置卡片 -->
            <div class="card" style="margin-bottom: 24px; padding: 20px; background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 12px;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <h3 style="margin: 0; font-size: 1.1rem; color: var(--text-primary);">全局调度设置</h3>
                        <span id="xhs-global-status-badge" class="badge" style="background: rgba(0,0,0,0.08); padding: 4px 10px; border-radius: 20px; font-size: 0.8rem;">检测中...</span>
                    </div>
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <label style="font-weight: 600; font-size: 0.9rem; color: var(--text-primary); cursor: pointer;" for="xhs-global-toggle">全局自动采集:</label>
                        <input type="checkbox" id="xhs-global-toggle" style="width: 20px; height: 20px; cursor: pointer;" onchange="XhsAutoCollectPage.onGlobalToggleChange(this.checked)" />
                    </div>
                </div>

                <!-- 风控与运行状态提示条 -->
                <div id="xhs-cooldown-alert" style="display: none; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; background: rgba(255, 149, 0, 0.1); border: 1px solid rgba(255, 149, 0, 0.3); color: #d97706; font-size: 0.88rem;">
                    ⚠️ <strong>风控冷却保护中：</strong><span id="xhs-cooldown-text">--</span>
                </div>

                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px;">
                    <div>
                        <label style="display: block; font-size: 0.85rem; color: var(--text-muted); margin-bottom: 6px;">采集时段 (起 ~ 止)</label>
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <select id="xhs-window-start" class="form-control" style="padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-input); color: var(--text-primary); width: 100%;">
                                ${Array.from({length: 24}, (_, i) => `<option value="${i}">${String(i).padStart(2, '0')}:00</option>`).join('')}
                            </select>
                            <span>~</span>
                            <select id="xhs-window-end" class="form-control" style="padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-input); color: var(--text-primary); width: 100%;">
                                ${Array.from({length: 25}, (_, i) => `<option value="${i}">${String(i).padStart(2, '0')}:00</option>`).join('')}
                            </select>
                        </div>
                    </div>

                    <div>
                        <label style="display: block; font-size: 0.85rem; color: var(--text-muted); margin-bottom: 6px;">单博主单次采集上限</label>
                        <select id="xhs-max-per-account" class="form-control" style="padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-input); color: var(--text-primary); width: 100%;">
                            <option value="10">最新 10 篇</option>
                            <option value="20" selected>最新 20 篇</option>
                            <option value="30">最新 30 篇 (首屏)</option>
                            <option value="50">最新 50 篇</option>
                        </select>
                    </div>

                    <div>
                        <label style="display: block; font-size: 0.85rem; color: var(--text-muted); margin-bottom: 6px;">触发风控自动冷却 (分钟)</label>
                        <select id="xhs-cooldown-minutes" class="form-control" style="padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-input); color: var(--text-primary); width: 100%;">
                            <option value="10">10 分钟</option>
                            <option value="15" selected>15 分钟</option>
                            <option value="30">30 分钟</option>
                            <option value="60">60 分钟</option>
                        </select>
                    </div>
                </div>

                <!-- 腾讯云 COS 与 远端服务器上传 -->
                <div style="border-top: 1px solid var(--border-color); padding-top: 16px; margin-top: 16px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <h4 style="margin: 0; font-size: 0.95rem; color: var(--text-primary);">☁️ 腾讯云 COS 上传 & 服务器推送</h4>
                            <span id="xhs-cos-badge" class="badge" style="font-size: 0.75rem; padding: 2px 8px; border-radius: 12px; background: rgba(0,0,0,0.06);">COS检测中</span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <label style="font-size: 0.85rem; color: var(--text-primary); cursor: pointer;" for="xhs-upload-toggle">新作品自动推送到服务器:</label>
                            <input type="checkbox" id="xhs-upload-toggle" style="width: 16px; height: 16px; cursor: pointer;" />
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px;">
                        <div>
                            <label style="display: block; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 4px;">服务器上传接口地址</label>
                            <input type="url" id="xhs-upload-url" class="form-control" placeholder="https://example.com/api/data/gzhAdd (为空默认复用公众号接口)" style="padding: 6px 10px; font-size: 0.85rem; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-input); color: var(--text-primary); width: 100%;" />
                        </div>
                        <div>
                            <label style="display: block; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 4px;">设备 ID (deviceId)</label>
                            <input type="text" id="xhs-device-id" class="form-control" placeholder="小红书_caiji100" style="padding: 6px 10px; font-size: 0.85rem; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-input); color: var(--text-primary); width: 100%;" />
                        </div>
                        <div>
                            <label style="display: block; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 4px;">COS 视频存储前缀</label>
                            <input type="text" id="xhs-cos-prefix" class="form-control" placeholder="xhs/" style="padding: 6px 10px; font-size: 0.85rem; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-input); color: var(--text-primary); width: 100%;" />
                        </div>
                    </div>
                    <div class="form-hint" style="font-size: 0.75rem; color: var(--text-muted); margin-top: 6px;">
                        💡 视频下载后将自动上传至腾讯云 COS，并将视频链接与文字描述合并写入 <code>data.json</code>，按公众号一致结构推送远端服务器。COS 凭证复用系统全局设置。
                    </div>
                </div>

                <div style="display: flex; justify-content: flex-end; margin-top: 16px;">
                    <button class="btn btn-primary btn-sm" onclick="XhsAutoCollectPage.saveGlobalSettings()">💾 保存全局配置</button>
                </div>
            </div>

            <!-- 博主采集管理列表 -->
            <div class="card" style="margin-bottom: 24px; padding: 20px; background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 10px;">
                    <h3 style="margin: 0; font-size: 1.1rem; color: var(--text-primary);">博主自动采集列表</h3>
                    <div style="font-size: 0.85rem; color: var(--text-muted);">
                        已开启: <strong id="xhs-enabled-count" style="color: var(--primary);">0</strong> / 总博主: <span id="xhs-total-count">0</span>
                    </div>
                </div>

                <div id="xhs-auto-accounts-container" class="animate-fade-in">
                    <div class="spinner" style="margin: 30px auto;"></div>
                </div>
            </div>

            <!-- 最近采集记录 -->
            <div class="card" style="padding: 20px; background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                    <h3 style="margin: 0; font-size: 1.1rem; color: var(--text-primary);">最近采集日志</h3>
                    <button class="btn btn-secondary btn-sm" onclick="XhsAutoCollectPage.loadLogs()">刷新日志</button>
                </div>
                <div id="xhs-auto-logs-container">
                    <div style="text-align: center; color: var(--text-muted); padding: 20px;">暂无采集日志</div>
                </div>
            </div>
        `;
    },

    async init() {
        this.destroy();
        await this.refreshStatus();
        await this.loadLogs();
        this._startTimer();
    },

    _startTimer() {
        if (this._timer) {
            clearInterval(this._timer);
        }
        // 若正在采集中，每 3 秒快速轮询更新进度；空闲时每 15 秒轮询
        const interval = (this._status && this._status.is_collecting) ? 3000 : 15000;
        this._timer = setInterval(() => {
            this.refreshStatus(true);
        }, interval);
    },

    destroy() {
        if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
        }
    },

    async refreshStatus(silent = false) {
        try {
            const data = await API.xhs.getAutoCollectStatus();
            const wasCollecting = this._status?.is_collecting;
            this._status = data;
            this._renderStatusUI();
            if (wasCollecting !== data.is_collecting) {
                this._startTimer();
                this.loadLogs();
            }
        } catch (err) {
            if (!silent) {
                Toast.error('获取自动采集状态失败: ' + err.message);
            }
        }
    },

    _renderStatusUI() {
        const s = this._status;
        if (!s) return;

        // 顶部操作按钮状态切换（采集中显示红色停止按钮）
        const headerActions = document.getElementById('xhs-header-actions');
        if (headerActions) {
            if (s.is_collecting) {
                headerActions.innerHTML = `
                    <button class="btn btn-secondary" onclick="XhsAutoCollectPage.refreshStatus()">🔄 刷新状态</button>
                    <button class="btn btn-danger" style="background: #ef4444; border-color: #dc2626; color: white; font-weight: 600;" onclick="XhsAutoCollectPage.stopAll()">⏹ 停止全部采集</button>
                `;
            } else {
                headerActions.innerHTML = `
                    <button class="btn btn-secondary" onclick="XhsAutoCollectPage.refreshStatus()">🔄 刷新状态</button>
                    <button class="btn btn-primary" onclick="XhsAutoCollectPage.triggerAll()">⚡ 立即采集全部</button>
                `;
            }
        }

        // 全局开关状态
        const toggle = document.getElementById('xhs-global-toggle');
        if (toggle) toggle.checked = s.global_enabled;

        // 状态徽章
        const badge = document.getElementById('xhs-global-status-badge');
        if (badge) {
            if (s.in_cooldown) {
                badge.style.background = 'rgba(255, 149, 0, 0.15)';
                badge.style.color = '#d97706';
                badge.textContent = `🛑 风控冷却中 (剩 ${Math.round(s.cooldown_remaining / 60)} 分钟)`;
            } else if (s.is_collecting || (s.running_nicknames && s.running_nicknames.length > 0)) {
                badge.style.background = 'rgba(0, 122, 255, 0.15)';
                badge.style.color = 'var(--primary)';
                badge.textContent = `⏳ 正在采集: ${(s.running_nicknames || []).join(', ') || '运行中'}`;
            } else if (!s.global_enabled) {
                badge.style.background = 'rgba(128, 128, 128, 0.15)';
                badge.style.color = 'var(--text-muted)';
                badge.textContent = '⏸ 未启用自动采集';
            } else if (!s.in_window) {
                badge.style.background = 'rgba(0, 122, 255, 0.1)';
                badge.style.color = 'var(--primary)';
                badge.textContent = `🌙 非采集时段 (${s.window_start_hour}:00 - ${s.window_end_hour}:00)`;
            } else {
                badge.style.background = 'rgba(52, 199, 89, 0.15)';
                badge.style.color = '#15803d';
                badge.textContent = '🟢 自动待命中';
            }
        }

        // 冷却提示条
        const cdAlert = document.getElementById('xhs-cooldown-alert');
        const cdText = document.getElementById('xhs-cooldown-text');
        if (cdAlert && cdText) {
            if (s.in_cooldown) {
                cdAlert.style.display = 'block';
                cdText.textContent = `${s.last_error || '由于触发小红书安全风控，已自动暂停采集'}，预计剩余 ${Math.round(s.cooldown_remaining / 60)} 分钟恢复。`;
            } else {
                cdAlert.style.display = 'none';
            }
        }

        // 统计数量
        const enabledCountEl = document.getElementById('xhs-enabled-count');
        const totalCountEl = document.getElementById('xhs-total-count');
        if (enabledCountEl) enabledCountEl.textContent = s.enabled_accounts_count || 0;
        if (totalCountEl) totalCountEl.textContent = s.total_accounts || 0;

        // 全局时间与上限配置回显
        const startEl = document.getElementById('xhs-window-start');
        const endEl = document.getElementById('xhs-window-end');
        const maxEl = document.getElementById('xhs-max-per-account');
        const cdEl = document.getElementById('xhs-cooldown-minutes');
        const uploadToggle = document.getElementById('xhs-upload-toggle');
        const uploadUrlEl = document.getElementById('xhs-upload-url');
        const deviceIdEl = document.getElementById('xhs-device-id');
        const cosPrefixEl = document.getElementById('xhs-cos-prefix');
        const cosBadge = document.getElementById('xhs-cos-badge');

        if (startEl && s.window_start_hour !== undefined) startEl.value = s.window_start_hour;
        if (endEl && s.window_end_hour !== undefined) endEl.value = s.window_end_hour;
        if (maxEl && s.max_per_account !== undefined) maxEl.value = s.max_per_account;
        if (cdEl && s.cooldown_minutes !== undefined) cdEl.value = s.cooldown_minutes;
        if (uploadToggle) uploadToggle.checked = !!s.upload_enabled;
        if (uploadUrlEl && s.upload_url !== undefined) uploadUrlEl.value = s.upload_url || '';
        if (deviceIdEl && s.device_id !== undefined) deviceIdEl.value = s.device_id || '小红书_caiji100';
        if (cosPrefixEl && s.cos_prefix !== undefined) cosPrefixEl.value = s.cos_prefix || 'xhs/';

        if (cosBadge) {
            if (s.has_cos_config) {
                cosBadge.style.background = 'rgba(52, 199, 89, 0.15)';
                cosBadge.style.color = '#15803d';
                cosBadge.textContent = '✓ 腾讯云 COS 凭证正常';
            } else {
                cosBadge.style.background = 'rgba(255, 149, 0, 0.15)';
                cosBadge.style.color = '#d97706';
                cosBadge.textContent = '⚠️ COS 凭证未配置 (仅本地保存)';
            }
        }

        // 渲染博主列表
        this._renderAccountsList(s.accounts || [], s.running_users || []);
    },

    _renderAccountsList(accounts, runningUsers) {
        const container = document.getElementById('xhs-auto-accounts-container');
        if (!container) return;

        if (accounts.length === 0) {
            container.innerHTML = `
                <div style="text-align: center; padding: 40px 20px; color: var(--text-muted);">
                    <p style="margin-bottom: 12px; font-size: 1rem;">暂无收藏博主</p>
                    <button class="btn btn-primary btn-sm" onclick="Router.navigate('xhs_accounts')">前往博主管理添加</button>
                </div>
            `;
            return;
        }

        container.innerHTML = `
            <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px;">
                ${accounts.map(acc => this._renderAccountCard(acc, runningUsers)).join('')}
            </div>
        `;
    },

    _renderAccountCard(acc, runningUsers) {
        const isRunning = runningUsers.includes(acc.user_id);
        const isEnabled = !!acc.auto_collect_enabled;
        const interval = acc.collect_interval_minutes || 120;
        const initial = (acc.nickname || '?').charAt(0);

        const lastTimeStr = acc.last_collect_time ? new Date(acc.last_collect_time * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '从未';
        const nextTimeStr = acc.next_collect_time && isEnabled ? new Date(acc.next_collect_time * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '未排期';

        return `
            <div style="
                background: var(--bg-card);
                border: 1px solid ${isEnabled ? 'var(--primary)' : 'var(--border-color)'};
                border-radius: 10px;
                padding: 16px;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                box-shadow: ${isEnabled ? '0 0 0 1px rgba(0, 122, 255, 0.2)' : 'none'};
            ">
                <div>
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;">
                        <div style="display: flex; gap: 10px; align-items: center;">
                            ${acc.avatar
                                ? `<img src="${acc.avatar}" alt="" style="width: 42px; height: 42px; border-radius: 50%; object-fit: cover;" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" />
                                   <div style="display: none; width: 42px; height: 42px; border-radius: 50%; background: var(--primary); color: white; align-items: center; justify-content: center; font-weight: 700;">${initial}</div>`
                                : `<div style="width: 42px; height: 42px; border-radius: 50%; background: var(--primary); color: white; display: flex; align-items: center; justify-content: center; font-weight: 700;">${initial}</div>`
                            }
                            <div>
                                <h4 style="margin: 0 0 2px; font-size: 0.95rem; color: var(--text-primary); font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 140px;">${this._esc(acc.nickname)}</h4>
                                <span style="font-size: 0.75rem; color: var(--text-muted);">粉丝: ${acc.fans || '0'}</span>
                            </div>
                        </div>

                        <div style="display: flex; align-items: center; gap: 6px;">
                            <label style="font-size: 0.8rem; color: ${isEnabled ? 'var(--primary)' : 'var(--text-muted)'}; font-weight: 600; cursor: pointer;">
                                ${isEnabled ? '自动采集' : '已暂停'}
                            </label>
                            <input type="checkbox" ${isEnabled ? 'checked' : ''} style="cursor: pointer; width: 16px; height: 16px;" onchange="XhsAutoCollectPage.toggleAccount('${acc.user_id}', this.checked)" />
                        </div>
                    </div>

                    <!-- 采集配置与运行指标 -->
                    <div style="background: rgba(0,0,0,0.03); border-radius: 6px; padding: 10px; font-size: 0.8rem; line-height: 1.6; margin-bottom: 12px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                            <span style="color: var(--text-muted);">采集频率:</span>
                            <select style="padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border-color); font-size: 0.75rem; background: var(--bg-input); color: var(--text-primary);" onchange="XhsAutoCollectPage.changeInterval('${acc.user_id}', this.value)">
                                <option value="30" ${interval === 30 ? 'selected' : ''}>每 30 分钟</option>
                                <option value="60" ${interval === 60 ? 'selected' : ''}>每 1 小时</option>
                                <option value="120" ${interval === 120 ? 'selected' : ''}>每 2 小时</option>
                                <option value="240" ${interval === 240 ? 'selected' : ''}>每 4 小时</option>
                                <option value="360" ${interval === 360 ? 'selected' : ''}>每 6 小时</option>
                                <option value="720" ${interval === 720 ? 'selected' : ''}>每 12 小时</option>
                                <option value="1440" ${interval === 1440 ? 'selected' : ''}>每 24 小时</option>
                            </select>
                        </div>
                        <div style="display: flex; justify-content: space-between;">
                            <span style="color: var(--text-muted);">上次采集:</span>
                            <span>${lastTimeStr} ${acc.last_collect_count !== undefined ? `(下载 ${acc.last_collect_count} 篇)` : ''}</span>
                        </div>
                        <div style="display: flex; justify-content: space-between;">
                            <span style="color: var(--text-muted);">下次采集:</span>
                            <span style="color: ${isEnabled ? 'var(--primary)' : 'var(--text-muted)'};">${nextTimeStr}</span>
                        </div>
                        ${acc.last_collect_error ? `
                            <div style="margin-top: 4px; color: var(--error); font-size: 0.75rem; word-break: break-all;">
                                ⚠️ 异常: ${this._esc(acc.last_collect_error)}
                            </div>
                        ` : ''}
                    </div>
                </div>

                <div style="display: flex; gap: 8px; padding-top: 8px; border-top: 1px solid var(--border-color);">
                    <button class="btn btn-secondary btn-sm" style="flex: 1; font-size: 0.75rem;" onclick="Router.navigate('xhs_notes', {user_id: '${acc.user_id}'})">📖 查看笔记</button>
                    ${isRunning ? `
                        <button class="btn btn-danger btn-sm" style="flex: 1; font-size: 0.75rem; background: #ef4444; border-color: #dc2626; color: white; font-weight: 500;" onclick="XhsAutoCollectPage.stopSingle('${acc.user_id}')">
                            ⏹ 停止采集
                        </button>
                    ` : `
                        <button class="btn btn-primary btn-sm" style="flex: 1; font-size: 0.75rem;" onclick="XhsAutoCollectPage.triggerSingle('${acc.user_id}')">
                            🚀 立即采集
                        </button>
                    `}
                </div>
            </div>
        `;
    },

    _esc(s) {
        if (!s) return '';
        const div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    },

    async onGlobalToggleChange(enabled) {
        try {
            await API.xhs.saveAutoCollectSettings({ xhs_auto_collect_enabled: enabled });
            Toast.success(enabled ? '已开启小红书全局自动采集' : '已暂停小红书全局自动采集');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('修改失败: ' + err.message);
        }
    },

    async saveGlobalSettings() {
        const startEl = document.getElementById('xhs-window-start');
        const endEl = document.getElementById('xhs-window-end');
        const maxEl = document.getElementById('xhs-max-per-account');
        const cdEl = document.getElementById('xhs-cooldown-minutes');
        const uploadToggle = document.getElementById('xhs-upload-toggle');
        const uploadUrlEl = document.getElementById('xhs-upload-url');
        const deviceIdEl = document.getElementById('xhs-device-id');
        const cosPrefixEl = document.getElementById('xhs-cos-prefix');

        const payload = {
            xhs_collect_window_start_hour: parseInt(startEl?.value || 8),
            xhs_collect_window_end_hour: parseInt(endEl?.value || 24),
            xhs_collect_max_per_account: parseInt(maxEl?.value || 20),
            xhs_collect_cooldown_minutes: parseInt(cdEl?.value || 15),
            xhs_upload_enabled: !!(uploadToggle && uploadToggle.checked),
            xhs_upload_url: uploadUrlEl ? uploadUrlEl.value.trim() : '',
            xhs_device_id: deviceIdEl ? deviceIdEl.value.trim() : '小红书_caiji100',
            xhs_cos_prefix: cosPrefixEl ? cosPrefixEl.value.trim() : 'xhs/',
        };

        try {
            await API.xhs.saveAutoCollectSettings(payload);
            Toast.success('全局采集配置已保存');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('保存失败: ' + err.message);
        }
    },

    async toggleAccount(userId, enabled) {
        try {
            await API.xhs.toggleAutoCollect({ user_id: userId, auto_collect_enabled: enabled });
            Toast.success(enabled ? '已开启该博主自动采集' : '已暂停该博主自动采集');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('操作失败: ' + err.message);
        }
    },

    async changeInterval(userId, interval) {
        try {
            await API.xhs.toggleAutoCollect({ user_id: userId, collect_interval_minutes: parseInt(interval) });
            Toast.success('采集间隔已更新');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('更新失败: ' + err.message);
        }
    },

    async triggerSingle(userId) {
        try {
            const res = await API.xhs.triggerAutoCollect(userId);
            Toast.success(res.message || '已触发采集');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('触发采集失败: ' + err.message);
        }
    },

    async stopSingle(userId) {
        try {
            const res = await API.xhs.stopAutoCollect(userId);
            Toast.warning(res.message || '已请求停止该博主采集');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('停止失败: ' + err.message);
        }
    },

    async triggerAll() {
        try {
            const res = await API.xhs.triggerAllAutoCollect();
            Toast.success(res.message || '已触发全部采集');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('触发全部采集失败: ' + err.message);
        }
    },

    async stopAll() {
        try {
            const res = await API.xhs.stopAllAutoCollect();
            Toast.warning(res.message || '已请求停止所有采集任务');
            await this.refreshStatus();
        } catch (err) {
            Toast.error('停止失败: ' + err.message);
        }
    },

    async loadLogs() {
        const container = document.getElementById('xhs-auto-logs-container');
        if (!container) return;

        try {
            const data = await API.xhs.getAutoCollectLogs(30);
            const logs = data.logs || [];
            this._logs = logs;

            if (logs.length === 0) {
                container.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 20px;">暂无采集日志</div>`;
                return;
            }

            container.innerHTML = `
                <div style="overflow-x: auto;">
                    <table class="data-table" style="width: 100%; border-collapse: collapse; font-size: 0.85rem;">
                        <thead>
                            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted); text-align: left;">
                                <th style="padding: 10px;">时间</th>
                                <th style="padding: 10px;">博主</th>
                                <th style="padding: 10px;">新下载</th>
                                <th style="padding: 10px;">跳过</th>
                                <th style="padding: 10px;">服务器推送</th>
                                <th style="padding: 10px;">运行状态</th>
                                <th style="padding: 10px; text-align: right;">作品明细</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${logs.map((log, lIdx) => {
                                const timeStr = new Date(log.time * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
                                const items = log.items || [];
                                const hasItems = items.length > 0;
                                const rowId = `xhs-log-detail-${lIdx}`;
                                
                                const upCount = log.uploaded_count || items.filter(it => it.uploaded).length;
                                const upFailCount = log.upload_failed_count || items.filter(it => it.download_success && it.upload_error).length;

                                return `
                                    <tr style="border-bottom: 1px solid rgba(0,0,0,0.05); background: ${lIdx % 2 === 0 ? 'transparent' : 'rgba(0,0,0,0.01)'};">
                                        <td style="padding: 10px; color: var(--text-muted); white-space: nowrap;">${timeStr}</td>
                                        <td style="padding: 10px; font-weight: 600; color: var(--text-primary);">${this._esc(log.nickname || log.user_id)}</td>
                                        <td style="padding: 10px;"><strong style="color: ${log.new_count > 0 ? '#15803d' : 'var(--text-muted)'};">+${log.new_count || 0}</strong></td>
                                        <td style="padding: 10px; color: var(--text-muted);">${log.skipped_count || 0}</td>
                                        <td style="padding: 10px; white-space: nowrap;">
                                            ${upCount > 0
                                                ? `<span style="color: #15803d; font-weight: 600;">✓ 推送 ${upCount} 篇</span>`
                                                : (upFailCount > 0
                                                    ? `<span style="color: #15803d; font-weight: 600;">✗ 失败 ${upFailCount} 篇</span>`
                                                    : (log.new_count > 0
                                                        ? `<span style="color: var(--text-muted); font-size: 0.8rem;">未开启推送</span>`
                                                        : `<span style="color: var(--text-muted);">-</span>`
                                                    )
                                                )
                                            }
                                        </td>
                                        <td style="padding: 10px;">
                                            ${log.success
                                                ? `<span style="color: #15803d; font-weight: 600;">✓ 采集完成</span>`
                                                : `<span style="color: var(--error); font-weight: 600;" title="${this._esc(log.error || '')}">✗ 异常 (${this._esc(log.error || '未知')})</span>`
                                            }
                                        </td>
                                        <td style="padding: 10px; text-align: right;">
                                            ${hasItems
                                                ? `<button type="button" class="btn btn-secondary btn-sm" style="font-size: 0.75rem; padding: 3px 8px; cursor: pointer;" onclick="window.XhsAutoCollectPage.toggleLogDetail(${lIdx})">
                                                    🔍 查看作品详情 (${items.length} 篇)
                                                   </button>`
                                                : `<span style="color: var(--text-muted); font-size: 0.8rem;">无新下载</span>`
                                            }
                                        </td>
                                    </tr>
                                    ${hasItems ? `
                                        <tr id="${rowId}" style="display: none; background: rgba(0,0,0,0.02);">
                                            <td colspan="7" style="padding: 12px 16px;">
                                                <div style="background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; font-size: 0.82rem;">
                                                    <div style="font-weight: 600; margin-bottom: 8px; color: var(--text-primary); display: flex; justify-content: space-between; align-items: center;">
                                                        <span>📥 采集与推送明细列表：</span>
                                                        <span style="color: var(--text-muted); font-size: 0.75rem; font-weight: normal;">共 ${items.length} 篇作品</span>
                                                    </div>
                                                    <table style="width: 100%; border-collapse: collapse;">
                                                        <thead>
                                                            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted); font-size: 0.78rem; text-align: left;">
                                                                <th style="padding: 6px 8px;">作品标题</th>
                                                                <th style="padding: 6px 8px;">类型</th>
                                                                <th style="padding: 6px 8px;">本地下载</th>
                                                                <th style="padding: 6px 8px;">腾讯云 COS 视频</th>
                                                                <th style="padding: 6px 8px;">远端服务器推送</th>
                                                                <th style="padding: 6px 8px; text-align: right;">操作</th>
                                                            </tr>
                                                        </thead>
                                                        <tbody>
                                                            ${items.map((item, itemIdx) => `
                                                                <tr style="border-bottom: 1px solid rgba(0,0,0,0.03);">
                                                                    <td style="padding: 6px 8px; font-weight: 500; color: var(--text-primary); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${this._esc(item.title || item.note_id)}">
                                                                        ${this._esc(item.title || item.note_id)}
                                                                    </td>
                                                                    <td style="padding: 6px 8px; color: var(--text-muted); white-space: nowrap;">${item.type || '图文'}</td>
                                                                    <td style="padding: 6px 8px; white-space: nowrap;">
                                                                        ${item.download_success
                                                                            ? `<span style="color: #15803d;">✓ 下载成功</span>`
                                                                            : `<span style="color: var(--error);" title="${this._esc(item.error || '')}">✗ 失败 (${this._esc(item.error || '未知')})</span>`
                                                                        }
                                                                    </td>
                                                                    <td style="padding: 6px 8px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                                                                        ${item.cos_url
                                                                            ? `<a href="${this._esc(item.cos_url)}" target="_blank" style="color: var(--primary); text-decoration: underline;" title="${this._esc(item.cos_url)}">✓ COS链接</a>`
                                                                            : `<span style="color: var(--text-muted);">-</span>`
                                                                        }
                                                                    </td>
                                                                    <td style="padding: 6px 8px; white-space: nowrap;">
                                                                        ${item.uploaded
                                                                            ? `<span style="color: #15803d; font-weight: 600;">✓ 推送成功</span>`
                                                                            : (item.upload_error
                                                                                ? `<span style="color: var(--error);" title="${this._esc(item.upload_error)}">✗ 失败: ${this._esc(item.upload_error)}</span>`
                                                                                : `<span style="color: var(--text-muted);">未开启或未推送</span>`
                                                                            )
                                                                        }
                                                                    </td>
                                                                    <td style="padding: 6px 8px; text-align: right; white-space: nowrap;">
                                                                        ${item.path ? `
                                                                            <button type="button" class="btn btn-outline btn-sm" style="font-size: 0.72rem; padding: 2px 6px; cursor: pointer;" onclick="window.XhsAutoCollectPage.openLogItemPath(${lIdx}, ${itemIdx})">
                                                                                📁 打开目录
                                                                            </button>
                                                                        ` : '-'}
                                                                    </td>
                                                                </tr>
                                                            `).join('')}
                                                        </tbody>
                                                    </table>
                                                </div>
                                            </td>
                                        </tr>
                                    ` : ''}
                                `;
                            }).join('')}
                        </tbody>
                    </table>
                </div>
            `;
        } catch (err) {
            container.innerHTML = `<div style="color: var(--error); padding: 12px;">加载日志失败: ${err.message}</div>`;
        }
    },

    toggleLogDetail(lIdx) {
        const el = document.getElementById(`xhs-log-detail-${lIdx}`);
        if (el) {
            const isHidden = el.style.display === 'none' || !el.style.display;
            el.style.display = isHidden ? 'table-row' : 'none';
        }
    },

    async openLogItemPath(lIdx, itemIdx) {
        const item = this._logs?.[lIdx]?.items?.[itemIdx];
        if (!item || !item.path) {
            Toast.warning('无可用本地路径');
            return;
        }
        try {
            await API.xhs.openParent(item.path);
        } catch (err) {
            Toast.error('打开目录失败: ' + err.message);
        }
    }
};

window.XhsAutoCollectPage = XhsAutoCollectPage;

