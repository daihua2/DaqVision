"""
vision-infer 推理服务 —— 阶段 3 "空跑骨架"（gRPC SERVER）。

当前为 STUB：不接相机、不接 NPU，只发假事件，用来先打通 daqgate(channel/vision) ⇄ Python 的 gRPC 链路。
"先通管道，再灌数据" —— 链路通了之后，再把 capture/infer/decision 换成真实实现。

运行前先生成桩：  ../../proto/buildPython.sh   （产出 vision_pb2.py / vision_pb2_grpc.py 到本目录）
启动：            python app/server.py            （或设 VISION_BIND 环境变量）
"""
import os
import sys
import time
import uuid
import threading
from concurrent import futures

# 让生成的 vision_pb2 / vision_pb2_grpc 可被导入（它们与本文件同目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grpc
from google.protobuf import timestamp_pb2

try:
    import vision_pb2 as pb
    import vision_pb2_grpc as pb_grpc
except ImportError:
    sys.exit("缺少生成的桩代码，请先运行 proto/buildPython.sh")

PROTO_VERSION = "1.0"
SERVICE_VERSION = "0.1.0-stub"


def _now_ts():
    ts = timestamp_pb2.Timestamp()
    ts.GetCurrentTime()
    return ts


class VisionInferenceServicer(pb_grpc.VisionInferenceServicer):
    def __init__(self):
        # source_id -> PipelineConfig（STUB：内存里记一下，便于 GetInfo 回报状态）
        self._pipelines = {}
        self._lock = threading.Lock()

    # ── 握手/探活 ──
    def GetInfo(self, request, context):
        with self._lock:
            statuses = [
                pb.PipelineStatus(source_id=sid, state=pb.PipelineStatus.RUNNING, fps=0.0)
                for sid in self._pipelines
            ]
        return pb.InfoReply(
            service_version=SERVICE_VERSION,
            proto_version=PROTO_VERSION,
            runtime="STUB (no NPU)",
            models=[pb.ModelInfo(
                name="helmet-yolo11n", version="stub", task="detection",
                input_w=640, input_h=640, labels=["helmet", "head"],
            )],
            pipelines=statuses,
        )

    # ── 事件流（STUB：心跳 + 周期性假检测）──
    def StreamEvents(self, request, context):
        source = request.source_ids[0] if request.source_ids else "cam-demo"
        n = 0
        while context.is_active():
            n += 1
            if n % 5 == 0:
                # 每 5 拍发一条假"未戴安全帽"检测
                yield pb.VisionEvent(
                    event_id=str(uuid.uuid4()), source_id=source,
                    model="helmet-yolo11n", type=pb.DETECTION, ts=_now_ts(),
                    infer_ms=12.3,
                    detections=[pb.Detection(
                        label="head", score=0.87,
                        box=pb.BBox(x=0.40, y=0.20, w=0.12, h=0.18),
                    )],
                    snapshot=pb.SnapshotRef(
                        snapshot_id=f"snap-{n}", width=1920, height=1080,
                        path=f"/tmp/daqvision/snap-{n}.jpg",
                    ),
                )
            elif request.include_heartbeat:
                yield pb.VisionEvent(
                    event_id=str(uuid.uuid4()), source_id=source,
                    type=pb.HEARTBEAT, ts=_now_ts(),
                )
            time.sleep(2)

    # ── 控制 ──
    def ApplyPipeline(self, request, context):
        with self._lock:
            self._pipelines[request.source_id] = request
        return pb.PipelineAck(ok=True, message=f"pipeline '{request.source_id}' applied (stub)")

    def StopPipeline(self, request, context):
        with self._lock:
            self._pipelines.pop(request.source_id, None)
        return pb.PipelineAck(ok=True, message=f"pipeline '{request.source_id}' stopped (stub)")

    # ── 抓拍/单图（STUB）──
    def GetSnapshot(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("GetSnapshot 尚未实现（stub）")
        return pb.SnapshotReply()

    def InferOnce(self, request, context):
        return pb.VisionEvent(
            event_id=str(uuid.uuid4()), model=request.model or "helmet-yolo11n",
            type=pb.DETECTION, ts=_now_ts(), infer_ms=10.0,
            detections=[pb.Detection(label="helmet", score=0.95,
                                     box=pb.BBox(x=0.3, y=0.1, w=0.15, h=0.2))],
        )


def serve():
    # 同机部署可用 Unix socket：VISION_BIND="unix:///var/run/daqgate/vision.sock"
    bind = os.environ.get("VISION_BIND", "0.0.0.0:50061")
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=8),
        options=[("grpc.max_send_message_length", 8 * 1024 * 1024)],
    )
    pb_grpc.add_VisionInferenceServicer_to_server(VisionInferenceServicer(), server)
    server.add_insecure_port(bind)  # 同机/内网先用 insecure；分布式换 mTLS
    server.start()
    print(f"[vision-infer:stub] VisionInference 已启动，监听 {bind}（proto {PROTO_VERSION}）")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
