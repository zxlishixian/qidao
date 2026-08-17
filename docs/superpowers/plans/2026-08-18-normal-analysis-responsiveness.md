# Normal Analysis Responsiveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ordinary Analysis mode preempt background work and present its first valid KataGo result without a settings click, including while QiDao is inactive.

**Architecture:** Treat ordinary and live current-position analysis as one interactive priority class. `AIManager` terminates residual full-scan work before every interactive query; `BoardViewModel` owns a generic first-result presentation gate, performs one asynchronous window invalidation, and resumes the ordinary full-game scan only after that first result.

**Tech Stack:** Swift 5, SwiftUI, AppKit, Combine, KataGo Analysis API through `qidao-core`, embedded Python fake engine smoke tests.

## Global Constraints

- Modify at most three production files: `AIManager.swift`, `BoardViewModel.swift`, and `BoardViewModel+ScreenAssist.swift`.
- Do not modify Rust, Python vision, generated bindings, model files, KataGo configuration, Xcode settings, or UI layout.
- Add no timer, polling loop, thread, third-party dependency, or synchronous AppKit display call.
- Never activate QiDao or make its window key to present a result.
- Keep ordinary ownership and policy output enabled according to the user's existing configuration.
- Use test-first RED → GREEN and commit the regression before production changes.

---

### Task 1: Reproduce ordinary-query starvation at the engine boundary

**Files:**
- Modify: `tools/smoke_live_ai_priority.swift:5-175`

**Interfaces:**
- Consumes: `AIManager.startFullGameAnalysis(...)`, `AIManager.updateAnalysis(..., fastResponse:)`, and the Analysis API JSON Lines protocol.
- Produces: one smoke executable that verifies both live and ordinary interactive queries terminate `fullscan-<session>` before submission and receive a first result within five seconds.

- [ ] **Step 1: Make the fake engine model full-scan contention**

Replace the embedded fake loop with stateful protocol behavior. A full-scan query sets `full_scan_active = True`; its matching terminate command clears the flag; a `qidao-*` query emits the fixed D4 result only when the full scan is no longer active.

```python
full_scan_active = False
for line in sys.stdin:
    message = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(message, separators=(",", ":")) + "\n")
        log.flush()

    action = message.get("action")
    query_id = message.get("id", "")
    if action == "terminate" and message.get("terminateId", "").startswith("fullscan-"):
        full_scan_active = False
        continue
    if action is None and query_id.startswith("fullscan-"):
        full_scan_active = True
        continue
    if action is None and query_id.startswith("qidao-") and not full_scan_active:
        turn = message.get("analyzeTurns", [0])[0]
        result = {
            "id": query_id,
            "turnNumber": turn,
            "isDuringSearch": True,
            "noResults": False,
            "rootInfo": {"winrate": 0.62, "scoreLead": 2.0, "visits": 8},
            "moveInfos": [{
                "move": "D4", "visits": 8, "winrate": 0.62,
                "scoreLead": 2.0, "pv": ["D4"],
            }],
        }
        print(json.dumps(result, separators=(",", ":")), flush=True)
```

- [ ] **Step 2: Add the ordinary-analysis scenario**

After the existing live scenario, start another full scan and submit a new current node with `fastResponse: false`. Use literal protocol expectations: the terminate message must occur between the latest full-scan query and ordinary `qidao-*` query; the ordinary query must use priority `30`; `reportDuringSearchEvery` must be at most `0.25`; and D4 must arrive within five seconds.

```swift
manager.startFullGameAnalysis(
    mainLineMoves: [["B", "D4"], ["W", "Q16"]],
    initialStones: [], metadata: metadata, config: config, initialPlayer: "B"
)
// Wait until the second fullscan query appears, then submit ordinary analysis.
manager.updateAnalysis(
    currentNodeId: "ordinary-priority",
    initialStones: [],
    moves: [["B", "D4"], ["W", "Q16"]],
    nextPlayer: "B", initialPlayer: "B", turnNumber: 2,
    metadata: metadata, config: config, fastResponse: false
)
```

Add distinct `SmokeError` cases for missing ordinary result, missing ordinary terminate ordering, wrong priority, and slow report cadence. Do not assert on fake-engine method calls; assert the real JSON protocol log and the real `AIManager.analysisResult` publication.

- [ ] **Step 3: Compile and verify RED**

```bash
mkdir -p /private/tmp/qidao-normal-analysis/module-cache
sdk_path="$(xcrun --sdk macosx --show-sdk-path)"
find QiDao/QiDao -name '*.swift' ! -name 'QiDaoApp.swift' -print0 | xargs -0 swiftc \
  -parse-as-library -sdk "$sdk_path" -target arm64-apple-macosx14.0 \
  -module-cache-path /private/tmp/qidao-normal-analysis/module-cache \
  -I QiDao/QiDao/Core/qidao_coreFFI \
  -Xcc -fmodule-map-file=QiDao/QiDao/Core/qidao_coreFFI/module.modulemap \
  QiDao/QiDao/Core/libqidao_core.a \
  -framework SwiftUI -framework AppKit -framework Foundation \
  tools/smoke_live_ai_priority.swift \
  -o /private/tmp/qidao-normal-analysis/ai-priority-red
/private/tmp/qidao-normal-analysis/ai-priority-red
```

Expected: the live scenario passes, then the ordinary scenario fails because the active full scan is not terminated and the fake engine withholds D4. If it reaches query validation first, priority `10` or report interval `1.0` must fail instead.

- [ ] **Step 4: Commit the engine-boundary regression**

```bash
git add tools/smoke_live_ai_priority.swift
git commit -m "test: reproduce stalled ordinary analysis"
```

---

### Task 2: Reproduce the missing ordinary window presentation

**Files:**
- Modify: `tools/smoke_live_board_refresh.swift:11-22,201-344`

**Interfaces:**
- Consumes: `BoardViewModel.updateAnalysis()`, `AIManager.analysisResult`, and `BoardViewModel.refreshLiveWindowsIfNeeded(force:)` through a visible non-key `NSWindow`.
- Produces: a regression proving that the first ordinary result requests asynchronous window display without invoking `displayIfNeeded()`.

- [ ] **Step 1: Extend the AppKit display probe**

Count asynchronous display invalidations separately from forbidden synchronous display calls:

```swift
private final class SynchronousDisplayProbeView: NSView {
    private(set) var displayIfNeededCalls = 0
    private(set) var displayInvalidationCalls = 0

    override func displayIfNeeded() {
        displayIfNeededCalls += 1
        super.displayIfNeeded()
    }

    override func setNeedsDisplay(_ flag: Bool) {
        if flag { displayInvalidationCalls += 1 }
        super.setNeedsDisplay(flag)
    }

    func reset() {
        displayIfNeededCalls = 0
        displayInvalidationCalls = 0
    }
}
```

- [ ] **Step 2: Add a non-live ordinary-result scenario**

Create a second `BoardViewModel` with no screen-assist baseline, a visible borderless window whose content view is only the probe, and `appMode == .analysis`. Set `isAnalyzing = true`, call `updateAnalysis()`, publish a literal current-node `AnalysisResult`, and service only `.eventTracking` mode for at most 500 ms.

Assert all three user-visible contracts:

```swift
guard ordinaryViewModel.analysisResult?.moveInfos.first?.moveStr == "D4" else {
    fatalError("Ordinary analysis result did not reach BoardViewModel")
}
guard ordinaryDisplayProbe.displayInvalidationCalls > 0 else {
    fatalError("First ordinary analysis result waited for a settings click")
}
guard ordinaryDisplayProbe.displayIfNeededCalls == 0 else {
    fatalError("Ordinary analysis reintroduced synchronous display")
}
```

The production mutation this catches is removing the generic first-result pending flag or changing its forced asynchronous invalidation back to live-only behavior.

- [ ] **Step 3: Compile and verify RED**

Use the same `swiftc` command from Task 1, replacing the tool input and output:

```bash
sdk_path="$(xcrun --sdk macosx --show-sdk-path)"
find QiDao/QiDao -name '*.swift' ! -name 'QiDaoApp.swift' -print0 | xargs -0 swiftc \
  -parse-as-library -sdk "$sdk_path" -target arm64-apple-macosx14.0 \
  -module-cache-path /private/tmp/qidao-normal-analysis/module-cache \
  -I QiDao/QiDao/Core/qidao_coreFFI \
  -Xcc -fmodule-map-file=QiDao/QiDao/Core/qidao_coreFFI/module.modulemap \
  QiDao/QiDao/Core/libqidao_core.a \
  -framework SwiftUI -framework AppKit -framework Foundation \
  tools/smoke_live_board_refresh.swift \
  -o /private/tmp/qidao-normal-analysis/board-refresh-red
/private/tmp/qidao-normal-analysis/board-refresh-red
```

Expected: fail with `First ordinary analysis result waited for a settings click`; existing live-board and synchronous-display checks must reach this scenario successfully.

- [ ] **Step 4: Commit the presentation regression**

```bash
git add tools/smoke_live_board_refresh.swift
git commit -m "test: reproduce ordinary analysis presentation stall"
```

---

### Task 3: Implement the shared interactive-analysis path

**Files:**
- Modify: `QiDao/QiDao/AIManager.swift:148-259`
- Modify: `QiDao/QiDao/BoardViewModel.swift:267-270,347-420,594-655`
- Modify: `QiDao/QiDao/BoardViewModel+ScreenAssist.swift:133-168`
- Test: `tools/smoke_live_ai_priority.swift`
- Test: `tools/smoke_live_board_refresh.swift`

**Interfaces:**
- Consumes: validated current node IDs, `AIManager.analysisSessionId`, existing `fullscan-*` and `qidao-*` protocol IDs, and the asynchronous `refreshLiveWindowsIfNeeded(force:)` invalidation.
- Produces: `awaitingFirstAnalysisResult: Bool`, `pendingAnalysisIsLive: Bool`, full-scan preemption for every interactive query, and one-shot ordinary result presentation.

- [ ] **Step 1: Give every interactive query the fast engine boundary**

In `AIManager.updateAnalysis(...)`:

- Change ordinary debounce from `500_000_000` ns to `120_000_000` ns; retain `20_000_000` ns for live analysis.
- Always call `engine.terminate(id: "fullscan-\(analysisSessionId)")` before terminating the old interactive ID and submitting the new query.
- Set interactive query `priority` to literal `30` for both modes.
- Compute the interactive report interval as `min(analysisSettings.reportDuringSearchEvery ?? 0.25, fastResponse ? 0.05 : 0.25)` and include it when it is at least `0.001`.
- Preserve ordinary `includeOwnership` and `includePolicy`; preserve all max-visits, max-time, and advanced-parameter behavior.

Do not change `startFullGameAnalysis` budgets or priorities.

- [ ] **Step 2: Generalize first-result state in BoardViewModel**

Replace `awaitingFirstLiveAIResult` with:

```swift
var awaitingFirstAnalysisResult = false
var pendingAnalysisIsLive = false
```

In `updateAnalysis()` compute the existing live-session predicate, then when analysis is enabled:

```swift
awaitingFirstAnalysisResult = true
pendingAnalysisIsLive = isLiveScreenAnalysis
stopFullGameAnalysis()
```

Submit the current query as before, but remove the immediate ordinary `startFullGameAnalysis()` call. Also remove the separate `startFullGameAnalysis()` call from the `isEngineReady` subscription because `updateAnalysis()` now controls recovery.

- [ ] **Step 3: Present the first current result and resume ordinary background work**

In the throttled `aiManager.$analysisResult` sink, recognize only a non-empty current-node result while `awaitingFirstAnalysisResult` is true. Capture whether this request was live, clear both pending fields, publish the result, and call:

```swift
self.refreshLiveWindowsIfNeeded(force: isFirstCurrentResult)
if isFirstCurrentResult,
   !wasLiveRequest,
   self.appMode == .analysis,
   self.config.display.showWinRateGraph {
    self.startFullGameAnalysis()
}
```

Stale node IDs and empty results must not clear the pending state or restart the full scan. Subsequent partial results remain bounded by the existing 120 ms Combine throttle and do not force explicit AppKit invalidation.

- [ ] **Step 4: Remove live-only ownership of the pending flag**

In `finishLivePositionSync`, remove its assignment to the old live-only flag. Keep `beginAIResponseTiming()` and `startAnalysisForLivePositionIfNeeded()` unchanged; the shared `updateAnalysis()` call now establishes the generic pending state. Keep model ACK and asynchronous board refresh behavior unchanged.

- [ ] **Step 5: Compile and verify GREEN**

Recompile both Task 1 and Task 2 smoke executables from current source, then run them.

Expected:

- `Live AI priority OK` output now includes ordinary current-position preemption.
- `Live board refresh OK` includes ordinary first-result invalidation.
- Both exit 0, use no direct synchronous display, and complete each first-result wait within five seconds.

- [ ] **Step 6: Commit the production fix**

```bash
git add QiDao/QiDao/AIManager.swift \
  QiDao/QiDao/BoardViewModel.swift \
  QiDao/QiDao/BoardViewModel+ScreenAssist.swift
git commit -m "fix: prioritize and present ordinary analysis"
```

---

### Task 4: Full verification and real inactive validation

**Files:**
- Verify: all files changed in Tasks 1-3.

**Interfaces:**
- Consumes: green focused smokes and the signed local app.
- Produces: evidence that ordinary analysis updates without opening settings and all existing subsystems remain green.

- [ ] **Step 1: Run complete automated regressions**

```bash
PYTHONPATH=vision python3 -B -m unittest discover -s vision/tests -p 'test_*.py' -v
CARGO_TARGET_DIR=/private/tmp/qidao-normal-analysis-rust cargo test --locked --manifest-path qidao-core/Cargo.toml
bash scripts/verify_repository.sh
bash scripts/verify_ci_policy.sh
bash tests/scripts/test_release_audits.sh
git diff --check
```

Expected: 94 Python tests, 41 Rust tests, repository/CI/release audits, and whitespace validation all pass.

- [ ] **Step 2: Build and verify the app**

```bash
./build_app.command
codesign --verify --deep --strict --verbose=2 .build/QiDao.app
```

Expected: a valid signed `$PWD/.build/QiDao.app` containing the current source build.

- [ ] **Step 3: Perform controlled ordinary-analysis UI validation**

Launch only `$PWD/.build/QiDao.app`, select ordinary Analysis mode, start the configured engine, place one controlled local test stone, and immediately keep Chrome frontmost for five seconds. Confirm the current node receives a candidate list and engine status without opening AI settings, QiDao does not become frontmost, and no direct `displayIfNeeded` appears in a five-second process sample.

Do not click an external online board intersection during this validation. Restore or close the local test position afterward.

---

### Task 5: Documentation, handoff, and delivery state

**Files:**
- Modify: `MEMO.md`
- Create: `handoff.md`
- Modify: `docs/superpowers/plans/2026-08-18-normal-analysis-responsiveness.md`

**Interfaces:**
- Consumes: verified root cause, exact commits, test outputs, and any remaining limitations.
- Produces: a zero-context handoff and an accurate development ledger.

- [ ] **Step 1: Update MEMO.md**

Add one concise completed entry covering ordinary full-scan preemption, generic first-result presentation, async-only window invalidation, and the focused regression names.

- [ ] **Step 2: Write handoff.md for a new session**

Create `handoff.md` with these concrete sections and current facts:

- `我们在做什么`: project and ordinary-analysis bug scope.
- `完成了什么`: root cause, implementation, commits, and verification counts.
- `卡在哪儿`: state `无阻塞` when all checks pass, otherwise exact failing command and output.
- `下一步计划`: launch/use instructions and any unpushed delivery action.
- `采过哪些坑不要再踩`: do not use focus activation, settings clicks, timers, synchronous AppKit display, live-only pending flags, or Swift-task cancellation without KataGo terminate.

Do not include absolute private engine/model paths, signing identities, credentials, or local audit branch contents.

- [ ] **Step 3: Mark this plan complete and review the exact staged diff**

Change every completed checkbox in this file to `[x]`. Review `git diff --cached --name-status` and ensure only source, smoke, documentation, and handoff files are staged; no model, generated binding, build output, signing material, or local audit branch may be included.

- [ ] **Step 4: Commit documentation**

```bash
git add MEMO.md handoff.md \
  docs/superpowers/plans/2026-08-18-normal-analysis-responsiveness.md
git commit -m "docs: hand off ordinary analysis hardening"
```

Do not push unless the user explicitly authorizes this task's commits to update `origin/main`.
