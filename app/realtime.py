import asyncio

from fastapi import WebSocket, WebSocketDisconnect


class ParkingConnections:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    def add(self, websocket: WebSocket):
        self.clients.add(websocket)

    def remove(self, websocket: WebSocket):
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
                self.remove(client)


parking_connections = ParkingConnections()


async def parking_websocket(websocket: WebSocket):
    parking_connections.add(websocket)

    try:
        await websocket.send_json({
            "type": "CONNECTED",
            "message": "Parking live updates connected",
        })

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    finally:
        parking_connections.remove(websocket)