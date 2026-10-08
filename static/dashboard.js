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
            input.max = "5000";
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
                            value > 5000
                        ) {
                            throw new Error(
                                "비중은 0~5000% 사이여야 합니다."
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
            input.max = "5000";
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
                                value > 5000
                            ) {
                                throw new Error(
                                    "종목 비중은 0~5000% 사이여야 합니다."
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
                            value > 5000
                        ) {
                            throw new Error(
                                "기본 비중은 0~5000% 사이여야 합니다."
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
 * Telegram message event feed
 * ============================================================ */
(() => {
    const container =
        document.getElementById(
            "surge-recent-messages"
        );

    if (!container) {
        return;
    }

    const PAGE_SIZE = 12;
    const POLL_MS = 1000;

    let nextCursor = null;
    let hasMore = true;
    let loadingOlder = false;
    let polling = false;
    let initialized = false;

    const knownKeys = new Set();

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function eventKey(row) {
        if (
            row.source === "history" &&
            row.version_id
        ) {
            return (
                "history:" +
                String(row.version_id)
            );
        }

        return [
            row.source || "legacy",
            row.chat_id,
            row.message_id,
            row.event_type || "NEW",
        ].join(":");
    }

    function eventTime(row) {
        if (
            row.source === "history" &&
            row.event_type === "EDIT"
        ) {
            return (
                row.edit_time_iso ||
                row.message_time_iso
            );
        }

        return row.message_time_iso;
    }

    function formatKst(value) {
        if (!value) {
            return "-";
        }

        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value);
        }

        const parts =
            new Intl.DateTimeFormat(
                "ko-KR",
                {
                    timeZone: "Asia/Seoul",
                    year: "numeric",
                    month: "2-digit",
                    day: "2-digit",
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                    hour12: false,
                }
            ).formatToParts(date);

        const values = {};

        for (const part of parts) {
            values[part.type] = part.value;
        }

        const tenth =
            Math.floor(
                date.getMilliseconds() / 100
            );

        return (
            `${values.year}.${values.month}.` +
            `${values.day} ` +
            `${values.hour}:${values.minute}:` +
            `${values.second}.${tenth}`
        );
    }

    function channelName(row) {
        if (row.chat_name) {
            return row.chat_name;
        }

        if (
            Number(row.chat_id) ===
            -1004442764226
        ) {
            return "멍꿀단 : 정보통";
        }

        if (
            Number(row.chat_id) ===
            -1003849484550
        ) {
            return "(구)멍꿀단 : 정보통";
        }

        return String(row.chat_id || "-");
    }

    function senderText(row) {
        const channel =
            channelName(row);

        const sender =
            String(
                row.sender_name || ""
            ).trim();

        const username =
            String(
                row.sender_username || ""
            ).trim();

        if (
            sender &&
            sender !== channel
        ) {
            return (
                username
                    ? `${sender} (@${username})`
                    : sender
            );
        }

        if (username) {
            return `@${username}`;
        }

        return "";
    }

    function messageLabel(row) {
        const id =
            escapeHtml(row.message_id);

        if (
            row.event_type === "EDIT" &&
            row.source === "history"
        ) {
            return `${id}번 메시지 수정 데이터`;
        }

        return `${id}번 메시지`;
    }

    function renderCard(row) {
        const surge =
            Boolean(row.is_surge);

        const edit =
            (
                row.event_type === "EDIT" &&
                row.source === "history"
            );

        const legacyEdited =
            Boolean(
                row.legacy_was_edited
            );

        const classes = [
            "surge-recent-item",
        ];

        if (surge) {
            classes.push(
                "surge-recent-item--surge"
            );
        }

        if (edit) {
            classes.push(
                "surge-recent-item--edit"
            );
        }

        const channel =
            escapeHtml(
                channelName(row)
            );

        const sender =
            senderText(row);

        const time =
            escapeHtml(
                formatKst(
                    eventTime(row)
                )
            );

        const label =
            escapeHtml(
                messageLabel(row)
            );

        const body =
            escapeHtml(
                row.text || ""
            );

        const badges = [];

        if (surge) {
            badges.push(
                '<span class="surge-feed-badge surge-feed-badge--surge">OI 급등</span>'
            );
        }

        if (edit) {
            badges.push(
                '<span class="surge-feed-badge surge-feed-badge--edit">수정 데이터</span>'
            );
        } else if (legacyEdited) {
            badges.push(
                '<span class="surge-feed-badge surge-feed-badge--legacy-edit">수정됨</span>'
            );
        }

        if (
            row.media_type ||
            row.has_media
        ) {
            badges.push(
                '<span class="surge-feed-badge">사진/미디어</span>'
            );
        }

        let senderHtml = "";

        if (sender) {
            senderHtml = `
                <span class="surge-feed-sender">
                    보낸이 · ${escapeHtml(sender)}
                </span>
            `;
        }

        let editNote = "";

        if (legacyEdited) {
            editNote = `
                <div class="surge-feed-edit-note">
                    과거 데이터는 수정 전 원문이
                    보존되어 있지 않습니다.
                </div>
            `;
        }

        let album = "";

        if (row.grouped_id) {
            album = `
                <span class="surge-feed-album">
                    앨범 · ${escapeHtml(row.grouped_id)}
                </span>
            `;
        }

        let mediaHtml = "";

        if (row.media_path) {
            const mediaUrl =
                escapeHtml(
                    row.media_path
                );

            mediaHtml = `
                <button
                    class="surge-feed-photo-button"
                    type="button"
                    data-surge-photo="${mediaUrl}"
                    aria-label="사진 크게 보기"
                >
                    <img
                        class="surge-feed-photo"
                        src="${mediaUrl}"
                        alt="${escapeHtml(
                            row.message_id
                        )}번 메시지 사진"
                        loading="lazy"
                    >
                </button>
            `;
        }

        let emptyBody = body;

        if (!emptyBody) {
            if (
                row.media_type ||
                row.has_media
            ) {
                emptyBody =
                    '<span class="surge-feed-muted">미디어 메시지</span>';
            } else {
                emptyBody =
                    '<span class="surge-feed-muted">본문 없음</span>';
            }
        }

        return `
            <article
                class="${classes.join(" ")}"
                data-event-key="${escapeHtml(eventKey(row))}"
            >
                <div class="surge-feed-topline">
                    <strong class="surge-feed-message-id">
                        ${label}
                    </strong>

                    <div class="surge-feed-badges">
                        ${badges.join("")}
                    </div>
                </div>

                <div class="surge-recent-meta">
                    <span>
                        ${channel}
                    </span>

                    ${senderHtml}

                    <span>
                        ${edit ? "수정" : "게시"} ·
                        ${time} KST
                    </span>

                    ${album}
                </div>

                ${editNote}

                ${mediaHtml}

                <div class="surge-recent-text">${emptyBody}</div>
            </article>
        `;
    }

    function makeFragment(messages) {
        const template =
            document.createElement(
                "template"
            );

        template.innerHTML =
            messages
                .map(renderCard)
                .join("");

        return template.content;
    }

    function remember(messages) {
        for (const row of messages) {
            knownKeys.add(
                eventKey(row)
            );
        }
    }

    function clearLoadingMessage() {
        const empty =
            container.querySelector(
                ".surge-recent-empty"
            );

        if (empty) {
            empty.remove();
        }
    }

    async function fetchPage(before=null) {
        const params =
            new URLSearchParams();

        params.set(
            "limit",
            String(PAGE_SIZE)
        );

        if (before) {
            params.set(
                "before",
                before
            );
        }

        const response = await fetch(
            "/api/surge/recent-messages?" +
            params.toString(),
            {
                cache: "no-store",
            }
        );

        const data =
            await response.json();

        if (!data.ok) {
            throw new Error(
                data.error ||
                "message feed load failed"
            );
        }

        return data;
    }

    async function initialLoad() {
        try {
            const data =
                await fetchPage();

            const messages =
                Array.isArray(data.messages)
                    ? data.messages
                    : [];

            container.innerHTML = "";

            if (!messages.length) {
                container.innerHTML = `
                    <div class="surge-recent-empty">
                        표시할 메시지가 없습니다.
                    </div>
                `;
            } else {
                container.appendChild(
                    makeFragment(messages)
                );

                remember(messages);
            }

            nextCursor =
                data.next_cursor || null;

            hasMore =
                Boolean(data.has_more);

            initialized = true;

        } catch (error) {
            console.error(
                "Telegram message feed:",
                error
            );

            container.innerHTML = `
                <div class="surge-recent-empty">
                    메시지를 불러오지 못했습니다.
                </div>
            `;
        }
    }

    async function loadOlder() {
        if (
            !initialized ||
            loadingOlder ||
            !hasMore ||
            !nextCursor
        ) {
            return;
        }

        loadingOlder = true;

        try {
            const data =
                await fetchPage(
                    nextCursor
                );

            const messages =
                (
                    Array.isArray(data.messages)
                        ? data.messages
                        : []
                ).filter(
                    (row) =>
                        !knownKeys.has(
                            eventKey(row)
                        )
                );

            if (messages.length) {
                clearLoadingMessage();

                container.appendChild(
                    makeFragment(messages)
                );

                remember(messages);
            }

            nextCursor =
                data.next_cursor || null;

            hasMore =
                Boolean(data.has_more);

        } catch (error) {
            console.error(
                "Older Telegram messages:",
                error
            );
        } finally {
            loadingOlder = false;
        }
    }

    async function pollNewest() {
        if (
            !initialized ||
            polling
        ) {
            return;
        }

        polling = true;

        try {
            const data =
                await fetchPage();

            const messages =
                Array.isArray(data.messages)
                    ? data.messages
                    : [];

            const fresh =
                messages.filter(
                    (row) =>
                        !knownKeys.has(
                            eventKey(row)
                        )
                );

            if (!fresh.length) {
                return;
            }

            clearLoadingMessage();

            const oldScrollLeft =
                container.scrollLeft;

            container.prepend(
                makeFragment(fresh)
            );

            remember(fresh);

            // 사용자가 과거 메시지를 보고 있으면
            // 새 카드가 들어와도 현재 위치를 최대한 유지한다.
            if (oldScrollLeft > 20) {
                requestAnimationFrame(
                    () => {
                        const addedWidth =
                            Array.from(
                                container.children
                            )
                            .slice(
                                0,
                                fresh.length
                            )
                            .reduce(
                                (total, node) =>
                                    total +
                                    node.getBoundingClientRect()
                                        .width +
                                    12,
                                0
                            );

                        container.scrollLeft =
                            oldScrollLeft +
                            addedWidth;
                    }
                );
            } else {
                container.scrollLeft = 0;
            }

        } catch (error) {
            console.error(
                "Telegram message poll:",
                error
            );
        } finally {
            polling = false;
        }
    }

    let photoModal = null;

    function closePhotoModal() {
        if (!photoModal) {
            return;
        }

        photoModal.remove();
        photoModal = null;

        document.body.classList.remove(
            "surge-photo-modal-open"
        );
    }

    function openPhotoModal(src) {
        closePhotoModal();

        const overlay =
            document.createElement(
                "div"
            );

        overlay.className =
            "surge-photo-modal";

        overlay.innerHTML = `
            <button
                class="surge-photo-modal-close"
                type="button"
                aria-label="사진 닫기"
            >
                ×
            </button>

            <img
                class="surge-photo-modal-image"
                src="${escapeHtml(src)}"
                alt="텔레그램 사진 확대"
            >
        `;

        overlay.addEventListener(
            "click",
            (event) => {
                if (
                    event.target === overlay ||
                    event.target.closest(
                        ".surge-photo-modal-close"
                    )
                ) {
                    closePhotoModal();
                }
            }
        );

        document.body.appendChild(
            overlay
        );

        document.body.classList.add(
            "surge-photo-modal-open"
        );

        photoModal = overlay;
    }

    container.addEventListener(
        "click",
        (event) => {
            const button =
                event.target.closest(
                    "[data-surge-photo]"
                );

            if (!button) {
                return;
            }

            const src =
                button.getAttribute(
                    "data-surge-photo"
                );

            if (src) {
                openPhotoModal(src);
            }
        }
    );

    document.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key === "Escape"
            ) {
                closePhotoModal();
            }
        }
    );

    container.addEventListener(
        "scroll",
        () => {
            const remaining =
                container.scrollWidth -
                container.clientWidth -
                container.scrollLeft;

            if (remaining < 500) {
                loadOlder();
            }
        },
        {
            passive: true,
        }
    );

    initialLoad().then(() => {
        window.setInterval(
            pollNewest,
            POLL_MS
        );
    });
})();
