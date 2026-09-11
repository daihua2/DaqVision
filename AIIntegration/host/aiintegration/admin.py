"""管理命令行 —— **只是服务 HTTP / gRPC 口的客户端，不直接开库**。

工作台库有"服务进程是唯一写者"的前提（训练执行器重启时据此收拾遗留任务），
管理脚本若直接写库，这个前提就破了。所以这里的每条命令都走服务自己开的口。

用法（在服务所在机器上）：

  .venv/bin/python -m aiintegration.admin import-artifact \\
      --domain vision_helmet --file /path/yolo11n_safety_640_fp32.onnx \\
      --name "安全帽 yolo11n 640 fp32" \\
      --source "AISERVER:/home/ruiteng/2026/meter_service/helmet_service/models/yolo11n_safety_640_fp32.onnx" \\
      --training-data "未知（原 helmet_service 作者训练，训练集未随模型交付）" \\
      --license "未知；模型元数据标注 AGPL-3.0"

  .venv/bin/python -m aiintegration.admin list-artifacts --domain vision_helmet

  .venv/bin/python -m aiintegration.admin activate-artifact --id 3 --yes

★来源、训练数据说明、许可三项**必填**：不清楚就明写"未知"，留空会被服务拒收。
★`activate-artifact` 必须带 `--yes`：启用是人的决定，免得顺手一敲就换了现场模型。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

DEFAULT_HTTP = "127.0.0.1:50071"
DEFAULT_API = "127.0.0.1:50070"


def _import_artifact(a: argparse.Namespace) -> int:
    path = Path(a.file)
    if not path.is_file():
        print(f"✗ 文件不存在：{path}", file=sys.stderr)
        return 2
    params = {"name": a.name, "source": a.source, "training_data": a.training_data,
              "license": a.license, "kind": a.kind, "binding": a.binding,
              "ext": path.suffix.lower() or ".bin", "algo": a.algo, "note": a.note}
    url = f"http://{a.http}/artifacts/import/{quote(a.domain)}?{urlencode(params)}"
    data = path.read_bytes()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "")
        except Exception:  # noqa: BLE001
            msg = ""
        print(f"✗ 服务拒收（HTTP {e.code}）：{msg}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"✗ 连不上服务 {a.http}：{e.reason}", file=sys.stderr)
        return 1
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0


def _grpc_call(api: str, method: str, req, res_cls):
    import grpc

    from .api import SERVICE
    ch = grpc.insecure_channel(api, options=[("grpc.enable_http_proxy", 0)])
    try:
        return ch.unary_unary(f"/{SERVICE}/{method}",
                              request_serializer=lambda m: m.SerializeToString(),
                              response_deserializer=res_cls.FromString)(req, timeout=10)
    finally:
        ch.close()


def _list_artifacts(a: argparse.Namespace) -> int:
    from .apiproto import aiintegration_pb2 as pb
    res = _grpc_call(a.api, "ListArtifacts", pb.ListArtifactsReq(domain=a.domain, limit=1000),
                     pb.ListArtifactsRes)
    print(f"共 {res.total} 个工件")
    for x in res.items:
        flag = "★启用中" if x.active else "      "
        origin = "外部导入·训练数据未核实" if x.origin == "imported" else (x.origin or "trained")
        print(f"{flag} id={x.id} [{x.kind}] {x.name}  对象={x.binding or '(全域)'}  "
              f"{x.size} 字节  来历={origin}")
        if x.source:
            print(f"         来源：{x.source}")
        if x.training_data:
            print(f"         训练数据：{x.training_data}")
        if x.license:
            print(f"         许可：{x.license}")
    return 0


def _activate_artifact(a: argparse.Namespace) -> int:
    if not a.yes:
        print("✗ 启用会立刻换掉该对象正在用的模型 —— 确认后加 --yes 再执行", file=sys.stderr)
        return 2
    from .apiproto import aiintegration_pb2 as pb
    res = _grpc_call(a.api, "ActivateArtifact", pb.IdReq(id=a.id), pb.MutateRes)
    if not res.ok:
        print(f"✗ {res.message}", file=sys.stderr)
        return 1
    print(f"✓ 已启用工件 {a.id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiintegration.admin", description="AIIntegration 管理命令行（走服务的口，不直接开库）")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import-artifact", help="把外部模型文件导入为工件（不自动启用）")
    imp.add_argument("--domain", required=True)
    imp.add_argument("--file", required=True)
    imp.add_argument("--name", required=True)
    imp.add_argument("--source", required=True, help="从哪来（必填；不清楚写「未知」）")
    imp.add_argument("--training-data", required=True, dest="training_data",
                     help="训练数据说明（必填；不清楚写「未知」）")
    imp.add_argument("--license", required=True, help="许可（必填；不清楚写「未知」）")
    imp.add_argument("--kind", default="model")
    imp.add_argument("--binding", default="", help="作用对象；空 = 全域通用")
    imp.add_argument("--algo", default="")
    imp.add_argument("--note", default="")
    imp.add_argument("--http", default=DEFAULT_HTTP)
    imp.set_defaults(fn=_import_artifact)

    ls = sub.add_parser("list-artifacts", help="列出工件（含来历）")
    ls.add_argument("--domain", default="")
    ls.add_argument("--api", default=DEFAULT_API)
    ls.set_defaults(fn=_list_artifacts)

    act = sub.add_parser("activate-artifact", help="启用工件（必须带 --yes）")
    act.add_argument("--id", type=int, required=True)
    act.add_argument("--yes", action="store_true")
    act.add_argument("--api", default=DEFAULT_API)
    act.set_defaults(fn=_activate_artifact)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
