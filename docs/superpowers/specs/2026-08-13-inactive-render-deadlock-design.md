# QiDao 失焦实时刷新彻底修复设计

## 目标

在 QiDao 保持失焦、用户持续操作外部棋盘的情况下：识别到的新局面在 1 秒内写入分析棋盘，主线程不等待 WindowServer，同一局面不会因显示确认失败而无限重放，AI 分析请求不依赖点击 QiDao 才推进。

## 已确认根因

真实进程采样显示，失焦时 QiDao 主线程停在 `RenderBox -> CAContext waitForCommit`。当前实时局面路径主动调用 `layoutSubtreeIfNeeded()` 和 `displayIfNeeded()`，把本应异步的 SwiftUI/WindowServer 提交变成同步等待。显示等待期间视觉服务收不到 ACK，于是每 400 ms 重放同一局面；重放再次安排同步显示，形成反馈循环。

此外，视觉服务的 `unchanged` 心跳约每 120 ms 到达一次。Swift 入口在判断 `unchanged` 之前更新 `scanSequence`、追踪分数和耗时等 `@Published` 属性，因此即使棋盘没有变化，主窗口仍持续失效和提交图层，进一步挤压真正的 `position` 消息。

`pmset` 证明 QiDao 在实时分析期间持有 `PreventUserIdleSystemSleep` 活动声明，因此 App Nap 不是根因。

## 方案

### 1. 模型提交与像素显示解耦

`finishLivePositionSync` 只以 `boardCells` 已等于视觉局面作为写入成功条件。成功后立即调用 `reportQiDaoPositionApplied`，发送该 `positionSequence` 的 ACK，并立即启动或更新 KataGo。

协议 ACK 表示“QiDao 的权威分析模型已经接受局面”，不再表示“WindowServer 已经显示像素”。像素提交不应成为跨进程状态机的一部分。

### 2. 窗口刷新只做异步失效

`refreshLiveWindowsIfNeeded` 保留现有 100 ms AI 结果节流，只给可见主窗口的内容视图设置 `needsLayout` 和 `needsDisplay`。删除两阶段 completion、`layoutSubtreeIfNeeded()`、`displayIfNeeded()` 以及全部同步显示等待。

局面变更仍通过 `boardCells`、`gameState`、`boardRevision` 的 `@Published` 更新驱动 SwiftUI。`.id(boardRevision)` 保证棋盘树获得新身份；AppKit 失效标记只负责唤醒正常的异步绘制周期。

### 3. 静态心跳不触发主窗口发布

`scan` 消息通过信任边界校验并更新非发布的跟踪四边形后，首先判断 `unchanged`。静态心跳只刷新悬浮框位置并退出，不更新 `scanSequence`、性能数据、追踪分数或棋盘摘要等 `@Published` 属性。

真正包含候选变化、视觉差异或局面变化的消息继续更新现有状态。`position` 消息始终完整更新局面和序号。

### 4. 明确唤醒主 RunLoop

视觉 stdout 的后台可读回调把数据提交到 `DispatchQueue.main` 后，显式调用 `CFRunLoopWakeUp(CFRunLoopGetMain())`。这不激活 QiDao、不抢焦点，只确保已经排入主线程的协议数据不会等待下一次鼠标事件。

## 数据流

1. Python 识别出稳定的新局面并输出 `position`。
2. `FileHandle.readabilityHandler` 读取数据，排入主线程并唤醒主 RunLoop。
3. `ScreenAssistManager` 校验消息并调用 `BoardViewModel.applyScreenPosition`。
4. `qidao-core` 应用单手或重建完整局面；`GameManager` 发布不可变快照。
5. `BoardViewModel` 更新 `boardCells`、`gameState` 和 `boardRevision`。
6. 局面一致后立即 ACK，并提交 KataGo 实时分析请求。
7. SwiftUI/AppKit 在正常异步显示周期渲染，不阻塞协议和 AI 链路。

## 测试设计

- 在真实 `NSHostingView` 中安装一个会记录同步显示调用的内容视图；实时局面刷新不得调用 `displayIfNeeded()` 或同步布局。
- 连续发送至少 30 个 `unchanged` 心跳，验证不会推进 UI 发布序号，也不会产生主窗口 `objectWillChange` 风暴。
- 从后台队列模拟 stdout 到达，验证主 RunLoop 无需鼠标事件即可在 1 秒内应用局面和 ACK。
- 连续局面、完整纠错、提子、隐藏后恢复和首个 AI 候选测试继续通过。
- 实际构建后让 Chrome/微信保持前台，检查 QiDao 不抢焦点，追踪悬浮框持续存在，主线程采样中不再出现由项目代码主动触发的同步 `displayIfNeeded` 等待循环。

## 范围与约束

- 修改不超过三个生产源文件：`ScreenAssistManager.swift`、`BoardViewModel.swift`、`BoardViewModel+ScreenAssist.swift`。
- 不修改 Python 视觉判定、KataGo 搜索参数、录屏权限、Xcode 设置或 UI 布局。
- 不新增线程、定时器、第三方依赖或独立窗口。
- 不调用 `NSApp.activate`、`makeKeyAndOrderFront` 等抢焦点 API。
- 不上传模型、签名或构建产物。
