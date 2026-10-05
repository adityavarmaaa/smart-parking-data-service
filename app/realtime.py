import asyncio
import os
from contextlib import suppress

from fastapi import WebSocket, WebSocketDisconnect


# =========================================================
# CONFIGURATION
# =========================================================

WS_TOKEN = os.getenv("WS_TOKEN")

HEARTBEAT_INTERVAL = 30
MAX_CONNECTIONS = 100


# =========================================================
# CONNECTION MANAGER
# =========================================================

class ParkingConnections:

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def add(self, websocket: WebSocket) -> bool:
        async with self.lock:

            if len(self.clients) >= MAX_CONNECTIONS:
                return False

            self.clients.add(websocket)

            return True

    async def remove(self, websocket: WebSocket):
        async with self.lock:
            self.clients.discard(websocket)

    async def broadcast(self, message: dict):

        async with self.lock:
            clients = list(self.clients)

        if not clients:
            return

        dead_clients = []

        for client in clients:

            try:
                await client.send_json(message)

            except Exception as error:

                print(
                    f"[WS BROADCAST ERROR] "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )

                dead_clients.append(client)

        for client in dead_clients:
            await self.remove(client)


# Global connection manager
parking_connections = ParkingConnections()


# =========================================================
# HEARTBEAT
# =========================================================

async def heartbeat_loop(websocket: WebSocket):

    try:

        while True:

            await asyncio.sleep(HEARTBEAT_INTERVAL)

            try:

                await websocket.send_json(
                    {
                        "type": "PING"
                    }
                )

                print(
                    "[WS] Heartbeat PING sent",
                    flush=True,
                )

            except Exception as error:

                print(
                    f"[WS HEARTBEAT ERROR] "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )

                break

    except asyncio.CancelledError:
        pass


# =========================================================
# WEBSOCKET HANDLER
# =========================================================

async def parking_websocket(websocket: WebSocket):

    print(
        "[WS] Handler entered",
        flush=True,
    )

    # -----------------------------------------------------
    # SECURITY CHECK
    # -----------------------------------------------------

    if not WS_TOKEN:

        print(
            "[WS] ERROR: WS_TOKEN is missing",
            flush=True,
        )

        await websocket.close(
            code=1011,
            reason="WebSocket authentication is not configured",
        )

        return

    token = websocket.query_params.get("token")

    print(
        f"[WS] Token received: {'YES' if token else 'NO'}",
        flush=True,
    )

    print(
        f"[WS] Configured token length: {len(WS_TOKEN)}",
        flush=True,
    )

    if token != WS_TOKEN:

        print(
            "[WS] ERROR: Token authentication FAILED",
            flush=True,
        )

        await websocket.close(
            code=1008,
            reason="Unauthorized",
        )

        return

    print(
        "[WS] Token authentication SUCCESS",
        flush=True,
    )

    # -----------------------------------------------------
    # CONNECTION LIMIT
    # -----------------------------------------------------

    added = await parking_connections.add(websocket)

    print(
        f"[WS] Client registration result: {added}",
        flush=True,
    )

    if not added:

        print(
            "[WS] ERROR: Maximum connection limit reached",
            flush=True,
        )

        await websocket.close(
            code=1013,
            reason="Server connection limit reached",
        )

        return

    # -----------------------------------------------------
    # HEARTBEAT TASK
    # -----------------------------------------------------

    heartbeat_task = asyncio.create_task(
        heartbeat_loop(websocket)
    )

    try:

        # -------------------------------------------------
        # CONNECTED MESSAGE
        # -------------------------------------------------

        print(
            "[WS] Sending CONNECTED message...",
            flush=True,
        )

        await websocket.send_json(
            {
                "type": "CONNECTED",
                "message": "Parking live updates connected",
            }
        )

        print(
            "[WS] CONNECTED message SENT successfully",
            flush=True,
        )

        # -------------------------------------------------
        # KEEP CONNECTION ALIVE
        # -------------------------------------------------

        print(
            "[WS] Waiting for messages...",
            flush=True,
        )

        while True:

            try:

                message = await websocket.receive_text()

                print(
                    f"[WS] Received message: {message!r}",
                    flush=True,
                )

                if message == "PING":

                    await websocket.send_json(
                        {
                            "type": "PONG"
                        }
                    )

                    print(
                        "[WS] PONG sent",
                        flush=True,
                    )

            except WebSocketDisconnect as error:

                print(
                    f"[WS] Client disconnected. "
                    f"code={error.code}",
                    flush=True,
                )

                break

    except Exception as error:

        print(
            f"[WS ERROR] {type(error).__name__}: {error}",
            flush=True,
        )

        import traceback

        traceback.print_exc()

    finally:

        print(
            "[WS] Cleaning up connection",
            flush=True,
        )

        heartbeat_task.cancel()

        with suppress(asyncio.CancelledError):
            await heartbeat_task

        await parking_connections.remove(websocket)

        print(
            "[WS] Connection cleanup complete",
            flush=True,
        )