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


class TopicConnectionManager:
    """In-memory broadcast manager for group-chat topics (live delivery).

    Room key: (org_db_path, topic_id) → set[WebSocket]. Один сокет на
    пользователя; при переключении темы сокет переезжает между комнатами.
    Используется только для «поке» («new»/активность) — само форматирование
    сообщений остаётся в HTTP-поллинге (корректные per-user поля), poll —
    фоллбэк при разрыве WS.
    """

    def __init__(self):
        self._rooms: dict[tuple[str, int], set[WebSocket]] = {}
        self._meta: dict[WebSocket, tuple[str, int, int]] = {}

    def join(self, org_db: str, topic_id: int, user_db_id: int, ws: WebSocket) -> None:
        # покинуть прежнюю комнату этого сокета (смена темы)
        self.leave(ws)
        self._rooms.setdefault((org_db, topic_id), set()).add(ws)
        self._meta[ws] = (org_db, topic_id, user_db_id)

    def leave(self, ws: WebSocket) -> None:
        meta = self._meta.pop(ws, None)
        if not meta:
            return
        org_db, topic_id, _ = meta
        room = self._rooms.get((org_db, topic_id))
        if room:
            room.discard(ws)
            if not room:
                self._rooms.pop((org_db, topic_id), None)

    async def broadcast(self, org_db: str, topic_id: int, payload: dict,
                        exclude_user: Optional[int] = None) -> None:
        room = self._rooms.get((org_db, topic_id))
        if not room:
            return
        dead = []
        for ws in list(room):
            meta = self._meta.get(ws)
            if exclude_user is not None and meta and meta[2] == exclude_user:
                continue
            try:
                await ws.send_json(payload)
            except Exception as e:
                logger.debug("Topic WS send failed topic=%s: %s", topic_id, e)
                dead.append(ws)
        for ws in dead:
            self.leave(ws)


topic_manager = TopicConnectionManager()
