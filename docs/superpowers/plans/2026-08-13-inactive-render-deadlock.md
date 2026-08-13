# Inactive Render Deadlock Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep recognized positions and KataGo results advancing while QiDao is inactive by removing synchronous WindowServer waits and eliminating static-heartbeat UI churn.

**Architecture:** Treat a position as committed when the authoritative QiDao model and `boardCells` match, then ACK it immediately. SwiftUI rendering becomes an asynchronous consequence of published state; static scan heartbeats update only the overlay geometry, and the stdout callback explicitly wakes the main run loop.

**Tech Stack:** Swift 5, SwiftUI, AppKit, Combine, Core Foundation run loop, Python/OpenCV vision JSON protocol, Rust `qidao-core` through UniFFI.

## Global Constraints

- Modify at most three production files: `ScreenAssistManager.swift`, `BoardViewModel.swift`, and `BoardViewModel+ScreenAssist.swift`.
- Do not change Python recognition logic, KataGo search parameters, screen-recording permissions, Xcode settings, or UI layout.
- Add no thread, timer, third-party dependency, or separate window.
- Never activate QiDao or make its window key while refreshing.
- Never synchronously wait for AppKit, SwiftUI, RenderBox, QuartzCore, or WindowServer presentation.
- Keep models, signing files, generated bindings, and build outputs out of Git.

---

### Task 1: Reproduce the inactive render deadlock and heartbeat storm

**Files:**
- Modify: `tools/smoke_live_board_refresh.swift`

**Interfaces:**
- Consumes: `BoardViewModel.refreshLiveWindowsIfNeeded(force:)`, `ScreenAssistManager.handleVisionMessage(_:)`, `ScreenAssistManager.appliedSequence`, and `ScreenAssistManager.objectWillChange`.
- Produces: a smoke executable that fails if a live position invokes synchronous content display, waits to ACK the model, or publishes unchanged scan telemetry.

- [ ] **Step 1: Add a synchronous-display probe and heartbeat assertions**

Add a content wrapper that records direct synchronous display calls:

```swift
private final class SynchronousDisplayProbeView: NSView {
    private(set) var displayIfNeededCalls = 0

    override func displayIfNeeded() {
        displayIfNeededCalls += 1
        super.displayIfNeeded()
    }

    func reset() {
        displayIfNeededCalls = 0
    }
}
```

Mount the existing `NSHostingView` inside this wrapper, perform the one intentional initial `window.displayIfNeeded()`, then reset the counter. After the protocol `position` event, require all of the following before servicing a mouse event:

```swift
guard viewModel.displayedStone(x: 5, y: 2) == .black else {
    fatalError("Live model update waited for inactive presentation")
}
guard manager.appliedSequence == 1 else {
    fatalError("Authoritative live position was not acknowledged immediately")
}
RunLoop.main.run(until: Date().addingTimeInterval(0.10))
guard displayProbe.displayIfNeededCalls == 0 else {
    fatalError("Inactive live refresh invoked synchronous displayIfNeeded")
}
```

Subscribe to `manager.objectWillChange`, reset the count after baseline/running setup, send 30 valid scan events containing `unchanged: true`, monotonically increasing `scanSequence`, the same move number and next player, and assert that the publication count remains zero and the UI-facing `scanSequence` remains at its prior value.

Update the hidden-window assertion to require immediate model ACK even when no content window is present; presentation is no longer part of the protocol contract.

- [ ] **Step 2: Compile and verify RED**

Compile the smoke with the same complete Swift source set and local `qidao_coreFFI` used by the existing verification workflow, then run it.

Expected failure on commit `19f37a7`: either `Authoritative live position was not acknowledged immediately`, `Inactive live refresh invoked synchronous displayIfNeeded`, or `Static heartbeats published UI state`.

- [ ] **Step 3: Commit the regression only**

```bash
git add tools/smoke_live_board_refresh.swift
git commit -m "test: reproduce inactive live render stall"
```

---

### Task 2: Decouple protocol progress from asynchronous rendering

**Files:**
- Modify: `QiDao/QiDao/BoardViewModel.swift:267-272,431-488`
- Modify: `QiDao/QiDao/BoardViewModel+ScreenAssist.swift:133-174`
- Modify: `QiDao/QiDao/ScreenAssistManager.swift:159-186,681-723`
- Test: `tools/smoke_live_board_refresh.swift`

**Interfaces:**
- Consumes: validated `ScreenBoardPosition`, published `boardCells`, `reportQiDaoPositionApplied(board:moveNumber:sequence:)`, and `refreshLiveWindowsIfNeeded(force:)`.
- Produces: immediate model ACK, nonblocking window invalidation, quiet static heartbeats, and explicit main-run-loop wakeup.

- [ ] **Step 1: Make window refresh asynchronous and nonblocking**

Delete `liveWindowRefreshNeedsCommit` and `liveWindowRefreshCompletions`. Change the signature to:

```swift
func refreshLiveWindowsIfNeeded(force: Bool = false)
```

Keep the existing monitoring guard and 100 ms throttle for non-forced AI progress. Coalesce one main-queue block that clears `liveWindowRefreshScheduled` and only performs:

```swift
for window in NSApp.windows where window.isVisible && !(window is NSPanel) {
    window.contentView?.needsLayout = true
    window.contentView?.needsDisplay = true
}
```

Do not call `layoutSubtreeIfNeeded`, `displayIfNeeded`, `window.display`, `NSApp.updateWindows`, `CATransaction.flush`, activation APIs, or completion callbacks.

- [ ] **Step 2: ACK immediately after authoritative model commit**

In `finishLivePositionSync`, keep board equality and sequence idempotency checks. After confirming `screenBoardSnapshot() == position.board`, call:

```swift
screenAssistManager.reportQiDaoPositionApplied(
    board: applied,
    moveNumber: position.moveNumber,
    sequence: position.sequence
)
```

Then start/update live analysis once for a new sequence and call `refreshLiveWindowsIfNeeded(force: true)`. Replayed equal sequences may request an asynchronous invalidation but must neither restart KataGo nor wait for presentation.

- [ ] **Step 3: Stop unchanged heartbeats from publishing SwiftUI state**

In the `scan` case, validate the message, then handle `unchanged == true` before assigning any `@Published` property:

```swift
guard AITrustBoundary.isValidVisionScan(message, boardSize: boardSize) else { return }
if message["unchanged"] as? Bool == true {
    refreshTrackingOverlay()
    break
}
guard let nextScanSequence = message["scanSequence"] as? Int,
      let nextMoveNumber = message["moveNumber"] as? Int,
      let nextPlayerValue = message["nextPlayer"] as? String else { return }
```

Only non-heartbeat scans update scan sequence, tracking/performance diagnostics, candidate state, and status text.

- [ ] **Step 4: Wake the main run loop after stdout delivery**

Immediately after scheduling the stdout data block on `DispatchQueue.main`, add:

```swift
CFRunLoopWakeUp(CFRunLoopGetMain())
```

This must not activate QiDao or change the frontmost application.

- [ ] **Step 5: Compile and verify GREEN**

Rebuild and run `tools/smoke_live_board_refresh.swift`.

Expected: exit 0; immediate ACK, zero synchronous display calls, zero static-heartbeat publications, consecutive position/correction/capture assertions, inactive AI result, and hidden-model commit all pass.

- [ ] **Step 6: Commit the production fix**

```bash
git add QiDao/QiDao/BoardViewModel.swift \
  QiDao/QiDao/BoardViewModel+ScreenAssist.swift \
  QiDao/QiDao/ScreenAssistManager.swift
git commit -m "fix: remove inactive render deadlock"
```

---

### Task 3: Full regression, real inactive validation, and delivery

**Files:**
- Modify: `MEMO.md`
- Modify: `docs/superpowers/plans/2026-08-13-inactive-render-deadlock.md`
- Verify: all files changed in Tasks 1 and 2.

**Interfaces:**
- Consumes: green live-board smoke and existing AI-priority smoke.
- Produces: verified local app bundle and an ordinary fast-forward update of `origin/main`.

- [ ] **Step 1: Run focused Swift verification**

Compile all Swift application sources including `QiDaoApp.swift`. Run the trust-boundary executable, updated live-board smoke, and `tools/smoke_live_ai_priority.swift`.

Expected: every command exits 0; the board smoke reports the new no-sync-display/quiet-heartbeat checks and the AI smoke reports a first result under five seconds.

- [ ] **Step 2: Run complete regressions and audits**

```bash
PYTHONPATH=vision /Users/horseli/code/.venv/bin/python -B -m unittest discover -s vision/tests -p 'test_*.py' -v
CARGO_TARGET_DIR=/private/tmp/qidao-inactive-render-rust cargo test --locked --manifest-path qidao-core/Cargo.toml
bash scripts/verify_repository.sh
bash scripts/verify_ci_policy.sh
bash tests/scripts/test_release_audits.sh
git diff --check
```

Expected: 94 Python vision tests, 41 Rust tests, all repository/release audits, and whitespace validation pass.

- [ ] **Step 3: Build and perform real inactive validation**

Run `./build_app.command` and verify the local signature with the project keychain. Launch only `/Users/horseli/code/qidao/.build/QiDao.app`, keep Chrome or WeChat frontmost, and observe the already selected board without clicking QiDao.

Confirm:

- QiDao never becomes frontmost.
- Tracking overlays remain visible.
- A simulated or controlled board change reaches QiDao within one second.
- A process sample no longer shows repeated project-triggered `displayIfNeeded` calls or a replay-driven RenderBox wait loop.

- [ ] **Step 4: Update the ledger and review the exact diff**

Add one concise `MEMO.md` entry describing the confirmed synchronous-render deadlock, immediate model ACK, quiet heartbeats, and regression coverage. Review every changed hunk and ensure no models, signing data, generated bindings, build output, or local audit branch is staged.

- [ ] **Step 5: Commit documentation and push**

```bash
git add MEMO.md \
  docs/superpowers/plans/2026-08-13-inactive-render-deadlock.md
git commit -m "docs: record inactive refresh hardening"
git push origin main
```

Verify `git rev-parse HEAD` equals `git ls-remote origin refs/heads/main`. Never force push and never push `local/audit-history-do-not-push`.
