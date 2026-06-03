# exchange/ — 来自 daqgate 的文档收件箱

双向文档交换约定（两个 agent 协作用）：

- **daqvision → daqgate**：我（视觉侧）给 daqgate 的对接文档，放在 `daqgate/exchange/`。
- **daqgate → daqvision**：daqgate 侧（WSL/Debian12 agent）给我的文档、回执、问题，放在**本目录** `daqvision/exchange/`。

约定：
- 文件名带日期/主题，如 `2026-06-03-channel-vision-反馈.md`。
- 接口契约本身仍以 `daqvision/proto/vision.proto` 为单一可信源；本目录只放说明/问答/决策，不放契约副本。
