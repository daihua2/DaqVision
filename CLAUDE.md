# CLAUDE.md —— daqvision

给在本仓工作的 Claude 会话看的。人读的总览在 [README.md](README.md) 与 [AIIntegration/README.md](AIIntegration/README.md)；这里只收"动手前必须知道、不知道就会做错"的东西。

## 仓库是什么

两条线，同一个仓：

| 目录 | 是什么 | 现状 |
| --- | --- | --- |
| `AIIntegration/` | **AI 项目群接入 AICloud 的统一新服务**（纯后端：骨架 + 算法域 + 工作台 + 训练）。部署在 AISERVER `/home/Project/AIIntegration` | **当前主线**，近期提交几乎都在这 |
| `proto/` + `vision-infer/` | 视觉 Sidecar（Python gRPC Server）⇄ daqgate `channel/vision`（Go Client） | 阶段 3 STUB，只发假事件 |
| `doc/` | 方案、预研、可行性结论（md） | 二进制原件不入库 |
| `exchange/` | daqgate → daqvision 的文档收件箱 | 只放说明/问答/决策，不放契约副本 |

AIIntegration 内部：`host/aiintegration/`（骨架）、`domains/*.py`（算法域，一个文件一个域）、`proto/`（对 AICloud 的契约正本）、`packaging/`（发布）、`doc/`（分界、已知问题）、`research/`。

## 硬规矩（违反会出事，不是风格问题）

1. **AISERVER 上原有四个 AI 项目（v4 / v5 / meter-helmet / VFD）一律不动**：不改、不停服、不共用它们的 venv。只读核查可以；它们的 `.env` 凭据**未经用户同意不读**。
2. **写 historystore 的只有骨架进程**（推点定义、写值）。worker 可用同一张证书**只读直连** hs（只调 `QueryHistory` / `SubscribeVQTs` 等读口），域本身不连 hs。依据 historystore `H-244 §2`：「共用 guid 互相踢流、互删快照」只发生在写配置流 `PushEntityConfigs` 上，对只读连接不成立。★只读是我方自律、hs 不强制，worker 进程内不得链写接口。（2026-09-17 用户同意修改；原规矩与原因见 AIIntegration/README.md §6）
3. **新增域 = 丢一个 `domains/*.py`，骨架不改**。骨架不枚举域；域不知道 hs / VQT / HTTP 的存在。分界判据见 [AIIntegration/doc/骨架与算法模块分界.md](AIIntegration/doc/骨架与算法模块分界.md)。
4. **没有可信输入，不许出质量 OK 的结论**（`runner.py` 在骨架层强制）。
5. **契约**（`proto/vision.proto`、`AIIntegration/proto/aiintegration.proto`）：字段编号只增不改不复用、废弃用 `reserved`、改就升 `proto_version` 并登记同目录 `CHANGELOG.md`。`aiintegration.proto` **改即投** `AISERVER:/home/Project/AIIntegration/proto/` **并发函**；不一致以本仓为准。
6. 生成物不入库：`vision-infer/app/vision_pb2*.py`、`proto/gen/`。`AIIntegration/host/aiintegration/apiproto/` 与 `hsproto/` 是入库的（用系统 `protoc` 经 `build.sh` / `regen.sh` 生成，别手改）。
7. `doc/` 下 pdf/pptx/docx、`data/**`、`AIIntegration/dist/` 不入库。结论以同名 md 入库。
8. 全仓 **LF**（`.gitattributes` 强制）；`.sh` / `.py` 在 Linux 上跑，CRLF 会让脚本失效。
9. 不预写投机性代码：没定案的议题只记在 README 待议表里，不先落码。

## 测试（AIIntegration）

在 **WSL** 里跑，stdlib `unittest`，零测试依赖。用脚本，**从 PowerShell 调**（已在 `.claude/settings.json` 免确认；Git Bash 会把 `/mnt/c/...` 改写成 Windows 路径，调不到）：

```powershell
wsl bash /mnt/d/Project/daqvision/AIIntegration/host/run-tests.sh          # 三环境串行
wsl bash /mnt/d/Project/daqvision/AIIntegration/host/run-tests.sh py310    # 只跑一个：312 | py310 | vision
```

三个环境：`/usr/bin/python3`（3.12，跳过视觉用例）、`~/aii-py310`（3.10，AISERVER 现场版本）、`~/aii-vision`（3.10 + onnxruntime，含视觉用例）。
★手工跑时**先 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY`**，否则访问 127.0.0.1 的 HTTP 用例全走代理回 502，看着像代码坏了（脚本已内置）。

- **三环境必须串行**（整机用例绑固定端口），且**三个都要过**才算过 —— 现场是 Python 3.10.12，只在 3.12 上跑过的曾经装不上。
- `tests/live_*_check.py` 是联机自检（要真 hs），不在 discover 里，按需手动跑。
- 修 bug / 加判据时做**变异验证**：把修复撤掉或改坏，确认有用例变红；不红说明用例是陪跑的，要补。结果写进提交说明。

## 发布

`PYV=3.10 bash AIIntegration/packaging/make-release.sh amd64-cpu`（AISERVER）。`PYV` **没有缺省**，必须按目标机 `python3 -V` 给。部署到 AISERVER 属于对外动作，**每次先经用户授权**。

## 往来函（与 AICloud / historystore / daqgate）

- 唯一投递位 `AISERVER:/home/Project/AIIntegration/docs/`（`ssh aiserver`），命名规范见该目录 README。
- 编号前缀：我方 `AI-`，AICloud `C-`，historystore `H-`，daqgate `D-`；接龙不跳号。
- **发文前先 `ls -lt` 该目录**，确认没漏看对方来函、编号没撞。回执一律新建文件不追加；发出不改，订正另发。
- 发函、部署、改现场 = 对外动作，先给用户过目。

## Git 提交约定

- 主分支 `master`，直接在上面提交（已定）。**提交与推送 origin 可自行决定，不必事先问**（2026-09-15 用户授权）；做完在回复里报提交号。**force push、改写已推送的历史、删分支仍须先问。**
- 标题：`类型(范围): 结论 —— 补一句为什么`，中文。类型用 `feat` / `fix` / `docs` / `chore` / `research`，范围如 `AIIntegration`、`AIIntegration/packaging`；AIIntegration 的行为改动也常直接写 `AIIntegration: …`。
  - 例：`fix(AIIntegration): 端口 0 不再静默随机化 —— 监听地址不合规拒绝启动，写路径不合规降级只读`
- 正文按需写：**现象 → 根因 → 改法 → 验证**。验证写实数：用例条数（+新增）、变异验证几发几红、三环境各多少通过；现场还没生效的写清卡在哪。引用往来函编号（`C-32 §4`、`AI-35`）。
- 写错过的东西要承认并订正，不悄悄改掉。
- 末尾带 `Co-Authored-By` 行。
