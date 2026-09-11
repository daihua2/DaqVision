"""外部模型导入为工件 —— **来源必填、默认未核实、域先校验、不自动启用**。

至今工件只从训练产出。视觉域要用的模型却是**外面训好的**（`helmet_service` 里的 ONNX），
于是需要一条导入的路。这条路是"合成数据训出的模型"那类问题（原 VFD）**唯一能在代码层做的事**：
代码救不了一个用假数据训的模型，但能让它**带着来历进来、并且看得见**。

---

## 1. 四条规矩

1. **来源、训练数据说明、许可 三项必填。** 不知道就明写"未知"——**写"未知"是如实，留空是回避**。
   来历不明的模型进了工件库，半年后没人答得上"这是谁、用什么训的、能不能商用"。
2. **导入即标"外部导入"**（`origin=imported`）。界面应如实显示"训练数据未经本系统核实"。
   本系统没有核实外部训练数据的手段，所以**不提供"已核实"这个状态**——有了它，就会有人去点。
3. **域先校验，不过就拒收。** 骨架不懂模型格式（分界：骨架不懂算法），能不能用只有域知道；
   域没提供校验就照收，但**回执里明说"没经过能不能用的检查"**。域从模型里读出的事实（元数据里的描述、
   版本、许可、类别表）**原样存进工件**——比如"文件名 yolo11n、元数据写 YOLOv5n"这种不一致，就留在记录里看得见。
4. **不自动启用。** 与训练产出同一条：启用是人的决定。

## 2. 为什么走服务进程，不让外部脚本直接写库

工作台库有"本进程是唯一写者"的前提（训练执行器重启时据此收拾遗留任务）。管理脚本若直接开库写，
这个前提就破了。⇒ 导入只在服务进程里发生；命令行工具只是 HTTP 口的一个客户端。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .domains import LoadedDomain
from .workbench import Workbench

logger = logging.getLogger(__name__)

#: 单个工件上限。视觉 m 模型 80 MB，留足余量；再大多半是传错了文件。
MAX_IMPORT_BYTES = 512 * 1024 * 1024

#: 必填的来历三项 + 名字。
REQUIRED_FIELDS = ("name", "source", "training_data", "license")

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")


class ImportRejected(Exception):
    """调用方给错了 / 域拒收。`status` 给 HTTP 层，`message` 原样回给调用方。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class ImportResult:
    artifact_id: int
    path: str
    size: int
    sha256: str
    validated: bool
    """域是否校验过这个文件（域没提供校验时为假）。"""

    facts: dict[str, str] = field(default_factory=dict)
    """域从文件里读出的事实（元数据描述、版本、许可、类别表…），已原样存进工件。"""

    warnings: list[str] = field(default_factory=list)


class ArtifactImporter:
    def __init__(self, *, workbench: Workbench, domains: dict[str, LoadedDomain],
                 artifacts_dir: Path) -> None:
        self._wb = workbench
        self._domains = domains
        self._dir = Path(artifacts_dir)

    def import_blob(self, *, domain: str, data: bytes, fields: dict[str, str]) -> ImportResult:
        loaded = self._domains.get(domain)
        if loaded is None:
            raise ImportRejected(404, f"域 {domain!r} 未装载；已装载：{sorted(self._domains)}")
        if "artifact" not in loaded.caps:
            raise ImportRejected(409, f"域 {domain} 不接受外部工件（能力位 {sorted(loaded.caps)} 里没有 artifact）")

        f = {k: (v or "").strip() for k, v in fields.items()}
        missing = [k for k in REQUIRED_FIELDS if not f.get(k)]
        if missing:
            raise ImportRejected(
                400, f"缺必填项 {missing} —— 来历不明的模型不许进工件库；不清楚就明写「未知」，不要留空")
        if len(f["name"]) > 128:
            raise ImportRejected(400, "name 太长（上限 128 字）")

        kind = f.get("kind") or "model"
        if not _KIND_RE.match(kind):
            raise ImportRejected(400, f"kind 不合法: {kind!r}")
        ext = (f.get("ext") or ".bin").lower()
        if not _EXT_RE.match(ext):
            raise ImportRejected(400, f"ext 不合法: {ext!r}（形如 .onnx）")
        binding = f.get("binding", "")

        if not data:
            raise ImportRejected(400, "上传内容为空")
        if len(data) > MAX_IMPORT_BYTES:
            raise ImportRejected(413, f"工件 {len(data)} 字节，超过上限 {MAX_IMPORT_BYTES} 字节")

        sha = hashlib.sha256(data).hexdigest()
        dup = self._wb.find_artifact_by_sha256(domain, kind, sha)
        if dup is not None:
            # 同一文件导两次，工件页上就有两个"看起来不同"的模型 —— 当场拒，指明已有那个。
            raise ImportRejected(409, f"同一文件已导入过：工件 {dup.id}「{dup.name}」（sha256 相同）")

        # ── 域校验 ──
        warnings: list[str] = []
        facts: dict[str, str] = {}
        validate = getattr(loaded.instance, "validate_artifact", None)
        validated = callable(validate)
        if not validated:
            warnings.append("该域未提供工件校验：这个文件没有经过「能不能用」的检查")
        else:
            try:
                reason, facts = validate(kind, bytes(data))
            except Exception as exc:  # noqa: BLE001 —— 校验炸了等于没通过
                reason, facts = f"校验时出错：{type(exc).__name__}: {exc}", {}
            if reason:
                raise ImportRejected(422, f"域 {domain} 拒收这个工件：{reason}")
            facts = {str(k): str(v) for k, v in (facts or {}).items()}

        # ── 落盘（临时文件再改名：半个工件躺着，下载是坏的而记录看着正常）──
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rel = f"{domain}/{kind}/imported-{stamp}-{_safe(f['name'])}{ext}"
        target = self._dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)

        meta = {"imported_at": datetime.now(timezone.utc).isoformat()}
        if f.get("note"):
            meta["note"] = f["note"]
        meta.update({f"model_{k}": v for k, v in facts.items()})
        try:
            aid = self._wb.add_artifact(
                domain=domain, name=f["name"], kind=kind, binding=binding,
                algo=f.get("algo", ""), path=rel, size=len(data), sha256=sha,
                meta_json=json.dumps(meta, ensure_ascii=False),
                origin="imported", source=f["source"],
                training_data=f["training_data"], license=f["license"])
        except Exception:
            target.unlink(missing_ok=True)      # 记录没写成，别留一个没人认领的文件
            raise

        warnings.append("外部导入：训练数据未经本系统核实（界面请如实标注）")
        warnings.append("未自动启用：请在工件页确认后启用")
        logger.warning("导入外部工件：域 %s 工件 %d「%s」%d 字节 sha256=%s 来源=%s 许可=%s",
                       domain, aid, f["name"], len(data), sha[:12], f["source"], f["license"])
        return ImportResult(artifact_id=aid, path=rel, size=len(data), sha256=sha,
                            validated=validated, facts=facts, warnings=warnings)


def _safe(name: str) -> str:
    """名字进文件名：只留字母数字（含中文）与 - _，其余去掉；空了用 artifact。"""
    out = "".join(c for c in name if c.isalnum() or c in "-_")[:48]
    return out or "artifact"
