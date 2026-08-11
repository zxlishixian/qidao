# QiDao 实时棋盘视觉模块

该目录是 QiDao 的屏幕输入层，不是独立应用。用户拖选其他软件中的围棋棋盘后，视觉服务持续输出经过确认的棋局状态；`BoardViewModel+ScreenAssist.swift` 将合法变化写入 QiDao 棋局树，原有 KataGo Analysis Engine 随后给出胜率、目数差和候选落点。

## 识别链路

1. **完整网格定位**：在用户拉框内拟合 9/13/19 路周期网格，得到精确的四个外侧交叉点。
2. **YOLO-ONNX 交叉验证**：轻量单类别 YOLO 风格模型判断棋盘粗框。它只验证网格结果，或者在全局网格搜索失败时缩小恢复范围；模型框与完整网格不一致时绝不覆盖网格坐标。
3. **透视归一化**：把棋盘变换到固定间距的正方形画布，避免窗口尺寸、Retina 缩放和轻微透视造成坐标偏移。
4. **ONNX 交叉点分类**：一次批量判断所有交叉点的 `empty / black / white / unknown`。每个补丁先独立做色彩均值/标准差归一化，防止模型把浅色木板误认成白棋。
5. **双视觉复核**：传统中心/圆周几何分类器处理最新落子红点、相邻棋子边缘和模型域外不确定性。
6. **围棋状态机**：只有颜色顺序正确、落点合法、提子结果一致、没有重复局面，并连续稳定两帧的单一新着，才会提交给 QiDao。重新拟合完整网格后坐标不一致的候选会被丢弃。

Swift 侧把“拉框、首帧载入、启动持续扫描”串成一次操作。视觉服务发布首个 `baseline` 局面后，QiDao 立即更新主棋盘并发送 `start`；后续每个通过状态机的 `position` 事件都会写入同一棋局树，由原有分析订阅自动请求 KataGo 重分析。

运行时只需要 `requirements.txt` 中的 NumPy、OpenCV 和 Pillow；ONNX 由 OpenCV DNN 加载。PyTorch 仅用于训练，不会随应用启动。

## 模型训练

当前权重没有复制 Kaya/Moku、Ultralytics 或其他第三方模型。Moku 的公开模型卡未声明许可证、训练数据和评测，因此本项目只借鉴“检测 + 几何校正”的思路。两个模型均从随机初始化开始，用本目录生成的屏幕域合成数据训练。

```bash
cd qidao
python3 -m venv .venv-train
.venv-train/bin/pip install -r vision/requirements-training.txt

cd vision
../.venv-train/bin/python -m ml.synthetic_data \
  --output ../.artifacts/vision-training
../.venv-train/bin/python -m ml.train \
  --data ../.artifacts/vision-training \
  --output models \
  --checkpoints ../.artifacts/vision-checkpoints \
  --detector-epochs 24 \
  --classifier-epochs 16 \
  --device mps
```

固定随机种子、数据规模、类别混淆矩阵、独立验证/测试指标、ONNX 输入输出形状和 SHA-256 均记录在 `models/vision_models.json`。合成指标只能验证实现与分布隔离，不能替代真实客户端测试。

## 当前验证

- 合成独立测试：定位平均 IoU `0.7397`，IoU≥0.5 召回 `95.19%`，空白拒绝 `100%`；交叉点四分类 `100%`（测试配色与训练配色隔离）。
- 用户真实截图：星阵双子局面只识别 `Q16 黑 / P7 白`，最新手 `P7`；星阵单子局面只识别 `Q16 黑`；星阵和腾讯围棋空盘均为 361 点全空，所有场景未知点为 0。
- 跨截图连续帧：单子局面到双子局面，第一帧只发布候选 `P7`，第二帧才接纳 `P7 白`，已知棋子一致率 100%。
- `PYTHONPATH=vision python -m unittest discover -s vision/tests -v` 覆盖非空局面初始化、稳定帧、新着/多着拒绝、提子、重复局面和 JSON-lines 服务协议。

## 降级与安全边界

模型文件缺失或 OpenCV 无法加载 ONNX 时，服务自动退回传统 OpenCV 分类和网格定位。无论使用哪个识别器，视觉结果都不能绕过围棋状态机，也不会自动点击或控制第三方下棋软件。
