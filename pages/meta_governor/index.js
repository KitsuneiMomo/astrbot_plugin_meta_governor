async function init() {
    const bridge = window.AstrBotPluginPage;

    try {
        if (bridge && typeof bridge.ready === "function") {
            await bridge.ready();
        }
    } catch (e) {
        console.error("Failed to connect to AstrBot Bridge:", e);
    }

    // State Variables
    let currentConfig = {};
    let currentSessions = {};
    let currentConstraints = [];
    let currentBannedPhrases = [];
    let currentProviders = [];
    let currentEditingConstraintId = null;

    // Helper: HTML Escaping for XSS protection
    function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    const DEFAULT_SUPERVISOR_PROMPT_TEMPLATE = `你是一个对话质量观察员，负责审查 AI 助手最近的表现并提供【针对性的温馨提醒与调整建议】。

【工作原则】
1. 默认输出为"无问题"。宁缺毋滥，仅在发现明确表达缺陷时才输出违规。
2. 任何判定必须附上从对话中逐字引用的原文证据 (evidence)。
3. 【拟人化提示格式】：在给出的改进建议 (suggestion) 中，严禁使用死板冷酷的控制命令（如‘避免...’、‘禁止...’）。
   **必须统一采用“我发现你……，请……”的第1人称提醒口吻**，格式固定为：
   \`"我发现你 [具体问题表现]，请 [具体调整动作]"\` (40字以内)。
   示例好建议：
   - "我发现你最近回答像在做严肃社论播报，请放轻松，多用自然的口语和语气词跟对方交流。"
   - "我发现你连续重复回复了单字“测试”，请结合当前话题给出有实意的回答。"
   - "我发现你与对方在无意义争吵，请礼貌收尾并主动转移话题。"
4. 【规则模式 (mode)】：
   - \`"once"\`：一次性提醒。适用于转移话题、主动收尾、总结、道歉等只应在接下来 1 轮中执行的单次动作。
   - \`"multi"\`：多轮保持。适用于表达语气、口吻风格、性格调理等需要持续多轮保持的规范。
5. 【场景区别】以下对话分为【参考上下文 (前 {n_count} 轮)】与【待检查对话 (最近 {k_count} 轮)】。
   参考上下文仅用于帮助你理解话题背景，请专门检查和评估【最近 {k_count} 轮对话】中 AI 助手的表现！
6. 请针对【当前生效中的规则】列表中的每一条规则，在 assessments 数组中显式提供判定 (verdict: improved|partial|not_improved|worse) 与引用证据。

【检查触发原因与提示】
{trigger_reason}

【规则检查项清单】
{constraints_desc}

【当前生效中的调整规则】
{directives_text}

【参考上下文 (前 {n_count} 轮对话 - 仅供了解背景，不做判定)】
{prior_text}

【待检查对话 (最近 {k_count} 轮对话 - 重点检查对象)】
{target_text}

【历史检查摘要】
{history_text}

【输出格式】必须输出且仅输出严格 JSON 格式：
{
  "assessments": [
    {"directive_id": "规则ID", "verdict": "improved|partial|not_improved|worse", "evidence": "逐字引用原文", "confidence": "high|low"}
  ],
  "violations": [
    {
      "constraint_id": "规则ID",
      "severity": "medium|high",
      "evidence": "逐字引用原文",
      "suggestion": "我发现你...，请...（40字以内）",
      "mode": "once|multi"
    }
  ]
}
无问题则两个数组都为空。`;

    // Main Tab Navigation
    const tabButtons = document.querySelectorAll(".tab-btn");
    const tabViews = document.querySelectorAll(".tab-view");

    tabButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");

            tabButtons.forEach(b => b.classList.remove("active"));
            tabViews.forEach(v => v.classList.remove("active"));

            btn.classList.add("active");
            document.getElementById(`view-${targetTab}`).classList.add("active");

            if (targetTab === "settings") {
                renderSettingsForm(currentConfig);
            }

            if (typeof lucide !== "undefined") {
                lucide.createIcons();
            }
        });
    });

    // Sub Tab Navigation (Sidebar)
    const subTabButtons = document.querySelectorAll(".sub-tab-btn");
    const subTabViews = document.querySelectorAll(".sub-tab-content");

    subTabButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetSubTab = btn.getAttribute("data-sub-tab");

            subTabButtons.forEach(b => b.classList.remove("active"));
            subTabViews.forEach(v => v.classList.remove("active"));

            btn.classList.add("active");
            document.getElementById(`sub-tab-content-${targetSubTab}`).classList.add("active");

            if (typeof lucide !== "undefined") {
                lucide.createIcons();
            }
        });
    });

    // Back Button
    const backBtn = document.getElementById("back-btn");
    if (window.self !== window.top) {
        backBtn.style.display = "none";
    } else {
        backBtn.addEventListener("click", () => {
            showToast("请使用外部导航返回");
        });
    }

    // Check if user is currently editing a session name inline
    function isEditingUmo() {
        return !!document.querySelector(".inline-umo-input");
    }

    // Format UMO display into friendly label with double-click edit capability
    function formatUmoDisplay(umo, sessionName = "") {
        if (!umo) return "<code>-</code>";
        const parts = umo.split(":");
        const platform = parts[0] || "bot";
        const msgType = parts[1] || "";
        const targetId = parts[2] || parts[0] || umo;

        const isGroup = msgType.includes("Group");
        const typeLabel = isGroup ? "群聊" : "私聊";
        const typeBadgeClass = isGroup ? "badge-info" : "badge-purple";
        const nameText = sessionName || targetId;

        const safeUmo = escapeHtml(umo);
        const safeSessionName = escapeHtml(sessionName || "");
        const safeNameText = escapeHtml(nameText);
        const safePlatform = escapeHtml(platform);

        return `
            <div class="umo-cell" data-umo="${safeUmo}" data-name="${safeSessionName}" title="双击修改群名/备注 (留空恢复群号) | 原始 UMO: ${safeUmo}">
                <span class="badge ${typeBadgeClass}">${typeLabel}</span>
                <strong class="umo-name" data-umo="${safeUmo}">${safeNameText}</strong>
                <span class="umo-platform">(${safePlatform})</span>
            </div>
        `;
    }

    // Handle Double Click inline edit for session names
    document.addEventListener("dblclick", (e) => {
        const cell = e.target.closest(".umo-cell");
        if (!cell || cell.querySelector("input")) return;

        const umo = cell.getAttribute("data-umo");
        const currentName = cell.getAttribute("data-name") || "";
        const nameElem = cell.querySelector(".umo-name");
        if (!umo || !nameElem) return;

        const input = document.createElement("input");
        input.type = "text";
        input.className = "form-control form-control-sm inline-umo-input";
        input.value = currentName;
        input.placeholder = "群名/备注(留空恢复群号)";
        input.style.width = "150px";
        input.style.display = "inline-block";
        input.style.padding = "2px 6px";
        input.style.fontSize = "13px";

        nameElem.replaceWith(input);
        input.focus();
        input.select();

        let isSaved = false;

        const saveChange = async () => {
            if (isSaved) return;
            isSaved = true;
            const newName = input.value.trim();

            try {
                let res;
                if (bridge && typeof bridge.apiPost === "function") {
                    res = await bridge.apiPost("update_session_name", { umo, session_name: newName });
                } else {
                    const response = await fetch("/astrbot_plugin_meta_governor/update_session_name", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ umo, session_name: newName }),
                    });
                    res = await response.json();
                }

                if (res && (res.status === "success" || res.code === 200)) {
                    showToast(newName ? `已修改备注为: '${newName}'` : "已清空备注，恢复显示群号");
                } else {
                    showToast("更新失败: " + (res ? res.message : "未知错误"), true);
                }
            } catch (err) {
                showToast("请求异常: " + err.message, true);
            } finally {
                await loadData(true);
            }
        };

        input.addEventListener("keydown", async (evt) => {
            if (evt.key === "Enter") {
                evt.preventDefault();
                input.blur();
            } else if (evt.key === "Escape") {
                isSaved = true;
                await loadData(true);
            }
        });

        input.addEventListener("blur", saveChange);
    });

    // Setup Custom Selects
    setupCustomSelects();

    function setupCustomSelects() {
        const selects = document.querySelectorAll(".custom-select");

        selects.forEach(select => {
            const trigger = select.querySelector(".custom-select-trigger");

            trigger.addEventListener("click", (e) => {
                e.stopPropagation();
                selects.forEach(s => {
                    if (s !== select) s.classList.remove("open");
                });
                select.classList.toggle("open");
            });
        });

        document.addEventListener("click", () => {
            selects.forEach(s => s.classList.remove("open"));
        });
    }

    function bindSelectOptionClick(selectId) {
        const select = document.getElementById(selectId);
        if (!select) return;

        const options = select.querySelectorAll(".custom-option");
        options.forEach(opt => {
            opt.addEventListener("click", (e) => {
                e.stopPropagation();
                const val = opt.getAttribute("data-value");
                const label = opt.textContent;

                select.setAttribute("data-value", val);
                select.querySelector(".custom-select-value").textContent = label;

                options.forEach(o => o.classList.remove("selected"));
                opt.classList.add("selected");

                select.classList.remove("open");
            });
        });
    }

    function setCustomSelectValue(selectId, val) {
        const select = document.getElementById(selectId);
        if (!select) return;

        const option = select.querySelector(`.custom-option[data-value="${val}"]`);
        if (option) {
            select.setAttribute("data-value", val);
            select.querySelector(".custom-select-value").textContent = option.textContent;

            select.querySelectorAll(".custom-option").forEach(o => o.classList.remove("selected"));
            option.classList.add("selected");
        } else {
            select.setAttribute("data-value", val || "");
            select.querySelector(".custom-select-value").textContent = val ? `已指定: ${val}` : "跟随当前聊天模型 (默认)";
        }
    }

    function getCustomSelectValue(selectId) {
        const select = document.getElementById(selectId);
        return select ? (select.getAttribute("data-value") || "") : "";
    }

    // Toast Alert
    function showToast(message, isError = false) {
        const toast = document.getElementById("toast");
        toast.textContent = message;
        toast.style.backgroundColor = isError ? "var(--color-danger)" : "var(--text-primary)";
        toast.style.color = isError ? "#ffffff" : "var(--bg-card)";
        toast.classList.add("show");

        setTimeout(() => {
            toast.classList.remove("show");
        }, 3000);
    }

    function formatTime(timestamp) {
        if (!timestamp) return "-";
        const date = new Date(timestamp * 1000);
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
    }

    // Modals Handling
    const constraintModal = document.getElementById("constraint-modal");
    const confirmModal = document.getElementById("confirm-modal");

    document.getElementById("btn-add-constraint").addEventListener("click", () => {
        openConstraintModal();
    });

    document.getElementById("constraint-modal-close").addEventListener("click", closeConstraintModal);
    document.getElementById("constraint-modal-cancel").addEventListener("click", closeConstraintModal);
    constraintModal.addEventListener("click", (e) => {
        if (e.target === constraintModal) closeConstraintModal();
    });

    function openConstraintModal(constraint = null) {
        currentEditingConstraintId = constraint ? constraint.id : null;
        document.getElementById("constraint-modal-title").textContent = constraint ? "编辑检查规则" : "新增检查规则";
        
        const idInput = document.getElementById("c-id");
        idInput.value = constraint ? constraint.id : "";
        idInput.disabled = !!constraint;
        
        document.getElementById("c-name").value = constraint ? constraint.name : "";
        document.getElementById("c-desc").value = constraint ? constraint.description : "";

        setCustomSelectValue("select-constraint-mode", constraint ? (constraint.mode || "auto") : "auto");
        
        constraintModal.classList.add("active");
    }

    function closeConstraintModal() {
        constraintModal.classList.remove("active");
        document.getElementById("constraint-form").reset();
        document.getElementById("c-id").disabled = false;
        currentEditingConstraintId = null;
    }

    // Save Constraint Rule
    document.getElementById("constraint-modal-save").addEventListener("click", async () => {
        const id = (currentEditingConstraintId || document.getElementById("c-id").value).trim();
        const name = document.getElementById("c-name").value.trim();
        const playbook = "free";
        const description = document.getElementById("c-desc").value.trim();
        const mode = getCustomSelectValue("select-constraint-mode") || "auto";

        if (!id || !name) {
            showToast("规则 ID 和名称不能为空", true);
            return;
        }

        try {
            let res;
            if (bridge && typeof bridge.apiPost === "function") {
                res = await bridge.apiPost("save_constraint", { id, name, playbook, description, mode });
            } else {
                const response = await fetch("/astrbot_plugin_meta_governor/save_constraint", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ id, name, playbook, description, mode }),
                });
                res = await response.json();
            }

            if (res && (res.status === "success" || res.code === 200)) {
                showToast("保存检查规则成功！");
                closeConstraintModal();
                await loadData(true);
            } else {
                showToast("保存失败: " + (res.message || "未知错误"), true);
            }
        } catch (e) {
            showToast("请求异常: " + e.message, true);
        }
    });

    // Confirm Delete Modal
    let pendingDeleteAction = null;

    document.getElementById("confirm-modal-cancel").addEventListener("click", closeConfirmModal);
    confirmModal.addEventListener("click", (e) => {
        if (e.target === confirmModal) closeConfirmModal();
    });

    function closeConfirmModal() {
        confirmModal.classList.remove("active");
        pendingDeleteAction = null;
    }

    document.getElementById("confirm-modal-ok").addEventListener("click", async () => {
        if (pendingDeleteAction) {
            await pendingDeleteAction();
            closeConfirmModal();
        }
    });

    function confirmDelete(msg, actionFn) {
        document.getElementById("confirm-modal-msg").textContent = msg;
        pendingDeleteAction = actionFn;
        confirmModal.classList.add("active");
    }

    // Banned Phrase Actions
    document.getElementById("btn-add-banned").addEventListener("click", async () => {
        const input = document.getElementById("banned-phrase-input");
        const phrase = input.value.trim();
        if (!phrase) return;

        try {
            let res;
            if (bridge && typeof bridge.apiPost === "function") {
                res = await bridge.apiPost("save_banned_phrase", { phrase });
            } else {
                const response = await fetch("/astrbot_plugin_meta_governor/save_banned_phrase", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ phrase }),
                });
                res = await response.json();
            }

            if (res && (res.status === "success" || res.code === 200)) {
                showToast(`成功添加禁用词: '${phrase}'`);
                input.value = "";
                await loadData(true);
            } else {
                showToast("添加失败: " + (res.message || "未知错误"), true);
            }
        } catch (e) {
            showToast("请求异常: " + e.message, true);
        }
    });

    document.getElementById("banned-phrase-input").addEventListener("keypress", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            document.getElementById("btn-add-banned").click();
        }
    });

    // Trigger Manual Eval
    document.getElementById("btn-trigger-eval").addEventListener("click", async () => {
        try {
            let res;
            if (bridge && typeof bridge.apiPost === "function") {
                res = await bridge.apiPost("trigger_eval", {});
            } else {
                const response = await fetch("/astrbot_plugin_meta_governor/trigger_eval", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                });
                res = await response.json();
            }

            if (res && (res.status === "success" || res.code === 200)) {
                showToast("已发起后台对话质量检查！");
                setTimeout(() => loadData(false), 2000);
            } else {
                showToast("触发失败: " + (res.message || "未知错误"), true);
            }
        } catch (e) {
            showToast("请求异常: " + e.message, true);
        }
    });

    // Fill Default Prompt Template Button Action
    const fillDefaultPromptBtn = document.getElementById("btn-fill-default-prompt");
    if (fillDefaultPromptBtn) {
        fillDefaultPromptBtn.addEventListener("click", () => {
            const promptInput = document.getElementById("cfg-supervisor-prompt");
            if (promptInput) {
                promptInput.value = DEFAULT_SUPERVISOR_PROMPT_TEMPLATE;
                showToast("已成功载入系统默认提示词模板");
            }
        });
    }

    // Save Settings Form
    document.getElementById("btn-save-settings").addEventListener("click", async () => {
        const enable = document.getElementById("cfg-enable").checked;
        const k_rounds = parseInt(document.getElementById("cfg-k-rounds").value) || 5;
        const n_context_rounds = parseInt(document.getElementById("cfg-n-context-rounds").value) || 3;
        const ttl_rounds = parseInt(document.getElementById("cfg-ttl-rounds").value) || 10;
        const max_active_directives = parseInt(document.getElementById("cfg-max-active").value) || 3;
        const supervisor_provider_id = getCustomSelectValue("select-supervisor-provider");
        const supervisor_prompt_template = document.getElementById("cfg-supervisor-prompt").value.trim();
        const enable_sentinel_heuristics = document.getElementById("cfg-sentinel-heuristics").checked;
        const sentinel_sensitivity = getCustomSelectValue("select-sentinel-sensitivity");
        const verbose = document.getElementById("cfg-verbose").checked;
        const clear_directives_on_reset = document.getElementById("cfg-reset-clear").checked;

        const configData = {
            enable,
            k_rounds,
            n_context_rounds,
            ttl_rounds,
            max_active_directives,
            supervisor_provider_id,
            supervisor_prompt_template,
            enable_sentinel_heuristics,
            sentinel_sensitivity,
            verbose,
            clear_directives_on_reset,
        };

        try {
            let res;
            if (bridge && typeof bridge.apiPost === "function") {
                res = await bridge.apiPost("save_config", configData);
            } else {
                const response = await fetch("/astrbot_plugin_meta_governor/save_config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(configData),
                });
                res = await response.json();
            }

            if (res && (res.status === "success" || res.code === 200)) {
                showToast("配置保存成功！");
                await loadData(true);
            } else {
                showToast("保存配置失败: " + (res.message || "未知错误"), true);
            }
        } catch (e) {
            showToast("请求异常: " + e.message, true);
        }
    });

    function isEditingSettings() {
        const settingsView = document.getElementById("view-settings");
        const isSettingsTabActive = settingsView && settingsView.classList.contains("active");
        const activeElem = document.activeElement;
        const configForm = document.getElementById("config-form");
        const isFocusInsideForm = configForm && activeElem && configForm.contains(activeElem);

        return isSettingsTabActive || isFocusInsideForm;
    }

    // Load All Data
    async function loadData(forceSettingsRender = false) {
        try {
            let data;
            if (bridge && typeof bridge.apiGet === "function") {
                data = await bridge.apiGet("get_data");
            } else {
                const res = await fetch("/astrbot_plugin_meta_governor/get_data");
                data = await res.json();
            }

            if (!data) return;

            currentConfig = data.config || {};
            currentSessions = data.sessions || {};
            currentConstraints = data.constraints || [];
            currentBannedPhrases = data.banned_phrases || [];
            currentProviders = data.providers || [];

            renderProvidersDropdown(currentProviders);
            renderStats(currentSessions);

            if (!isEditingUmo()) {
                renderDirectivesTable(currentSessions);
                renderSessionsTable(currentSessions);
            }

            renderLogsTable(currentSessions);
            renderConstraintsTable(currentConstraints);
            renderBannedPhrases(currentBannedPhrases);

            if (forceSettingsRender || !isEditingSettings()) {
                renderSettingsForm(currentConfig);
            }

            if (typeof lucide !== "undefined") {
                lucide.createIcons();
            }
        } catch (e) {
            console.error("加载数据失败:", e);
        }
    }

    function renderProvidersDropdown(providers) {
        const listContainer = document.getElementById("provider-options-list");
        if (!listContainer) return;

        const currentVal = getCustomSelectValue("select-supervisor-provider");
        let html = `<div class="custom-option ${!currentVal ? 'selected' : ''}" data-value="">跟随当前聊天模型 (默认)</div>`;
        if (providers && providers.length > 0) {
            providers.forEach(p => {
                const pId = typeof p === "string" ? p : p.id;
                const pName = typeof p === "string" ? p : (p.name || p.id);
                const isSel = pId === currentVal ? 'selected' : '';
                const safePId = escapeHtml(pId);
                const safePName = escapeHtml(pName);
                html += `<div class="custom-option ${isSel}" data-value="${safePId}">${safePName} (${safePId})</div>`;
            });
        }
        listContainer.innerHTML = html;
        bindSelectOptionClick("select-supervisor-provider");
    }

    function renderStats(sessions) {
        let totalActiveDirs = 0;
        let totalRounds = 0;
        let maxLastEval = 0;
        let totalCharter = 0;

        Object.values(sessions).forEach(s => {
            totalRounds += s.round_counter || 0;
            if ((s.last_eval_round || 0) > maxLastEval) maxLastEval = s.last_eval_round;
            if (s.charter) totalCharter += s.charter.length;

            if (s.directives) {
                Object.values(s.directives).forEach(d => {
                    if (d.state === "active" || d.state === "fading") totalActiveDirs++;
                });
            }
        });

        document.getElementById("stat-active-dirs").textContent = totalActiveDirs;
        document.getElementById("stat-total-rounds").textContent = totalRounds;
        document.getElementById("stat-last-eval").textContent = maxLastEval;
        document.getElementById("stat-charter-count").textContent = totalCharter;
    }

    function renderDirectivesTable(sessions) {
        const tbody = document.getElementById("directives-body");
        let html = "";

        let activeItems = [];
        let expiredItems = [];
        let charterItems = [];

        Object.entries(sessions).forEach(([umo, s]) => {
            const sName = s.session_name || "";
            if (s.charter) {
                s.charter.forEach((cText, idx) => {
                    charterItems.push({
                        umo,
                        sessionName: sName,
                        type: "charter",
                        id: `charter_${idx+1}`,
                        constraint_id: "常驻规则",
                        text: cText,
                    });
                });
            }

            if (s.directives) {
                Object.values(s.directives).forEach(d => {
                    if (d.state === "active" || d.state === "fading") {
                        activeItems.push({ umo, sessionName: sName, directive: d, type: "active" });
                    } else {
                        expiredItems.push({ umo, sessionName: sName, directive: d, type: "expired" });
                    }
                });
            }
        });

        expiredItems.sort((a, b) => (b.directive.created_round || 0) - (a.directive.created_round || 0));
        const recentExpired = expiredItems.slice(0, 5);

        const allDisplayItems = [...activeItems, ...charterItems, ...recentExpired];

        if (allDisplayItems.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="table-empty">暂无调整规则与常驻规则。</td></tr>`;
            return;
        }

        allDisplayItems.forEach(item => {
            const umoHtml = formatUmoDisplay(item.umo, item.sessionName);
            const safeText = escapeHtml(item.text);
            const safeUmo = escapeHtml(item.umo);

            if (item.type === "charter") {
                html += `
                    <tr>
                        <td><code>-</code></td>
                        <td>${umoHtml}</td>
                        <td><span class="badge badge-purple">常驻规则</span></td>
                        <td style="max-width: 520px;"><strong>${safeText}</strong></td>
                        <td>
                            <div class="stacked-status">
                                <span class="badge badge-purple">常驻规则</span>
                                <span class="badge badge-danger">永久生效</span>
                            </div>
                        </td>
                        <td>-</td>
                    </tr>
                `;
            } else if (item.type === "active") {
                const d = item.directive;
                const safeDId = escapeHtml(d.id);
                const safeConstraintId = escapeHtml(d.constraint_id);
                const safeDText = escapeHtml(d.text);
                const safeRemainingTtl = escapeHtml(d.remaining_ttl);
                const safeTtlRounds = escapeHtml(d.ttl_rounds);

                const stateBadge = d.state === "active"
                    ? `<span class="badge badge-danger">生效中</span>`
                    : `<span class="badge badge-warning">观察期 (良好)</span>`;

                const intensityBadge = d.intensity === "必须"
                    ? `<span class="badge badge-danger">必须</span>`
                    : `<span class="badge badge-warning">尽量</span>`;

                const modeBadge = d.mode === "once"
                    ? `<span class="badge badge-purple">一次性</span>`
                    : `<span class="badge badge-info">多轮</span>`;

                const ttlBadge = `<span class="badge badge-secondary">${safeRemainingTtl} / ${safeTtlRounds} 轮</span>`;

                html += `
                    <tr>
                        <td><code>${safeDId}</code></td>
                        <td>${umoHtml}</td>
                        <td><span class="badge badge-info">${safeConstraintId}</span></td>
                        <td style="max-width: 520px;"><strong>${safeDText}</strong></td>
                        <td>
                            <div class="stacked-status">
                                ${modeBadge}
                                ${intensityBadge}
                                ${ttlBadge}
                                ${stateBadge}
                            </div>
                        </td>
                        <td>
                            <button class="btn btn-sm btn-outline-danger btn-revoke" data-umo="${safeUmo}" data-id="${safeDId}">
                                解除规则
                            </button>
                        </td>
                    </tr>
                `;
            } else if (item.type === "expired") {
                const d = item.directive;
                const safeDId = escapeHtml(d.id);
                const safeConstraintId = escapeHtml(d.constraint_id);
                const safeDText = escapeHtml(d.text);

                const modeBadge = d.mode === "once"
                    ? `<span class="badge badge-secondary">一次性</span>`
                    : `<span class="badge badge-secondary">多轮</span>`;

                html += `
                    <tr>
                        <td><code>${safeDId}</code></td>
                        <td>${umoHtml}</td>
                        <td><span class="badge badge-secondary">${safeConstraintId}</span></td>
                        <td style="max-width: 520px; opacity: 0.75;">${safeDText}</td>
                        <td>
                            <div class="stacked-status">
                                ${modeBadge}
                                <span class="badge badge-secondary">已解除</span>
                            </div>
                        </td>
                        <td><span style="color: var(--text-secondary); font-size: 12px;">已结束</span></td>
                    </tr>
                `;
            }
        });

        tbody.innerHTML = html;

        tbody.querySelectorAll(".btn-revoke").forEach(btn => {
            btn.addEventListener("click", () => {
                const umo = btn.getAttribute("data-umo");
                const dId = btn.getAttribute("data-id");
                confirmDelete(`确定要解除规则 [${dId}] 吗？`, async () => {
                    try {
                        let res;
                        if (bridge && typeof bridge.apiPost === "function") {
                            res = await bridge.apiPost("revoke_directive", { umo, directive_id: dId });
                        } else {
                            const response = await fetch("/astrbot_plugin_meta_governor/revoke_directive", {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ umo, directive_id: dId }),
                            });
                            res = await response.json();
                        }
                        if (res && (res.status === "success" || res.code === 200)) {
                            showToast("成功解除调整规则！");
                            await loadData(false);
                        }
                    } catch (e) {
                        showToast("解除失败: " + e.message, true);
                    }
                });
            });
        });
    }

    function renderSessionsTable(sessions) {
        const tbody = document.getElementById("sessions-body");
        const sessionEntries = Object.entries(sessions);

        if (sessionEntries.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="table-empty">暂无聊天窗口记录。</td></tr>`;
            return;
        }

        let html = "";
        sessionEntries.forEach(([umo, s]) => {
            let activeCount = 0;
            if (s.directives) {
                Object.values(s.directives).forEach(d => {
                    if (d.state === "active" || d.state === "fading") activeCount++;
                });
            }

            const statusBadge = s.paused
                ? `<span class="badge badge-warning">已暂停</span>`
                : `<span class="badge badge-success">运行中</span>`;

            const umoHtml = formatUmoDisplay(umo, s.session_name);
            const safeUmo = escapeHtml(umo);
            const safeRoundCounter = escapeHtml(s.round_counter || 0);
            const safeLastEvalRound = escapeHtml(s.last_eval_round || 0);

            html += `
                <tr>
                    <td>${umoHtml}</td>
                    <td>${safeRoundCounter} 轮</td>
                    <td>${safeLastEvalRound} 轮</td>
                    <td><span class="badge badge-info">${activeCount}</span></td>
                    <td>${statusBadge}</td>
                    <td>
                        <div style="display: flex; gap: 8px;">
                            <button class="btn btn-sm btn-outline-primary btn-eval-umo" data-umo="${safeUmo}">
                                检查
                            </button>
                            <button class="btn btn-sm ${s.paused ? 'btn-primary' : 'btn-secondary'} btn-pause-umo" data-umo="${safeUmo}" data-paused="${s.paused ? 'true' : 'false'}">
                                ${s.paused ? '恢复' : '暂停'}
                            </button>
                        </div>
                    </td>
                </tr>
            `;
        });

        tbody.innerHTML = html;

        tbody.querySelectorAll(".btn-eval-umo").forEach(btn => {
            btn.addEventListener("click", async () => {
                const umo = btn.getAttribute("data-umo");
                try {
                    let res;
                    if (bridge && typeof bridge.apiPost === "function") {
                        res = await bridge.apiPost("trigger_eval", { umo });
                    } else {
                        const response = await fetch("/astrbot_plugin_meta_governor/trigger_eval", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ umo }),
                        });
                        res = await response.json();
                    }
                    if (res && (res.status === "success" || res.code === 200)) {
                        showToast(`已成功为聊天窗口 [${umo}] 发起质量检查！`);
                        setTimeout(() => loadData(false), 2000);
                    }
                } catch (e) {
                    showToast("检查失败: " + e.message, true);
                }
            });
        });

        tbody.querySelectorAll(".btn-pause-umo").forEach(btn => {
            btn.addEventListener("click", async () => {
                const umo = btn.getAttribute("data-umo");
                const currentlyPaused = btn.getAttribute("data-paused") === "true";
                try {
                    let res;
                    if (bridge && typeof bridge.apiPost === "function") {
                        res = await bridge.apiPost("toggle_pause", { umo, pause: !currentlyPaused });
                    } else {
                        const response = await fetch("/astrbot_plugin_meta_governor/toggle_pause", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ umo, pause: !currentlyPaused }),
                        });
                        res = await response.json();
                    }
                    if (res && (res.status === "success" || res.code === 200)) {
                        showToast(`已${!currentlyPaused ? '暂停' : '恢复'}会话 [${umo}] 的调优！`);
                        await loadData(false);
                    }
                } catch (e) {
                    showToast("操作失败: " + e.message, true);
                }
            });
        });
    }

    function renderLogsTable(sessions) {
        const tbody = document.getElementById("logs-body");
        let allLogs = [];

        Object.entries(sessions).forEach(([umo, s]) => {
            if (s.eval_history) {
                s.eval_history.forEach(log => {
                    allLogs.push({ umo, log });
                });
            }
        });

        allLogs.sort((a, b) => (b.log.timestamp || 0) - (a.log.timestamp || 0));

        if (allLogs.length === 0) {
            tbody.innerHTML = `<tr><td colspan="4" class="table-empty">暂无历史检查记录。</td></tr>`;
            return;
        }

        let html = "";
        allLogs.slice(0, 20).forEach(item => {
            const l = item.log;
            const safeTime = formatTime(l.timestamp);
            const safeRound = escapeHtml(l.round || 0);
            const safeReason = l.reason ? escapeHtml(l.reason) : '-';
            const safeSummary = l.verdict_summary ? escapeHtml(l.verdict_summary) : '-';
            html += `
                <tr>
                    <td>${safeTime}</td>
                    <td>第 ${safeRound} 轮</td>
                    <td><code>${safeReason}</code></td>
                    <td>${safeSummary}</td>
                </tr>
            `;
        });

        tbody.innerHTML = html;
    }

    function renderConstraintsTable(constraints) {
        const tbody = document.getElementById("constraints-body");
        if (!constraints || constraints.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="table-empty">暂无检查规则。</td></tr>`;
            return;
        }

        let html = "";
        constraints.forEach(c => {
            const m = c.mode || "auto";
            const modeBadge = m === "once"
                ? `<span class="badge badge-purple">一次性</span>`
                : (m === "multi" ? `<span class="badge badge-info">多轮</span>` : `<span class="badge badge-success">自动</span>`);

            const safeCId = escapeHtml(c.id);
            const safeCName = escapeHtml(c.name);
            const safeCDesc = c.description ? escapeHtml(c.description) : '-';

            html += `
                <tr>
                    <td><code>${safeCId}</code></td>
                    <td><strong>${safeCName}</strong></td>
                    <td>${modeBadge}</td>
                    <td style="max-width: 440px;">${safeCDesc}</td>
                    <td>
                        <div style="display: flex; gap: 8px;">
                            <button class="btn btn-sm btn-outline-primary btn-edit-c" data-id="${safeCId}">编辑</button>
                            <button class="btn btn-sm btn-outline-danger btn-del-c" data-id="${safeCId}">删除</button>
                        </div>
                    </td>
                </tr>
            `;
        });

        tbody.innerHTML = html;

        tbody.querySelectorAll(".btn-edit-c").forEach(btn => {
            btn.addEventListener("click", () => {
                const cId = btn.getAttribute("data-id");
                const target = constraints.find(x => x.id === cId);
                if (target) openConstraintModal(target);
            });
        });

        tbody.querySelectorAll(".btn-del-c").forEach(btn => {
            btn.addEventListener("click", () => {
                const cId = btn.getAttribute("data-id");
                confirmDelete(`确定要删除规则 [${cId}] 吗？`, async () => {
                    try {
                        let res;
                        if (bridge && typeof bridge.apiPost === "function") {
                            res = await bridge.apiPost("delete_constraint", { id: cId });
                        } else {
                            const response = await fetch("/astrbot_plugin_meta_governor/delete_constraint", {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ id: cId }),
                            });
                            res = await response.json();
                        }
                        if (res && (res.status === "success" || res.code === 200)) {
                            showToast("成功删除规则！");
                            await loadData(false);
                        }
                    } catch (e) {
                        showToast("删除失败: " + e.message, true);
                    }
                });
            });
        });
    }

    function renderBannedPhrases(phrases) {
        const container = document.getElementById("banned-phrases-list");
        if (!phrases || phrases.length === 0) {
            container.innerHTML = `<span style="color: var(--text-secondary); font-size: 13px;">暂无硬性禁用词。</span>`;
            return;
        }

        let html = "";
        phrases.forEach(p => {
            const safePhrase = escapeHtml(p);
            html += `
                <span class="tag-item">
                    <span>${safePhrase}</span>
                    <span class="tag-remove" data-phrase="${safePhrase}">×</span>
                </span>
            `;
        });

        container.innerHTML = html;

        container.querySelectorAll(".tag-remove").forEach(btn => {
            btn.addEventListener("click", async () => {
                const phrase = btn.getAttribute("data-phrase");
                try {
                    let res;
                    if (bridge && typeof bridge.apiPost === "function") {
                        res = await bridge.apiPost("delete_banned_phrase", { phrase });
                    } else {
                        const response = await fetch("/astrbot_plugin_meta_governor/delete_banned_phrase", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ phrase }),
                        });
                        res = await response.json();
                    }
                    if (res && (res.status === "success" || res.code === 200)) {
                        showToast(`已删除禁用词: '${phrase}'`);
                        await loadData(false);
                    }
                } catch (e) {
                    showToast("删除失败: " + e.message, true);
                }
            });
        });
    }

    function renderSettingsForm(config) {
        document.getElementById("cfg-enable").checked = config.enable !== false;
        document.getElementById("cfg-k-rounds").value = config.k_rounds || 5;
        document.getElementById("cfg-n-context-rounds").value = config.n_context_rounds || 3;
        document.getElementById("cfg-ttl-rounds").value = config.ttl_rounds || 10;
        document.getElementById("cfg-max-active").value = config.max_active_directives || 3;
        
        setCustomSelectValue("select-supervisor-provider", config.supervisor_provider_id || "");
        const promptInput = document.getElementById("cfg-supervisor-prompt");
        if (promptInput) {
            promptInput.placeholder = DEFAULT_SUPERVISOR_PROMPT_TEMPLATE;
            promptInput.value = config.supervisor_prompt_template || "";
        }

        document.getElementById("cfg-sentinel-heuristics").checked = config.enable_sentinel_heuristics !== false;
        setCustomSelectValue("select-sentinel-sensitivity", config.sentinel_sensitivity || "medium");
        
        document.getElementById("cfg-verbose").checked = !!config.verbose;
        document.getElementById("cfg-reset-clear").checked = !!config.reset_clear || !!config.clear_directives_on_reset;
    }

    bindSelectOptionClick("select-sentinel-sensitivity");
    bindSelectOptionClick("select-constraint-mode");

    // Initial Load & Auto Refresh Timer (4s)
    await loadData(true);
    setInterval(() => loadData(false), 4000);
}

document.addEventListener("DOMContentLoaded", init);
