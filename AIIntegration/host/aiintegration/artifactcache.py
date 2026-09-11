"""当前启用工件的读取与缓存 —— 推理时把它交给模块。

模块不碰存储（分界文档 §1），但它要用自己训出来的东西。于是这一层：
**按 (域, 对象) 查"当前启用的是哪个工件"，把字节读进来，交给成帧那一步。**

三条：

1. **按工件 id 缓存**，不每拍重读磁盘。有人在界面上点了"启用"，下一拍就换过来
   （判据是 `artifacts` 表里 `active=1` 的那一行的 id 变了，不是文件时间）。
2. **没启用就没有这一档** —— 不给空的、更不"随便挑个最新的顶上"。
   顶上去的结论看起来完全正常，是最坏的一种错。模块据此落 `MODEL_NOT_LOADED`。
3. **文件读不到要每周期都吵**（可用性关键降级态，不做"只记一次"限流）：
   记录说有一个启用的工件、磁盘上却没有，这是**数据面与文件面对不上**，
   比"没有模型"更需要有人去看。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .types import ArtifactBlob
from .workbench import Workbench

logger = logging.getLogger(__name__)

#: 关心哪几类工件。★不是封闭枚举 —— 域产出什么类就缓存什么类，这里只是"去问哪几类"。
#: 加一类只改这一行，且加错了最坏的后果是多问一次数据库。
KINDS = ("model", "baseline")


class ActiveArtifacts:
    """(域, 对象) → {kind: ArtifactBlob}。线程安全。"""

    def __init__(self, workbench: Workbench, artifacts_dir: Path) -> None:
        self._wb = workbench
        self._dir = Path(artifacts_dir)
        self._lock = threading.Lock()
        # id → ArtifactBlob。★按 id 缓存：id 不变则内容不变（工件是只增不改的）。
        self._by_id: dict[int, ArtifactBlob] = {}

    def for_binding(self, domain: str, binding: str) -> dict[str, ArtifactBlob]:
        out: dict[str, ArtifactBlob] = {}
        for kind in KINDS:
            row = self._wb.active_artifact(domain, kind, binding)
            if row is None and binding:
                # 退回"全域通用"那一档：一个域可以有一个跨对象的模型（`binding=''`）。
                # ★这不是"随便挑一个"——`binding=''` 是**显式声明过**的全域件。
                row = self._wb.active_artifact(domain, kind, "")
            if row is None:
                continue
            blob = self._load(row)
            if blob is not None:
                out[kind] = blob
        return out

    def _load(self, row) -> ArtifactBlob | None:
        with self._lock:
            hit = self._by_id.get(row.id)
        if hit is not None:
            return hit

        path = self._dir / row.path
        try:
            data = path.read_bytes()
        except OSError as exc:
            # ★每次都吵：记录说有启用件、磁盘上却没有 —— 这是两边对不上，不是"没模型"。
            logger.warning(
                "启用中的工件读不到，本对象将按「无可用模型」处置：%s/%s kind=%s id=%d path=%s (%s)",
                row.domain, row.binding or "(全域)", row.kind, row.id, row.path, exc)
            return None
        if not data:
            logger.warning(
                "启用中的工件是空文件（写坏或被截断），按「无可用模型」处置：id=%d path=%s",
                row.id, row.path)
            return None

        try:
            meta = json.loads(row.meta_json or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except json.JSONDecodeError:
            # meta 坏了不影响工件本体可用 —— 但要说一声，别让人以为它本来就是空的。
            logger.warning("工件 id=%d 的 meta_json 解析不了，按空处理", row.id)
            meta = {}

        blob = ArtifactBlob(id=row.id, kind=row.kind, name=row.name, blob=data,
                            algo=row.algo, accuracy=row.accuracy,
                            meta={str(k): str(v) for k, v in meta.items()},
                            created_at=row.created_at)
        with self._lock:
            self._by_id[row.id] = blob
        return blob

    def invalidate(self, artifact_id: int | None = None) -> None:
        """丢缓存。删工件后调；不给 id 就全丢。"""
        with self._lock:
            if artifact_id is None:
                self._by_id.clear()
            else:
                self._by_id.pop(artifact_id, None)
