# QiDao Public Release Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已复现的棋盘状态破坏、引擎错误语义和输入崩溃问题，并形成不含敏感材料或 KataGo 大模型的可公开源码仓库。

**Architecture:** 保留现有 SwiftUI → Python 视觉服务 → 围棋状态机 → Rust/UniFFI → KataGo 数据流，只在各信任边界增加明确校验。发布采用源码仓库加用户自备 KataGo 权重；两个自训练轻量 ONNX 继续随源码分发，生产签名与 Sparkle 更新在 fork 专用密钥体系建立前保持禁用。

**Tech Stack:** Swift 6/SwiftUI、Rust 2021/Tokio/UniFFI、Python 3.12/OpenCV/ONNX Runtime、GitHub Actions、Bash。

## Global Constraints

- 只支持 9、13、19 路正方形棋盘。
- 不新增运行时第三方依赖。
- 不修改 `QiDao/QiDao/Core` 生成文件；Rust 变化统一通过 `./build_core.sh` 生成。
- 不提交 `.signing`、证书、私钥、keychain、DMG、归档、静态库、虚拟环境或 KataGo `.bin.gz` 权重。
- 提交 `vision/models/*.onnx` 及其 `vision_models.json`，保留可复现训练说明和 SHA-256。
- 未配置 fork 专用 Sparkle 密钥与受保护发布环境前，不启用生产签名或自动更新工作流。
- 每个非平凡修复必须先有可运行的失败检查，再做最小实现。

---

### Task 1: 固定源码发布边界与模型获取流程

**Files:**
- Modify: `.gitignore`
- Modify: `build_app.command`
- Modify: `README.md`
- Modify: `vision/README.md`
- Modify: `QiDao/QiDao/Info.plist`
- Modify: `QiDao/QiDao/QiDaoApp.swift`
- Modify: `QiDao/QiDao/en.lproj/Localizable.strings`
- Modify: `QiDao/QiDao/zh-Hans.lproj/Localizable.strings`
- Modify: `QiDao/QiDao.xcodeproj/project.pbxproj`
- Modify: `setup.command`
- Modify: `QiDao/QiDao/ScreenAssistManager.swift`
- Modify: `katago/NETWORK_LICENSE.md`
- Modify: `vision/models/vision_models.json`
- Create: `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- Consumes: 本地可选文件 `katago/default_model.bin.gz`。
- Produces: 无大模型也能构建的 `build_app.command`；README 中可执行的 KataGo 配置流程；明确的 Git 忽略边界。

- [ ] **Step 1: 写发布边界失败检查**

运行：

```bash
git check-ignore -q .signing/keychain-password
git check-ignore -q certificate.p12
git check-ignore -q build.keychain
git check-ignore -q QiDao/QiDao/Core/libqidao_core.a
git check-ignore -q katago/default_model.bin.gz
! git check-ignore -q vision/models/board_locator.onnx
! git check-ignore -q vision/models/intersection_classifier.onnx
```

预期：修复前至少 `certificate.p12`、静态库和 KataGo 权重检查失败。

- [ ] **Step 2: 扩充 `.gitignore` 并允许锁文件入库**

加入：

```gitignore
certificate.p12
.env
.env.*
!.env.example
*.p12
*.p8
*.pem
*.key
*.keychain
*.keychain-db
*.provisionprofile
*.mobileprovision
notary_result.txt
*.dmg
*.xcarchive
sparkle.tar.xz
sparkle_tools/
QiDao/QiDao/Core/
katago/*.bin.gz
appcast.xml
.github/workflows/release.yml
/exportOptions.plist
```

删除 `Package.resolved` 忽略规则，使 Swift 依赖解析结果可以提交。

- [ ] **Step 3: 让本地构建接受用户自备 KataGo 模型**

将无条件复制改为：

```bash
if [ -f "$PROJECT_DIR/katago/default_model.bin.gz" ]; then
    cp "$PROJECT_DIR/katago/default_model.bin.gz" "$RESOURCES_DIR/katago/"
else
    echo "未打包 KataGo 权重；启动后请在 AI 引擎设置中选择模型文件"
fi
```

`analysis.cfg` 和 `NETWORK_LICENSE.md` 仍始终复制。

- [ ] **Step 4: 删除 fork 中不可信的上游自动更新入口并补录屏说明**

从 `QiDao/QiDao/Info.plist` 删除 `SUFeedURL` 与 `SUPublicEDKey`，加入：

```xml
<key>NSScreenCaptureUsageDescription</key>
<string>QiDao 仅在用户主动框选后读取棋盘区域，用于实时识别和本地分析。</string>
```

运行：

```bash
plutil -lint QiDao/QiDao/Info.plist
! rg -n "neolee/qidao|SUPublicEDKey|SUFeedURL" QiDao/QiDao/Info.plist
```

预期：plist 合法且没有可执行的上游更新配置。

- [ ] **Step 5: 暂时移除 Sparkle 和本机开发团队绑定**

- 从 `QiDaoApp.swift` 删除 Sparkle import、updater controller 和检查更新菜单。
- 从两个 `Localizable.strings` 删除 `Check for Updates...` 文案。
- 从 `project.pbxproj` 删除 Sparkle build file、framework、package product、remote package 引用。
- 从 `project.pbxproj` 删除四处 `DEVELOPMENT_TEAM = S2D42Y29YT;`，由构建者自行选择团队。

运行：

```bash
! rg -n "Sparkle|SPUStandardUpdaterController|S2D42Y29YT" \
  QiDao/QiDao/QiDaoApp.swift QiDao/QiDao.xcodeproj/project.pbxproj
```

- [ ] **Step 6: 把 Python 环境固定在仓库内**

`setup.command` 使用 `$PROJECT_DIR/.venv/bin/python`，不再创建父目录共享环境。`ScreenAssistManager.locatePython()` 的源码构建候选使用仓库根 `.venv/bin/python`，随后才回退到 Homebrew/系统 Python。

- [ ] **Step 7: 更新安装文档与第三方说明**

README 必须明确写出：

```text
1. 使用 Homebrew 或 KataGo 官方发行版安装可执行程序。
2. 从 KataGo 官方网络页面下载与设备兼容的 .bin.gz 权重。
3. 在 QiDao → AI 引擎设置中分别选择可执行程序、权重和 analysis.cfg。
4. 未配置权重时，棋谱编辑和屏幕识别仍可用，AI 分析保持未配置状态。
5. 两个 ONNX 是本项目用合成数据从随机初始化训练的 MIT 发布资产。
```

把 `vision/README.md` 中的本机绝对 Python 路径改为 `python3`；把 `katago/NETWORK_LICENSE.md` 的 bundled 表述改为推荐下载文件；在 `vision_models.json` 增加 `license: "MIT"`、训练脚本路径和合成数据声明；在 `THIRD_PARTY_NOTICES.md` 记录上游 QiDao、KataGo、网络权重与 ONNX 来源/许可。README 还要准确说明：服务启动时会使用同一个签名 helper 读取 8×8 像素做权限可用性探测，像素不落盘、不上传；完整棋盘只在用户框选后读取。

- [ ] **Step 8: 复查并提交发布边界**

```bash
git check-ignore -v katago/default_model.bin.gz QiDao/QiDao/Core/libqidao_core.a certificate.p12
git diff --check
git add .gitignore build_app.command README.md vision/README.md QiDao/QiDao/Info.plist \
  QiDao/QiDao/QiDaoApp.swift QiDao/QiDao/en.lproj/Localizable.strings \
  QiDao/QiDao/zh-Hans.lproj/Localizable.strings QiDao/QiDao.xcodeproj/project.pbxproj \
  setup.command QiDao/QiDao/ScreenAssistManager.swift katago/NETWORK_LICENSE.md \
  vision/models/vision_models.json THIRD_PARTY_NOTICES.md
git commit -m "build: define safe source release boundary"
```

---

### Task 2: 阻止视觉整盘恢复误删或改色

**Files:**
- Modify: `vision/tests/test_current_position.py`
- Modify: `vision/tests/test_state_machine.py`
- Modify: `vision/vision_service.py`
- Modify: `vision/go_vision/adaptive_vision.py`

**Interfaces:**
- Consumes: `snapshot_verification_agrees(confirmed, candidate, verified) -> bool`、`AdaptiveBoardTracker.analyze(..., expected_color)`。
- Produces: 删除/改色必须得到明确复核；`expected_color` 始终表示下一手方，提子归一化使用其对手作为最后落子方。

- [ ] **Step 1: 增加误删和半数新增的失败测试**

在 `test_current_position.py` 加入：

```python
def test_snapshot_verification_rejects_unknown_deletion(self) -> None:
    confirmed = [list(row) for row in empty_board(9)]
    confirmed[3][3] = Stone.WHITE
    candidate = [row[:] for row in confirmed]
    candidate[3][3] = Stone.EMPTY
    candidate[4][4] = Stone.BLACK
    verified = [row[:] for row in candidate]
    verified[3][3] = Stone.UNKNOWN
    self.assertFalse(snapshot_verification_agrees(
        tuple(map(tuple, confirmed)), tuple(map(tuple, candidate)), tuple(map(tuple, verified))
    ))

def test_snapshot_verification_requires_strict_majority_for_even_additions(self) -> None:
    confirmed = empty_board(9)
    candidate = [list(row) for row in confirmed]
    candidate[2][2] = Stone.BLACK
    candidate[6][6] = Stone.WHITE
    verified = [list(row) for row in candidate]
    verified[6][6] = Stone.UNKNOWN
    self.assertFalse(snapshot_verification_agrees(
        confirmed, tuple(map(tuple, candidate)), tuple(map(tuple, verified))
    ))
```

- [ ] **Step 2: 运行红测**

```bash
python3 -B -m unittest \
  tests.test_current_position.CurrentPositionRecognitionTests.test_snapshot_verification_rejects_unknown_deletion \
  tests.test_current_position.CurrentPositionRecognitionTests.test_snapshot_verification_requires_strict_majority_for_even_additions -v
```

预期：两项都失败，证明旧逻辑会接受证据不足的覆盖。

- [ ] **Step 3: 最小化收紧整盘复核**

在 `snapshot_verification_agrees` 中先验证破坏性变化：

```python
for x, y in transition.changed:
    before = confirmed[y][x]
    after = candidate[y][x]
    if before in (Stone.BLACK, Stone.WHITE) and before != after:
        if verified[y][x] != after:
            return False

if transition.added:
    independently_confirmed = sum(
        verified[move.y][move.x] == move.color for move in transition.added
    )
    return independently_confirmed * 2 > len(transition.added)
```

保留对所有已知矛盾的拒绝检查。

- [ ] **Step 4: 增加最后落子方归一化测试并修复方向**

在 `test_state_machine.py` 构造 `next_color == Stone.WHITE` 的黑方提白快照，断言白子移除且新黑子保留。随后把 `analyze` 改为：

```python
last_mover = Stone.WHITE if expected_color == Stone.BLACK else Stone.BLACK
absolute_board = normalize_snapshot_captures(absolute_board, last_mover)
```

- [ ] **Step 5: 禁止反色重试预先提交虚假停着**

增加测试：反色二次分析抛错时，`move_history`、`board_history`、`move_count`、`next_color` 全部不变。删除 `scan_once` 中在二次分析成功前执行的 `commit_pass()`；颜色无法确认时发 warning 并等待显式 `pass` 或 `setNextPlayer`。

- [ ] **Step 6: 给状态机提交增加颜色不变量**

先写测试，随后在 `AdaptiveBoardTracker.commit` 开头加入：

```python
if move.color != self.next_color:
    raise ValueError("落子颜色与当前行棋方不一致")
```

确认异常路径没有修改任何历史数组。

- [ ] **Step 7: 运行视觉全套测试并提交**

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
git add vision/tests/test_current_position.py vision/tests/test_state_machine.py vision/vision_service.py vision/go_vision/adaptive_vision.py
git commit -m "fix: require complete evidence for board recovery"
```

---

### Task 3: 防止停止后的视觉扫描继续写回

**Files:**
- Modify: `vision/tests/test_current_position.py`
- Modify: `vision/tests/test_protocol.py`
- Create: `vision/tests/test_capture_process.py`
- Modify: `vision/vision_service.py`
- Modify: `vision/go_vision/capture.py`

**Interfaces:**
- Consumes: `VisionService.set_running(bool)`、`scan_once()`、`ScreenController.capture(...)`。
- Produces: start/stop/scan 使用同一状态锁串行化；停止返回后不再有视觉写入；屏幕 helper 超时或异常长度产生 `CaptureError` 而不是永久阻塞。

- [ ] **Step 1: 写停止竞态红测**

使用 `threading.Event` 暂停 fake tracker 的 `analyze`，在另一线程调用 `set_running(False)`。断言 stop 会等待在途扫描；释放 analyze 并等待 stop 返回后，不再产生 `position` 事件，tracker 状态不再变化。

- [ ] **Step 2: 用现有状态锁串行化 start/stop/scan**

`monitor()` 已经在 `_state_lock` 内执行 `scan_once()`；让 `set_running()` 和 shutdown 的 running/closed 变更也持有同一把锁：

```python
def set_running(self, running: bool) -> None:
    with self._state_lock:
        self.running = running
        with self._condition:
            self._condition.notify_all()
```

这样 stop 最多等待一个受 capture deadline 限制的扫描，返回后不会有旧扫描提交；无需新增第二套会话状态。

- [ ] **Step 3: 写 shutdown 协议红测并修复循环退出**

在 `test_protocol.py` 启动服务，发送 `shutdown` 后保持 stdin 打开，使用 `process.wait(timeout=2)`，预期旧代码超时。修复 `run()`：每次 `execute(command)` 后若 `self.closed` 立即退出读取循环。

- [ ] **Step 4: 写屏幕 helper 超时与长度红测**

`test_capture_process.py` 使用临时可执行 Python helper：一个读取请求后不输出，另一个返回超过 `MAX_CAPTURE_BYTES` 的长度。断言 capture 在设定期限内抛 `CaptureError`，进程被回收，`close()` 能返回。

- [ ] **Step 5: 给 helper I/O 增加 deadline 和大小上限**

在 `capture.py` 定义：

```python
CAPTURE_IO_TIMEOUT = 2.0
MAX_CAPTURE_BYTES = 128 * 1024 * 1024
```

用 `selectors.DefaultSelector` 等待 stdout 可读；header 限长 4096 字节，宽高必须为正，payload 必须等于协议声明且不超过上限。任何超时或协议错误都调用现有 `_stop_capture_process()` 后抛 `CaptureError`。

- [ ] **Step 6: 运行视觉测试并提交**

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
git add vision/tests/test_current_position.py vision/tests/test_protocol.py vision/tests/test_capture_process.py vision/vision_service.py vision/go_vision/capture.py
git commit -m "fix: isolate visual scan lifecycles"
```

---

### Task 4: 加固 Rust 棋盘和 KataGo 子进程边界

**Files:**
- Modify: `qidao-core/src/lib.rs`
- Generated by script: `QiDao/QiDao/Core/`（保持 Git 忽略）

**Interfaces:**
- Consumes: `Game::new(size)`、`Game::from_sgf(content)`、设置子编辑 API、`AnalysisEngine::stop()`。
- Produces: 仅接受 9/13/19；祖先编辑后不返回旧缓存；停止引擎最长等待 2 秒后强制终止；UTF-8 日志安全截断。

- [ ] **Step 1: 准备 Rust 工具链并记录版本**

```bash
brew install rust
rustc --version
cargo --version
```

若工具链安装未获授权，停止本任务并明确标记 Rust 未验证，不能进入最终发布提交。

- [ ] **Step 2: 写棋盘尺寸和缓存失效红测**

在 `lib.rs` 末尾增加 `#[cfg(test)] mod tests`，至少包含：

```rust
#[test]
fn rejects_unsupported_sgf_size() {
    let error = Game::from_sgf("(;GM[1]FF[4]SZ[20])".into())
        .err()
        .expect("20x20 SGF must be rejected");
    assert!(error.to_string().contains("9, 13, or 19"));
}

#[test]
fn editing_ancestor_invalidates_descendant_board_cache() {
    let game = Game::new(19).unwrap();
    game.place_stone(3, 3, StoneColor::Black).unwrap();
    let child = game.get_current_node();
    assert!(game.go_back());
    game.add_stone(4, 4, StoneColor::White);
    game.jump_to_node(child);
    assert_eq!(game.get_board().get_stone(4, 4), Some(StoneColor::White));
}
```

```bash
cargo test --manifest-path qidao-core/Cargo.toml
```

预期：尺寸或缓存测试失败。

- [ ] **Step 3: 集中棋盘尺寸验证并清空陈旧缓存**

增加：

```rust
fn validate_board_size(size: u32) -> Result<u32, SgfError> {
    match size {
        9 | 13 | 19 => Ok(size),
        _ => Err(SgfError::ParseError {
            message: "QiDao supports square board sizes 9, 13, or 19".into(),
        }),
    }
}
```

`Game::new` 改为返回 `Result<Arc<Game>, SgfError>`；`from_sgf` 拒绝矩形和非法尺寸。`recalculate_board_internal` 在重放当前路径前执行 `state.board_cache.clear()`，从根重建当前路径，确保所有后代缓存失效。

- [ ] **Step 4: 写并实现 UTF-8 安全截断**

测试中文字符串在第 500 字节附近不会 panic。实现：

```rust
fn truncate_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}
```

替换两处 `&value[..500]`。

- [ ] **Step 5: 写挂死子进程停止测试并实现超时 kill**

使用忽略 stdin EOF 的假进程启动 `AnalysisEngine`，断言 `stop()` 在 3 秒内返回。实现时先释放 stdin/stdout/stderr，再：

```rust
match tokio::time::timeout(Duration::from_secs(2), child.wait()).await {
    Ok(result) => { result.map_err(to_sgf_error)?; }
    Err(_) => {
        child.kill().await.map_err(to_sgf_error)?;
        child.wait().await.map_err(to_sgf_error)?;
    }
}
```

- [ ] **Step 6: 运行 Rust 测试、生成桥接并提交源码**

```bash
cargo fmt --manifest-path qidao-core/Cargo.toml -- --check
cargo test --locked --manifest-path qidao-core/Cargo.toml
./build_core.sh
git diff --check
git add qidao-core/src/lib.rs qidao-core/Cargo.toml qidao-core/Cargo.lock build_core.sh
git commit -m "fix: validate core and engine boundaries"
```

---

### Task 5: 区分 AI 停着与错误并消除 Swift 输入崩溃

**Files:**
- Create: `QiDao/QiDao/AITrustBoundary.swift`
- Create: `tests/swift/main.swift`
- Modify: `QiDao/QiDao/AIManager.swift`
- Modify: `QiDao/QiDao/AIManager+Analysis.swift`
- Modify: `QiDao/QiDao/BoardViewModel+Play.swift`
- Modify: `QiDao/QiDao/GameManager.swift`
- Modify: `QiDao/QiDao/GameBoardView.swift`
- Modify: `QiDao/QiDao/AIConfigView.swift`
- Modify: `QiDao/QiDao/BoardComponents.swift`
- Modify: `QiDao/QiDao/BoardViewModel.swift`

**Interfaces:**
- Consumes: KataGo move string、用户跳转手数、显示候选数、core 的可失败 Game 构造器。
- Produces: `AIMoveDecision` 的 `.move(x:y:)`、`.pass`、`.failure(message:)`、`.cancelled`；共享的坐标和整数范围校验。

- [ ] **Step 1: 写可独立编译的 Swift 信任边界红测**

`tests/swift/main.swift` 使用断言：

```swift
assert(AITrustBoundary.parseMove("Q16", boardSize: 19) == .move(x: 15, y: 3))
assert(AITrustBoundary.parseMove("PASS", boardSize: 19) == .pass)
assert(AITrustBoundary.parseMove("A20", boardSize: 19) == .failure("AI 返回越界坐标：A20"))
assert(AITrustBoundary.validatedMoveNumber(-1, maximum: 100) == nil)
assert(AITrustBoundary.candidateCount(-5) == 1)
```

运行：

```bash
swiftc QiDao/QiDao/AITrustBoundary.swift tests/swift/main.swift -o /tmp/qidao-swift-safety
/tmp/qidao-swift-safety
```

预期：新类型尚不存在，编译失败。

- [ ] **Step 2: 实现纯 Swift 边界类型**

```swift
enum AIMoveDecision: Equatable {
    case move(x: Int, y: Int)
    case pass
    case failure(String)
    case cancelled
}

enum AITrustBoundary {
    static func validatedMoveNumber(_ value: Int, maximum: Int) -> Int? {
        (0...maximum).contains(value) ? value : nil
    }

    static func candidateCount(_ value: Int) -> Int {
        min(100, max(1, value))
    }
}
```

`parseMove` 使用不含 `I` 的 19 路列映射，并校验 `0..<boardSize`。

- [ ] **Step 3: 让 AI 请求返回显式结果**

`requestAIMove` 改为 `async -> AIMoveDecision`。只有字符串 `PASS` 返回 `.pass`；超时、无结果、协议错误返回 `.failure(...)`；Task 取消返回 `.cancelled`。调用端使用完整 switch，只有 `.pass` 调用 `pass(isAI: true)`，错误仅更新日志和状态。

- [ ] **Step 4: 修复引擎 EOF 高频重试**

`startResultPolling` 对包含 `Timeout` 的错误继续等待；其他错误记录一次、设置 `aiStatus = .error`、`isEngineStarted = false` 并退出轮询。不得在 EOF 后每 10 ms 重试。

- [ ] **Step 5: 将所有用户输入和数组访问接入共享校验**

- `GameManager.jumpToMove`：无效值直接返回，不做 `UInt32` 转换。
- `GameBoardView`：使用 `AITrustBoundary.candidateCount` 后再调用 `prefix`。
- `AIConfigView`：保存时夹紧到 `1...100`。
- `BoardComponents`：不支持的 gridSize 不索引 19 字母表，坐标绘制直接跳过。
- `GameManager`：适配 `try Game(size:)`，UI 只传 9/13/19；构造失败回退为 19 路并记录错误。

- [ ] **Step 6: 运行 Swift 边界测试和应用构建**

```bash
swiftc QiDao/QiDao/AITrustBoundary.swift tests/swift/main.swift -o /tmp/qidao-swift-safety
/tmp/qidao-swift-safety
./build_app.command
test -x .build/QiDao.app/Contents/MacOS/QiDao
```

- [ ] **Step 7: 提交 Swift 修复**

```bash
git diff --check
git add QiDao/QiDao/AITrustBoundary.swift tests/swift/main.swift \
  QiDao/QiDao/AIManager.swift QiDao/QiDao/AIManager+Analysis.swift \
  QiDao/QiDao/BoardViewModel+Play.swift QiDao/QiDao/GameManager.swift \
  QiDao/QiDao/GameBoardView.swift QiDao/QiDao/AIConfigView.swift \
  QiDao/QiDao/BoardComponents.swift QiDao/QiDao/BoardViewModel.swift
git commit -m "fix: make AI and UI trust boundaries explicit"
```

---

### Task 6: 建立无签名密钥的持续集成与仓库验收

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `scripts/verify_repository.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: Git 暂存清单、Python/Rust/Swift 测试命令。
- Produces: 不接触 Apple 或 Sparkle 密钥的 pull/push CI；本地和 CI 共用的发布前仓库检查。

- [ ] **Step 1: 写仓库验收脚本**

脚本必须使用 `git ls-files` 并拒绝以下跟踪项：

```bash
forbidden='(^|/)(\.signing|target|\.build|DerivedData|sparkle_tools)(/|$)|\.(p12|p8|pem|key|keychain-db|dmg)$|default_model\.bin\.gz$|libqidao_core\.a$|appcast\.xml$|release\.yml$'
if git ls-files | grep -E "$forbidden"; then
    echo "检测到禁止发布的文件" >&2
    exit 1
fi
```

另检查：不存在 macOS 用户目录绝对路径、不存在私钥 PEM 头、`board_locator.onnx` 与 `intersection_classifier.onnx` 和 `vision_models.json` 声明的 SHA-256 一致、所有跟踪文件小于 50 MiB。

- [ ] **Step 2: 先证明脚本能拦截故意暂存的假敏感路径**

在临时 Git 仓库复制脚本并跟踪 `certificate.p12`，断言脚本退出非零；删除假文件后断言退出零。测试不得向真实仓库添加敏感文件。

- [ ] **Step 3: 创建最小 CI**

`.github/workflows/ci.yml` 仅使用 `contents: read`，在 pull request 和 main push 上执行。唯一 action 使用固定提交 `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`，不使用浮动 tag：

```text
repository-audit: scripts/verify_repository.sh
python-vision: 安装 vision/requirements.txt 后运行 unittest discover
rust-core: stable Rust + cargo fmt --check + cargo test
swift-boundary: swiftc AITrustBoundary.swift tests/swift/main.swift 后执行
```

CI 不读取任何 secrets，不签名、不公证、不生成 appcast、不创建 Release。

- [ ] **Step 4: README 增加发布状态说明**

明确源码 CI 与生产发布分离：当前仓库没有官方签名 DMG 和自动更新；用户自行构建或等待维护者建立 fork 专用签名体系。

- [ ] **Step 5: 运行验收并提交**

```bash
chmod +x scripts/verify_repository.sh
scripts/verify_repository.sh
git diff --check
git add .github/workflows/ci.yml scripts/verify_repository.sh README.md
git commit -m "ci: verify the public source release"
```

---

### Task 7: 全量回归、显式暂存与 GitHub 推送

**Files:**
- Modify only if verification reveals a defect: files owned by Tasks 1-6
- Stage: all remaining source, tests, docs, permitted resources, lock files and two ONNX models

**Interfaces:**
- Consumes: Tasks 1-6 的已验证结果。
- Produces: `origin/main` 上可公开克隆的完整源码提交，不含任何禁止文件。

- [ ] **Step 1: 运行全量测试与构建**

```bash
python3 -B -m unittest discover -s vision/tests -p 'test_*.py' -v
cargo fmt --manifest-path qidao-core/Cargo.toml -- --check
cargo test --locked --manifest-path qidao-core/Cargo.toml
swiftc QiDao/QiDao/AITrustBoundary.swift tests/swift/main.swift -o /tmp/qidao-swift-safety
/tmp/qidao-swift-safety
./build_app.command
```

- [ ] **Step 2: 显式暂存允许发布的项目文件**

先执行 `git status --short --ignored`，再只暂存源码目录、文档、构建脚本、配置、许可证和模型清单。不得使用会绕过忽略规则的 `git add -f`。

```bash
git add .github AGENTS.md INIT.md LICENSE.md MEMO.md README.md THIRD_PARTY_NOTICES.md \
  QiDao app build_app.command build_core.sh build_vision.sh \
  katago/analysis.cfg katago/NETWORK_LICENSE.md qidao-core resources setup.command \
  setup_signing.command tools vision docs tests scripts .gitignore
```

- [ ] **Step 3: 对暂存快照执行最终安全检查**

```bash
scripts/verify_repository.sh
git diff --cached --check
git diff --cached --name-only
git ls-files -z | xargs -0 du -h | sort -h | tail -20
```

逐项确认 `.signing`、`default_model.bin.gz`、`libqidao_core.a`、`appcast.xml`、`release.yml` 不在输出，两个 `.onnx` 在输出。

- [ ] **Step 4: 提交完整源码快照**

```bash
git commit -m "feat: publish real-time QiDao analysis"
git status --short
```

预期：只剩被明确忽略的本机产物；没有未跟踪的必要源码。

- [ ] **Step 5: 验证 GitHub 身份并推送**

```bash
gh auth status -h github.com
git remote -v
git push origin main
git ls-remote origin refs/heads/main
```

若 `gh auth status` 失败，用户在自己的终端运行 `gh auth login -h github.com`；不得通过聊天传递访问令牌。

- [ ] **Step 6: 核验远端提交内容**

确认远端 main SHA 等于本地 `git rev-parse HEAD`，并用 GitHub 文件列表复核 README、源码、CI、ONNX 均存在且禁止文件均不存在。
