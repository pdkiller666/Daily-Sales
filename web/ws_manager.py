import logging
from typing import Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class DMConnectionManager:
    """In-memory WebSocket connection manager for Direct Messages.

    Key: (org_db_path, user_db_id) → WebSocket
    One connection per user per org (new login replaces old).
    """

    def __init__(self):
        self._connections: dict[tuple[str, int], WebSocket] = {}

    async def connect(self, org_db: str, user_db_id: int, ws: WebSocket) -> None:
        await ws.accept()
        old = self._connections.get((org_db, user_db_id))
        if old:
            try:
                await old.close(code=4000)
            except Exception:
                pass
        self._connections[(org_db, user_db_id)] = ws
        logger.debug("DM WS connected: org=%s uid=%s total=%d", org_db, user_db_id, len(self._connections))

    def disconnect(self, org_db: str, user_db_id: int) -> None:
        self._connections.pop((org_db, user_db_id), None)
        logger.debug("DM WS disconnected: org=%s uid=%s total=%d", org_db, user_db_id, len(self._connections))

    def is_online(self, org_db: str, user_db_id: int) -> bool:
        return (org_db, user_db_id) in self._connections

    async def send_to_user(self, org_db: str, user_db_id: int, payload: dict) -> bool:
        ws = self._connections.get((org_db, user_db_id))
        if not ws:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception as e:
            logger.debug("DM WS send failed uid=%s: %s", user_db_id, e)
            self.disconnect(org_db, user_db_id)
            return False

    def get_online_ids(self, org_db: str) -> set[int]:
        return {uid for (db, uid) in self._connections if db == org_db}


dm_manager = DMConnectionManager()
