# data/ — 数据与模型区（默认不入库）

体积大、可重建/敏感，**整个 data/ 已在 .gitignore 忽略**（除本 README）。
建议放本机/NAS，跨机用 rsync 同步，不要进 git。

```
data/
  datasets/<场景>/{images,labels}/   # 原始样本 + 标注（如 datasets/helmet/）
  calib/<场景>/                       # RKNN INT8 量化校准图（现场实拍 几十~200 张）
  models/<场景>/<版本>/
      best.pt        # 训练产物
      model.onnx     # 导出
      model.rknn     # 量化后板上模型
      modelcard.md   # 模型卡：数据来源/类别/输入尺寸/量化方式/PC精度vs板上/FPS
  snapshots/                          # 运行期告警抓拍
```

约定：
- **校准集用现场实拍图**，代表性决定量化精度。
- 每个模型配一张 `modelcard.md`。
- 标注工具：LabelImg / X-AnyLabeling。
