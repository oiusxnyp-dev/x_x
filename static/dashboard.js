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
