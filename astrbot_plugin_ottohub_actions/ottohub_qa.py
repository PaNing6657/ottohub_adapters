"""匿问我答:本地存储与回答匹配逻辑(纯命令模式)。

记录结构:
- qa_id       独立问答ID,全局递增纯数字(如 1 / 2 / 3),方便用户记忆与输入
- question    提问内容
- target_uid  被提问的用户(回答者)
- asker_origin 提问者的会话标识(用于回答后转达),不发送给被问者
- asker_name  提问者昵称(本地记录,不发送给被问者)
- status      awaiting(待回答) / answered(已回答)
- answer      回答内容(answered 后填充)
- answerer_name 回答者昵称
- created_at  提问时间
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional


class QuestionStore:
    def __init__(self, data_file: str | Path) -> None:
        self.data_file = Path(data_file)
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._receive_switch: dict[str, bool] = {}
        self._load()

    # ------------------------------------------------------------ 持久化

    def _load(self) -> None:
        try:
            if self.data_file.exists():
                with open(self.data_file, encoding="utf-8") as f:
                    data = json.load(f)
                self._records = data.get("records", [])
                self._receive_switch = data.get("receive_switch", {}) or {}
        except (OSError, json.JSONDecodeError):
            self._records = []
            self._receive_switch = {}
        # 兼容旧格式:忽略历史 events 字段,统一回写标准结构

    def _save(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.data_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "records": self._records,
                    "receive_switch": self._receive_switch,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        tmp.replace(self.data_file)

    # ------------------------------------------------------------ 接收开关

    def set_receive(self, uid: str, on: bool) -> None:
        """设置某用户是否接收匿问提问(默认接收)。"""
        with self._lock:
            self._receive_switch[str(uid)] = bool(on)
            self._save()

    def receives(self, uid: str) -> bool:
        """该用户是否接收匿问提问(未设置时默认接收)。"""
        with self._lock:
            return self._receive_switch.get(str(uid), True)

    # ------------------------------------------------------------ ID 生成

    @staticmethod
    def _is_numeric_record(record: dict[str, Any]) -> bool:
        qa_id = record.get("qa_id", "")
        return bool(qa_id) and str(qa_id).isdigit()

    def _next_qa_id(self) -> str:
        """全局递增纯数字 ID(兼容历史 QA-xxx 长格式记录,忽略之)。"""
        max_id = 0
        for record in self._records:
            if self._is_numeric_record(record):
                max_id = max(max_id, int(record["qa_id"]))
        return str(max_id + 1)

    # ------------------------------------------------------------ 查询

    def create(
        self,
        question: str,
        target_uid: str,
        asker_origin: str,
        asker_name: str,
    ) -> dict[str, Any]:
        qa_id = self._next_qa_id()
        record = {
            "qa_id": qa_id,
            "question": question,
            "target_uid": str(target_uid),
            "asker_origin": asker_origin,
            "asker_name": asker_name,
            "status": "awaiting",
            "answer": None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self._lock:
            self._records.append(record)
            self._save()
        return record

    def get(self, qa_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return next(
                (r for r in self._records if str(r["qa_id"]) == str(qa_id)), None
            )

    def list_by_asker(self, asker_origin: str) -> list[dict[str, Any]]:
        """某提问者发起的全部问题,按提问时间从早到晚(用于状态查询)。"""
        with self._lock:
            return [r for r in self._records if r["asker_origin"] == asker_origin]

    def list_by_target(self, target_uid: str) -> list[dict[str, Any]]:
        """某被问者收到的全部问题,按提问时间从早到晚(用于状态查询)。"""
        with self._lock:
            return [
                r
                for r in self._records
                if r["target_uid"] == str(target_uid)
            ]

    def list_awaiting_by_target(self, target_uid: str) -> list[dict[str, Any]]:
        """目标用户尚未回答的问题,按提问时间从早到晚。"""
        with self._lock:
            return [
                r
                for r in self._records
                if r["target_uid"] == str(target_uid) and r["status"] == "awaiting"
            ]

    def mark_answered(
        self, qa_id: str, answer: str, answerer_name: str
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            for record in self._records:
                if str(record["qa_id"]) == str(qa_id):
                    record["status"] = "answered"
                    record["answer"] = answer
                    record["answerer_name"] = answerer_name
                    self._save()
                    return record
        return None