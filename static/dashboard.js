(() => {
    let socket = null;
    let reconnectTimer = null;

    function setText(id, value) {
        if (value === null || value === undefined) {
            return;
        }

        const element = document.getElementById(id);

        if (element) {
            element.textContent = value;
        }
    }

    function renderPositions(positions) {
        const body =
            document.getElementById("position-body");

        if (!body) {
            return;
        }

        body.innerHTML = "";

        if (!positions || positions.length === 0) {
            const row = document.createElement("tr");
            const cell = document.createElement("td");

            cell.colSpan = 6;
            cell.className = "position-empty";
            cell.textContent = "현재 보유 포지션이 없습니다.";

            row.appendChild(cell);
            body.appendChild(row);
            return;
        }

        for (const position of positions) {
            const row = document.createElement("tr");

            const values = [
                position.symbol ?? "-",
                position.side === "Buy"
                    ? "LONG"
                    : position.side === "Sell"
                        ? "SHORT"
                        : position.side ?? "-",
                position.size ?? "-",
                position.avgPrice ?? "-",
                position.lastPrice ?? "-",
                position.unrealisedPnl ?? "-",
            ];

            values.forEach((value, index) => {
                const cell =
                    document.createElement("td");

                cell.textContent = value;

                if (index === 1) {
                    if (position.side === "Buy") {
                        cell.className =
                            "position-side position-long";
                    } else if (
                        position.side === "Sell"
                    ) {
                        cell.className =
                            "position-side position-short";
                    }
                }

                if (index === 5) {
                    const pnl = Number(
                        position.unrealisedPnl
                    );

                    if (Number.isFinite(pnl)) {
                        if (pnl > 0) {
                            cell.className =
                                "position-pnl position-profit";
                        } else if (pnl < 0) {
                            cell.className =
                                "position-pnl position-loss";
                        } else {
                            cell.className =
                                "position-pnl";
                        }
                    }
                }

                row.appendChild(cell);
            });

            body.appendChild(row);
        }
    }

    function connect() {
        const protocol =
            window.location.protocol === "https:"
                ? "wss:"
                : "ws:";

        socket = new WebSocket(
            `${protocol}//${window.location.host}/ws`
        );

        socket.addEventListener("open", () => {
            console.log("Realtime WebSocket connected");
        });

        socket.addEventListener("message", (event) => {
            const data = JSON.parse(event.data);

            if (data.type !== "realtime") {
                return;
            }

            const wallet = data.wallet || {};

            setText(
                "wallet-equity",
                wallet.equity
            );

            setText(
                "wallet-wallet",
                wallet.wallet
            );

            setText(
                "wallet-available",
                wallet.available
            );

            // UPL은 ticker 기반 실시간 계산값.
            setText(
                "wallet-unrealised",
                data.unrealised
            );

            const walletUnrealised =
                document.getElementById(
                    "wallet-unrealised"
                );

            if (walletUnrealised) {
                const upl =
                    Number(data.unrealised);

                walletUnrealised.classList.remove(
                    "wallet-pnl-profit",
                    "wallet-pnl-loss",
                    "wallet-pnl-flat"
                );

                if (Number.isFinite(upl)) {
                    if (upl > 0) {
                        walletUnrealised.classList.add(
                            "wallet-pnl-profit"
                        );
                    } else if (upl < 0) {
                        walletUnrealised.classList.add(
                            "wallet-pnl-loss"
                        );
                    } else {
                        walletUnrealised.classList.add(
                            "wallet-pnl-flat"
                        );
                    }
                } else {
                    walletUnrealised.classList.add(
                        "wallet-pnl-flat"
                    );
                }
            }

            renderPositions(
                data.positions || []
            );

        });

        socket.addEventListener("close", () => {
            console.log("Realtime WebSocket disconnected");

            clearTimeout(reconnectTimer);

            reconnectTimer = setTimeout(
                connect,
                2000
            );
        });

        socket.addEventListener("error", () => {
            socket.close();
        });
    }

    connect();
})();


/* ============================================================
 * Surge settings collapse / expand
 * ============================================================ */
(() => {
    const toggle = document.getElementById(
        "surge-settings-toggle"
    );
    const panel = document.getElementById(
        "surge-settings-panel"
    );
    const icon = document.getElementById(
        "surge-settings-toggle-icon"
    );

    if (!toggle || !panel) {
        return;
    }

    toggle.addEventListener("click", () => {
        const willOpen = panel.hidden;

        panel.hidden = !willOpen;
        toggle.setAttribute(
            "aria-expanded",
            willOpen ? "true" : "false"
        );

        if (icon) {
            icon.textContent = willOpen ? "▲" : "▼";
        }
    });
})();


/* ============================================================
 * Surge trading settings UI
 * ============================================================ */
(() => {
    const byId = (id) => document.getElementById(id);

    const sourceLabel = {
        symbol: "종목",
        stage: "차수",
        global: "기본",
    };

    function showMessage(message, isError = false) {
        const box = byId("surge-message");

        if (!box) {
            return;
        }

        box.hidden = false;
        box.textContent = message;
        box.classList.toggle(
            "surge-message-error",
            Boolean(isError)
        );

        clearTimeout(showMessage.timer);

        showMessage.timer = setTimeout(() => {
            box.hidden = true;
        }, 3000);
    }

    async function postForm(path, data) {
        const body = new URLSearchParams();

        Object.entries(data).forEach(([key, value]) => {
            body.set(key, String(value));
        });

        const response = await fetch(path, {
            method: "POST",
            headers: {
                "Content-Type":
                    "application/x-www-form-urlencoded",
            },
            body,
        });

        const result = await response.json();

        if (!result.ok) {
            throw new Error(
                result.error || "저장에 실패했습니다."
            );
        }

        return result;
    }

    function renderStages(stages) {
        const grid = byId("surge-stage-grid");

        if (!grid) {
            return;
        }

        grid.innerHTML = "";

        for (const row of stages || []) {
            const stageNumber =
                Number(row.stage);

            // 첫 등장은 위의 기본 진입 비중을 사용한다.
            // 별도의 1차 설정 카드는 필요하지 않다.
            if (stageNumber === 1) {
                continue;
            }

            const card = document.createElement("div");
            card.className = "surge-stage-card";

            const title = document.createElement("div");
            title.className = "surge-stage-title";
            title.textContent =
                stageNumber >= 5
                    ? "5차 이후 진입"
                    : `${stageNumber}차 진입`;

            const percentWrap =
                document.createElement("div");

            percentWrap.className = "percent-input";

            const input = document.createElement("input");
            input.type = "number";
            input.min = "0";
            input.max = "1000";
            input.step = "0.1";
            input.value = row.entry_percent;

            const percentMark =
                document.createElement("span");

            percentMark.textContent = "%";

            percentWrap.append(
                input,
                percentMark
            );

            const globalLabel =
                document.createElement("label");

            globalLabel.className =
                "surge-global-checkbox";

            const checkbox =
                document.createElement("input");

            checkbox.type = "checkbox";
            checkbox.checked =
                Boolean(row.use_global);

            const checkboxText =
                document.createElement("span");

            checkboxText.textContent =
                "기본 비중 사용";

            globalLabel.append(
                checkbox,
                checkboxText
            );

            const button =
                document.createElement("button");

            button.type = "button";
            button.className =
                "surge-save-button";

            button.textContent = "저장";

            button.addEventListener(
                "click",
                async () => {
                    try {
                        const value =
                            Number(input.value);

                        if (
                            !Number.isFinite(value) ||
                            value < 0 ||
                            value > 1000
                        ) {
                            throw new Error(
                                "비중은 0~1000% 사이여야 합니다."
                            );
                        }

                        await postForm(
                            "/api/surge/stage",
                            {
                                stage: row.stage,
                                entry_percent: value,
                                use_global:
                                    checkbox.checked
                                        ? 1
                                        : 0,
                            }
                        );

                        const stageLabel =
                            stageNumber >= 5
                                ? "5차 이후"
                                : `${stageNumber}차`;

                        showMessage(
                            `${stageLabel} 설정을 저장했습니다.`
                        );

                        await loadSettings();
                    } catch (error) {
                        showMessage(
                            error.message,
                            true
                        );
                    }
                }
            );

            card.append(
                title,
                percentWrap,
                globalLabel,
                button
            );

            grid.appendChild(card);
        }
    }

    function renderSymbols(symbols) {
        const body = byId("surge-symbol-body");

        if (!body) {
            return;
        }

        body.innerHTML = "";

        if (!symbols || symbols.length === 0) {
            const row = document.createElement("tr");

            const cell = document.createElement("td");
            cell.colSpan = 7;
            cell.className = "surge-loading";
            cell.textContent =
                "급등 신호 종목이 없습니다.";

            row.appendChild(cell);
            body.appendChild(row);
            return;
        }

        for (const item of symbols) {
            const row = document.createElement("tr");

            const symbolCell =
                document.createElement("td");

            symbolCell.textContent =
                item.symbol || "-";

            const countCell =
                document.createElement("td");

            countCell.textContent =
                item.signal_count ??
                item.current_stage ??
                0;

            const stageCell =
                document.createElement("td");

            stageCell.textContent =
                item.next_stage ?? "-";

            const percentCell =
                document.createElement("td");

            const percentValue =
                item.next_entry_percent ?? 0;

            const notionalValue =
                Number(item.next_entry_notional);

            if (
                item.next_entry_notional !== null &&
                item.next_entry_notional !== undefined &&
                Number.isFinite(notionalValue)
            ) {
                percentCell.textContent =
                    `${percentValue}% · ` +
                    `${notionalValue.toFixed(2)} USDT`;
            } else {
                percentCell.textContent =
                    `${percentValue}% · 명목가치 계산 불가`;
            }

            const sourceCell =
                document.createElement("td");

            sourceCell.textContent =
                sourceLabel[
                    item.next_entry_percent_source
                ] ||
                item.next_entry_percent_source ||
                "-";

            const inputCell =
                document.createElement("td");

            const input =
                document.createElement("input");

            input.type = "number";
            input.min = "0";
            input.max = "1000";
            input.step = "0.1";
            input.placeholder = "자동";
            input.className =
                "surge-symbol-input";

            if (
                item.entry_percent !== null &&
                item.entry_percent !== undefined
            ) {
                input.value =
                    item.entry_percent;
            }

            inputCell.appendChild(input);

            const actionCell =
                document.createElement("td");

            const button =
                document.createElement("button");

            button.type = "button";
            button.className =
                "surge-save-button";

            button.textContent = "저장";

            const inlineMessage =
                document.createElement("div");

            inlineMessage.className =
                "surge-symbol-message";

            inlineMessage.hidden = true;

            let inlineMessageTimer = null;

            const showSymbolMessage = (
                message,
                isError = false
            ) => {
                inlineMessage.textContent = message;
                inlineMessage.hidden = false;

                inlineMessage.classList.toggle(
                    "surge-symbol-message-error",
                    Boolean(isError)
                );

                clearTimeout(inlineMessageTimer);

                inlineMessageTimer = setTimeout(
                    () => {
                        inlineMessage.hidden = true;
                    },
                    3000
                );
            };

            button.addEventListener(
                "click",
                async () => {
                    try {
                        const raw =
                            input.value.trim();

                        if (raw !== "") {
                            const value =
                                Number(raw);

                            if (
                                !Number.isFinite(value) ||
                                value < 0 ||
                                value > 1000
                            ) {
                                throw new Error(
                                    "종목 비중은 0~1000% 사이여야 합니다."
                                );
                            }
                        }

                        await postForm(
                            "/api/surge/symbol",
                            {
                                symbol:
                                    item.symbol,
                                entry_percent:
                                    raw,
                            }
                        );

                        showSymbolMessage(
                            raw === ""
                                ? "기본 규칙 적용"
                                : "저장됨"
                        );

                        setTimeout(
                            () => {
                                loadSettings().catch(
                                    (error) => {
                                        showMessage(
                                            error.message,
                                            true
                                        );
                                    }
                                );
                            },
                            700
                        );
                    } catch (error) {
                        showSymbolMessage(
                            error.message,
                            true
                        );
                    }
                }
            );

            actionCell.append(
                button,
                inlineMessage
            );

            row.append(
                symbolCell,
                countCell,
                stageCell,
                percentCell,
                sourceCell,
                inputCell,
                actionCell
            );

            body.appendChild(row);
        }
    }

    async function loadSettings() {
        const response = await fetch(
            "/api/surge/settings",
            {
                cache: "no-store",
            }
        );

        const data = await response.json();

        if (!data.ok) {
            throw new Error(
                data.error ||
                "급등매매 설정을 불러오지 못했습니다."
            );
        }

        const settings = data.settings || {};
        const trailing = data.trailing || {};
        const symbols = data.symbols || [];

        const enabled = byId("surge-enabled");
        const enabledText =
            byId("surge-enabled-text");

        if (enabled) {
            enabled.checked =
                Boolean(settings.enabled);
        }

        if (enabledText) {
            enabledText.textContent =
                settings.enabled
                    ? "ON"
                    : "OFF";
        }

        const globalPercent =
            byId("surge-global-percent");

        if (globalPercent) {
            globalPercent.value =
                settings.entry_percent ?? 100;
        }

        const globalNotional =
            byId("surge-global-notional");

        if (globalNotional) {
            const available =
                symbols.length > 0
                    ? Number(symbols[0].available)
                    : NaN;

            const percent =
                Number(settings.entry_percent ?? 0);

            if (
                Number.isFinite(available) &&
                Number.isFinite(percent)
            ) {
                const notional =
                    available * percent / 100;

                globalNotional.textContent =
                    `예상 명목가치: ` +
                    `${notional.toFixed(2)} USDT`;
            } else {
                globalNotional.textContent =
                    "예상 명목가치: 계산 불가";
            }
        }

        const arm =
            byId("surge-arm-percent");

        if (arm) {
            arm.value =
                trailing.arm_percent ?? 0.5;
        }

        const gap =
            byId("surge-gap-percent");

        if (gap) {
            gap.value =
                trailing.gap_percent ?? 0.5;
        }

        renderStages(data.stages || []);
        renderSymbols(symbols);
    }

    function bindEvents() {
        const enabled = byId("surge-enabled");

        if (enabled) {
            enabled.addEventListener(
                "change",
                async () => {
                    const desired =
                        enabled.checked;

                    try {
                        await postForm(
                            "/api/surge/enabled",
                            {
                                enabled:
                                    desired ? 1 : 0,
                            }
                        );

                        const text =
                            byId(
                                "surge-enabled-text"
                            );

                        if (text) {
                            text.textContent =
                                desired
                                    ? "ON"
                                    : "OFF";
                        }

                        showMessage(
                            desired
                                ? "급등매매를 활성화했습니다."
                                : "급등매매를 비활성화했습니다."
                        );
                    } catch (error) {
                        enabled.checked =
                            !desired;

                        showMessage(
                            error.message,
                            true
                        );
                    }
                }
            );
        }

        const globalSave =
            byId("surge-global-save");

        if (globalSave) {
            globalSave.addEventListener(
                "click",
                async () => {
                    try {
                        const input =
                            byId(
                                "surge-global-percent"
                            );

                        const value =
                            Number(input.value);

                        if (
                            !Number.isFinite(value) ||
                            value < 0 ||
                            value > 1000
                        ) {
                            throw new Error(
                                "기본 비중은 0~1000% 사이여야 합니다."
                            );
                        }

                        await postForm(
                            "/api/surge/global",
                            {
                                entry_percent:
                                    value,
                            }
                        );

                        showMessage(
                            "기본 진입 비중을 저장했습니다."
                        );

                        await loadSettings();
                    } catch (error) {
                        showMessage(
                            error.message,
                            true
                        );
                    }
                }
            );
        }

        const trailingSave =
            byId("surge-trailing-save");

        if (trailingSave) {
            trailingSave.addEventListener(
                "click",
                async () => {
                    try {
                        const arm =
                            Number(
                                byId(
                                    "surge-arm-percent"
                                ).value
                            );

                        const gap =
                            Number(
                                byId(
                                    "surge-gap-percent"
                                ).value
                            );

                        if (
                            !Number.isFinite(arm) ||
                            !Number.isFinite(gap) ||
                            arm < 0 ||
                            gap < 0
                        ) {
                            throw new Error(
                                "트레일링 값은 0 이상이어야 합니다."
                            );
                        }

                        await postForm(
                            "/api/surge/trailing",
                            {
                                arm_percent: arm,
                                gap_percent: gap,
                            }
                        );

                        showMessage(
                            "트레일링 설정을 저장했습니다."
                        );

                        await loadSettings();
                    } catch (error) {
                        showMessage(
                            error.message,
                            true
                        );
                    }
                }
            );
        }
    }

    async function start() {
        if (!byId("surge-enabled")) {
            return;
        }

        bindEvents();

        try {
            await loadSettings();
        } catch (error) {
            showMessage(
                error.message,
                true
            );
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            start
        );
    } else {
        start();
    }
})();
/* ============================================================
 * Recent surge Telegram messages
 * ============================================================ */
(() => {
    const container =
        document.getElementById(
            "surge-recent-messages"
        );

    if (!container) {
        return;
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function formatTime(value) {
        if (!value) {
            return "-";
        }

        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return value;
        }

        return date.toLocaleString(
            "ko-KR",
            {
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                hour12: false,
            }
        );
    }

    function render(messages) {
        if (
            !Array.isArray(messages) ||
            messages.length === 0
        ) {
            container.innerHTML = `
                <div class="surge-recent-empty">
                    표시할 급등 메시지가 없습니다.
                </div>
            `;
            return;
        }

        container.innerHTML =
            messages.map((row) => {
                const symbol =
                    escapeHtml(row.symbol || "-");

                const stage =
                    Number(row.signal_stage || 0);

                const time =
                    escapeHtml(
                        formatTime(
                            row.message_time_iso
                        )
                    );

                const body =
                    escapeHtml(row.text || "");

                return `
                    <article class="surge-recent-item">
                        <div class="surge-recent-meta">
                            <strong
                                class="surge-recent-symbol"
                            >
                                #${symbol}
                            </strong>

                            <span>
                                ${stage}차
                            </span>

                            <span>
                                ${time}
                            </span>
                        </div>

                        <div class="surge-recent-text">${body}</div>
                    </article>
                `;
            }).join("");
    }

    async function loadRecentSurgeMessages() {
        try {
            const response = await fetch(
                "/api/surge/recent-messages",
                {
                    cache: "no-store",
                }
            );

            const data = await response.json();

            if (!data.ok) {
                throw new Error(
                    data.error ||
                    "recent message load failed"
                );
            }

            render(data.messages || []);

        } catch (error) {
            console.error(
                "Recent surge messages:",
                error
            );

            container.innerHTML = `
                <div class="surge-recent-empty">
                    최근 메시지를 불러오지 못했습니다.
                </div>
            `;
        }
    }

    loadRecentSurgeMessages();
})();
