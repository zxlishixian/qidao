# QiDao 失焦实时刷新修复设计

## 目标

QiDao 窗口保持失焦且不抢回焦点时，真实棋盘的新局面和 KataGo 的首个分析结果都应在 1 秒内自动显示。用户不需要点击棋盘、AI 设置或任何 QiDao 控件。

## 根因

实时局面已经同步到 `GameManager` 后，`finishLivePositionSync` 仍将“确认同步并启动 AI”放在强制窗口绘制的完成回调中。失焦窗口执行 `displayIfNeeded`、`NSApp.updateWindows` 或 `CATransaction.flush` 时可能等待窗口合成，因而阻塞后续局面消息与 AI 请求。

AI 状态和分析结果又通过 `RunLoop.main` 调度。该调度依赖主 RunLoop 模式，失焦期间可能积压，直到用户点击 QiDao 触发新的界面事件。

## 修复设计

- 棋盘状态写入、同步确认和 AI 请求属于逻辑事务，不等待窗口是否已经绘制；局面校验成功后立即完成这三步。
- 窗口刷新只在主队列异步合并，并对内容视图调用 `displayIfNeeded()`；不等待 `window.displayIfNeeded`、`NSApp.updateWindows` 或 Core Animation 强制 flush。逻辑同步和 AI 请求在此之前已经完成。
- `AIManager` 已受 `@MainActor` 保护，因此去除多余的 `RunLoop.main` 跳转；高频分析结果继续以 120 ms 节流，但调度器改为 `DispatchQueue.main`，保证失焦时也能被 GCD 唤醒。
- 不激活应用、不改变窗口层级、不增加常驻定时器。

## 数据流

`vision position → applyScreenPosition → GameManager.syncState → BoardViewModel 快照发布 → 确认已同步 → 提交 KataGo 查询 → AIManager 发布结果 → BoardViewModel/悬浮提示更新`

界面绘制是该数据流的非阻塞旁路；绘制暂时变慢不能阻断下一手识别或 AI 请求。

## 验证

- 扩充现有 Swift smoke，证明局面同步确认和 AI 请求不依赖绘制完成回调。
- 在非默认 RunLoop 模式下发布 AI 状态/结果，证明 `BoardViewModel` 无需鼠标事件即可收到。
- 验证连续局面、完整局面恢复、提子和失焦窗口刷新仍通过。
- 运行 Swift 边界测试、实时棋盘 smoke、AI 启动 smoke（具备本机 KataGo 资源时）、Rust 与视觉回归，以及仓库审计。

## 非目标

- 不修改视觉定位、棋子分类或围棋状态机。
- 不通过抢焦点、提高窗口层级或高频定时强刷规避问题。
- 不改变 KataGo 的分析参数和棋力配置。
