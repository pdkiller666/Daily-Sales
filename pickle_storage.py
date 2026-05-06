"""
Persistent FSM storage based on pickle — survives bot restarts.
Drop-in replacement for MemoryStorage in aiogram 3.x.
"""
import asyncio
import logging
import pickle
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


class PickleStorage(BaseStorage):
    def __init__(self, file_path: str = "data/fsm_storage.pkl"):
        self._file = Path(file_path)
        self._lock = asyncio.Lock()
        self._data: Dict[str, Dict] = {}
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            try:
                with open(self._file, "rb") as f:
                    self._data = pickle.load(f)
            except Exception as e:
                logging.error(f"PickleStorage: failed to load state file, starting fresh: {e}")
                self._data = {}

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump(self._data, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(self._file)
        except Exception as e:
            logging.error(f"PickleStorage: failed to persist state: {e}")

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        async with self._lock:
            k = self._key(key)
            if k not in self._data:
                self._data[k] = {"state": None, "data": {}}
            self._data[k]["state"] = state.state if state else None
            self._save()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with self._lock:
            return self._data.get(self._key(key), {}).get("state")

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        async with self._lock:
            k = self._key(key)
            if k not in self._data:
                self._data[k] = {"state": None, "data": {}}
            self._data[k]["data"] = data
            self._save()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with self._lock:
            return dict(self._data.get(self._key(key), {}).get("data", {}))

    async def close(self) -> None:
        pass
