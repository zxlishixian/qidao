# QiDao 失焦实时刷新与 AI 首结果提速设计

## 目标

QiDao 保持失焦且不抢回焦点时：

- 视觉服务确认真实棋盘变化后，QiDao 分析棋盘在约 1 秒内显示同一局面。
- KataGo 已完成冷启动的前提下，QiDao 在局面同步后约 5 秒内显示首个有效候选点。
- 用户不需要点击 QiDao 棋盘、AI 设置或其他控件来触发刷新。

## 当前证据

上一轮修复已经把局面确认和 AI 请求从窗口绘制回调中解耦，并将分析结果调度从 `RunLoop.main` 改为 `DispatchQueue.main`。当前构建产物包含这些代码，因此剩余问题不是“用户仍在运行旧代码”。

代码审查发现两个仍然存在的链路缺口：

1. `refreshLiveWindowsIfNeeded` 在一次主队列回调中同时标记并立即绘制内容视图。SwiftUI 的 `objectWillChange` 可能尚未完成视图树事务，此时绘制的仍是旧棋盘；失焦后没有后续窗口事件，直到用户点击 QiDao 才提交新视图。
2. `stopFullGameAnalysis()` 只取消本地 Swift `Task`，没有向 KataGo 发送 `terminate`。已经提交的 `fullscan-*` 查询仍会占用神经网络批次，实时查询虽然优先级较高，首个结果仍可能被拖慢。

第一点必须由真实 `NSHostingView` 失焦回归测试确认后再修改；第二点可由模拟 KataGo 协议记录命令顺序确认。

## 设计

### 两阶段主窗口刷新

局面写入、同步确认和 AI 请求继续作为主 Actor 上的逻辑事务立即完成。窗口呈现作为独立旁路：

1. 合并同一时间段内的刷新请求，避免每个视觉心跳或 KataGo 局部结果都重复绘制。
2. 第一阶段只标记 QiDao 主内容视图需要布局和显示，然后返回主 RunLoop，让 SwiftUI 完成状态事务。
3. 约一个显示帧后执行第二阶段，仅对可见的非 `NSPanel` 主窗口调用 `layoutSubtreeIfNeeded()` 和 `displayIfNeeded()`。
4. 不调用 `activate`、`makeKeyAndOrderFront`、`NSApp.updateWindows` 或 `CATransaction.flush`，不改变焦点和窗口层级，也不增加常驻定时器。

棋盘局面更新使用强制刷新；AI 的首个有效结果也使用强制刷新。后续高频分析结果仍保留 120ms 节流和合并。

### 实时 AI 查询抢占

只要屏幕分析已经取得基线或正在监控，该局面都按实时查询处理：

- 取消本地全局分析任务。
- 在同一个交互分析任务中，先向 KataGo 发送 `terminate`，终止当前 session 的 `fullscan-*` 查询。
- 再终止上一条 `qidao-*` 查询并提交当前局面；命令顺序固定，避免两个异步任务竞争。
- 保留实时查询的高优先级、20ms 防抖、50ms 搜索中报告间隔，以及关闭 ownership/policy 的轻量配置。
- 不降低用户配置的最终计算量；5 秒指标针对首个有效搜索中结果，不包含 KataGo 冷启动。

## 数据流与时限

`视觉 position → GameManager/BoardViewModel 写入 → 立即确认同步 → 提交高优先级 KataGo 查询 → 两阶段窗口呈现`

- 棋盘显示时限：从合法 `position` 进入 Swift 侧到真实 SwiftUI 棋盘更新，目标不超过 1 秒。
- AI 显示时限：从已启动 KataGo 收到当前局面查询到首个包含候选点的结果显示，目标不超过 5 秒。

## 测试与验收

1. 扩展实时棋盘 Swift smoke，使用真实 `NSHostingView` 和 `NSViewRepresentable` 探针；只运行非默认 RunLoop 模式，证明失焦窗口在 1 秒内渲染最新 revision。测试必须在当前单阶段实现上失败。
2. 增加模拟 KataGo 协议 smoke，先制造 `fullscan-*`，再提交实时局面；断言 `terminate fullscan-*` 先于新的 `qidao-*` 查询，并在 5 秒内收到首个候选。测试必须在当前实现上失败。
3. 修复后运行 Swift smoke、完整 Swift 编译、94 个视觉测试、41 个 Rust 测试以及仓库/敏感文件审计。
4. 构建新的 `.build/QiDao.app`，确认二进制包含新方法签名；普通 fast-forward 推送 `main`，不上传模型、签名材料、生成 Core 文件或本地审计分支。

## 非目标

- 不调整棋盘定位、交叉点分类或围棋状态机。
- 不用高频定时强刷、抢焦点或提高主窗口层级来掩盖问题。
- 不把 KataGo 冷启动时间纳入 5 秒首结果指标。
- 不修改用户的 KataGo 棋力和最终访问量配置。
