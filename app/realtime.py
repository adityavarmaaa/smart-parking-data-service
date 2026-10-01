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

            except Exception:
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

            except Exception:
                break

    except asyncio.CancelledError:
        pass


# =========================================================
# WEBSOCKET HANDLER
# =========================================================

async def parking_websocket(websocket: WebSocket):

    # -----------------------------------------------------
    # SECURITY CHECK
    # -----------------------------------------------------

    if not WS_TOKEN:

        await websocket.close(
            code=1011,
            reason="WebSocket authentication is not configured"
        )

        return

    token = websocket.query_params.get("token")

    if token != WS_TOKEN:

        await websocket.close(
            code=1008,
            reason="Unauthorized"
        )

        return

    # -----------------------------------------------------
    # CONNECTION LIMIT
    # -----------------------------------------------------

    added = await parking_connections.add(websocket)

    if not added:

        await websocket.close(
            code=1013,
            reason="Server connection limit reached"
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

        await websocket.send_json(
            {
                "type": "CONNECTED",
                "message": "Parking live updates connected"
            }
        )

        # -------------------------------------------------
        # KEEP CONNECTION ALIVE
        # -------------------------------------------------

        while True:

            try:

                message = await websocket.receive_text()

                if message == "PING":

                    await websocket.send_json(
                        {
                            "type": "PONG"
                        }
                    )

            except WebSocketDisconnect:

                break

    except Exception:
        pass

    finally:

        # -------------------------------------------------
        # STOP HEARTBEAT
        # -------------------------------------------------

        heartbeat_task.cancel()

        with suppress(asyncio.CancelledError):
            await heartbeat_task

        # -------------------------------------------------
        # REMOVE CONNECTION
        # -------------------------------------------------

        await parking_connections.remove(websocket)