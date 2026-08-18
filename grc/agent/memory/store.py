"""store.py:长期记忆——成功流图 / 修复经验的复用库。

DeepAgent 每完成一个可跑的流图,或修好一个 critic 报错,都可把
"意图签名 -> 解法"存进本库;下次遇到相似意图/相似报错时,skills 层
可优先召回历史解法,减少 LLM 往返(架构文档里的"经验复用"支线)。

实现取向:JSONL 追加 + 关键词签名召回,零外部依赖、可离线复现。
不做向量检索(避免引入依赖),用轻量词袋 Jaccard 近似即可满足演示。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _sig(text: str) -> set:
    """把一段意图/报错文本转成词袋签名(小写 token 集合)。"""
    return set(_TOKEN.findall((text or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass
class Recall:
    """一次召回结果。"""

    score: float
    kind: str
    query: str
    payload: dict

    def as_dict(self) -> dict:
        return {"score": round(self.score, 3), "kind": self.kind,
                "query": self.query, "payload": self.payload}


@dataclass
class FlowGraphStore:
    """成功流图 / 修复经验库(JSONL 落盘)。

    两类记录:
        kind="flowgraph":意图文本 -> {recipe, params, grc_path}
        kind="fix":     critic 报错文本 -> {block, action, note}
    """

    path: Optional[str] = None
    _rows: List[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.path and os.path.exists(self.path):
            self._load()

    # -- 写入 --------------------------------------------------------------
    def remember_flowgraph(self, intent: str, recipe: str,
                           params: Optional[dict] = None,
                           grc_path: Optional[str] = None) -> None:
        self._append({"kind": "flowgraph", "query": intent,
                      "payload": {"recipe": recipe, "params": params or {},
                                  "grc_path": grc_path}})

    def remember_fix(self, error: str, block: str, action: str,
                     note: str = "") -> None:
        self._append({"kind": "fix", "query": error,
                      "payload": {"block": block, "action": action,
                                  "note": note}})

    # -- 召回 --------------------------------------------------------------
    def recall(self, query: str, kind: Optional[str] = None,
               top_k: int = 3, min_score: float = 0.15) -> List[Recall]:
        q = _sig(query)
        scored: List[Tuple[float, dict]] = []
        for r in self._rows:
            if kind and r.get("kind") != kind:
                continue
            s = _jaccard(q, _sig(r.get("query", "")))
            if s >= min_score:
                scored.append((s, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [Recall(score=s, kind=r["kind"], query=r["query"],
                       payload=r["payload"]) for s, r in scored[:top_k]]

    def best_recipe(self, intent: str) -> Optional[str]:
        """便捷:给一句意图,返回最相似历史流图用的 recipe 名。"""
        hits = self.recall(intent, kind="flowgraph", top_k=1)
        return hits[0].payload.get("recipe") if hits else None

    def __len__(self) -> int:
        return len(self._rows)

    # -- 内部 --------------------------------------------------------------
    def _append(self, row: dict) -> None:
        row["ts"] = round(time.time(), 3)
        self._rows.append(row)
        if self.path:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                        exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._rows.append(json.loads(line))
                except ValueError:
                    continue
