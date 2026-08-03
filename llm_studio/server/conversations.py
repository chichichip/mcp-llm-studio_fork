"""conversations.py

대화 기록을 conversations/ 폴더에 대화당 JSON 파일 하나로 저장한다.
메시지는 OpenAI 규격 그대로 저장하므로(assistant.tool_calls, role=tool 포함)
불러온 이력을 그대로 다음 요청에 넣을 수 있다.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path


class ConversationStore:
    def __init__(self, base_dir: Path):
        self.dir = base_dir / "conversations"
        self.dir.mkdir(exist_ok=True)
        # 대화 하나당 락. 탭 두 개가 같은 대화에 동시에 쓰면 읽고-고치고-쓰는 사이에
        # 서로를 덮어쓴다(한쪽 턴이 조용히 사라진다). 저장 구간만 짧게 잡는다.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _path(self, conv_id: str) -> Path:
        safe = "".join(ch for ch in conv_id if ch.isalnum() or ch in "-_")
        return self.dir / f"{safe}.json"

    def _lock_for(self, conv_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conv_id, threading.Lock())

    def create(self, title: str = "새 대화") -> dict:
        conv = {
            "id": f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "title": title,
            "created": time.time(),
            "updated": time.time(),
            "messages": [],
        }
        self.save(conv)
        return conv

    def save(self, conv: dict, base_count: int | None = None) -> None:
        """대화를 저장한다.

        base_count: 이 대화를 읽었을 때의 메시지 개수. 주면 **덮어쓰기 대신 병합**한다 —
        내가 읽은 뒤 다른 탭이 같은 대화에 먼저 썼으면, 그쪽 이력을 살리고 이번에 늘어난
        메시지만 뒤에 붙인다(안 그러면 남의 턴이 통째로 사라진다). None이면 그대로 쓴다
        (제목 변경처럼 메시지를 건드리지 않는 저장).
        """
        with self._lock_for(conv["id"]):
            if base_count is not None:
                disk = self.load(conv["id"])
                if disk is not None and len(disk.get("messages", [])) != base_count:
                    conv = dict(conv)
                    conv["messages"] = list(disk.get("messages", [])) + \
                        list(conv.get("messages", []))[base_count:]
            conv["updated"] = time.time()
            self._write_atomic(self._path(conv["id"]), conv)

    def _write_atomic(self, path: Path, conv: dict) -> None:
        """임시 파일에 쓰고 갈아끼운다 — 쓰는 도중 앱이 죽어도 반쯤 쓰인 JSON이 남지 않는다.

        os.replace는 같은 볼륨에서 원자적이다(Windows 포함). 임시 파일은 `.json.tmp`라
        list()의 `*.json` 글롭에 걸리지 않는다.
        """
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(conv, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, conv_id: str) -> dict | None:
        path = self._path(conv_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self, conv_id: str) -> bool:
        path = self._path(conv_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def rename(self, conv_id: str, title: str) -> bool:
        conv = self.load(conv_id)
        if conv is None:
            return False
        conv["title"] = title.strip()[:80] or conv["title"]
        self.save(conv)
        return True

    def list(self) -> list[dict]:
        """메시지 본문을 뺀 메타 목록. 최근 수정 순."""
        metas = []
        for path in self.dir.glob("*.json"):
            try:
                conv = json.loads(path.read_text(encoding="utf-8"))
                metas.append({
                    "id": conv["id"],
                    "title": conv.get("title", "(제목 없음)"),
                    "updated": conv.get("updated", 0),
                    "message_count": len(conv.get("messages", [])),
                })
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(metas, key=lambda m: m["updated"], reverse=True)

    @staticmethod
    def title_from(text: str) -> str:
        first_line = text.strip().splitlines()[0] if text.strip() else "새 대화"
        return first_line[:40] + ("…" if len(first_line) > 40 else "")
