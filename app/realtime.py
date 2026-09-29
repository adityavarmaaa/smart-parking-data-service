import asyncio

from fastapi import WebSocket, WebSocketDisconnect


class ParkingConnections:

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.clients.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.clients.discard(websocket)

    async def broadcast(self, message: dict):

        if not self.clients:
            return

        clients = list(self.clients)

        results = await asyncio.gather(
            *(client.send_json(message) for client in clients),
            return_exceptions=True,
        )

        for client, result in zip(clients, results):

            if isinstance(result, Exception):
                self.disconnect(client)


# =========================================================
# GLOBAL CONNECTION MANAGER
# =========================================================

parking_connections = ParkingConnections()


# =========================================================
# WEBSOCKET ENDPOINT HANDLER
# =========================================================

async def parking_websocket(websocket: WebSocket):

    await parking_connections.connect(websocket)

    try:

        await websocket.send_json({
            "type": "CONNECTED",
            "message": "Parking live updates connected",
        })

        while True:

            # Keep connection alive.
            # Client messages are not required.
            await websocket.receive_text()

    except WebSocketDisconnect:

        pass

    finally:

        parking_connections.disconnect(websocket)