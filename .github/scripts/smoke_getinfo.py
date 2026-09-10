"""CI 冒烟：起 vision-infer stub，打一次 GetInfo，校验 proto_version 与模型清单非空。

只验"管道通不通"，不验推理结果——stub 阶段本来就没有真模型。
接真模型后本脚本不必改：GetInfo 的语义（能力/版本/模型清单）不随实现变。
"""
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "vision-infer" / "app"
BIND = "127.0.0.1:50061"

sys.path.insert(0, str(APP))
import grpc  # noqa: E402
import vision_pb2 as pb  # noqa: E402
import vision_pb2_grpc as pb_grpc  # noqa: E402

env = dict(os.environ, VISION_BIND=BIND)
proc = subprocess.Popen([sys.executable, str(APP / "server.py")], env=env)
try:
    channel = grpc.insecure_channel(BIND)
    # server 起来要一会儿；给 10s，超时就是真failed，不要无限等。
    grpc.channel_ready_future(channel).result(timeout=10)
    reply = pb_grpc.VisionInferenceStub(channel).GetInfo(pb.InfoRequest(), timeout=5)

    assert reply.proto_version, "proto_version 为空"
    assert reply.service_version, "service_version 为空"
    assert len(reply.models) > 0, "models 清单为空"
    print(
        f"OK  proto={reply.proto_version}  service={reply.service_version}  "
        f"runtime={reply.runtime}  models={[m.name for m in reply.models]}"
    )
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
