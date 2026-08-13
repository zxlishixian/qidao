# QiDao Inactive Live Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make live board positions and KataGo analysis results appear within one second while QiDao remains inactive, without stealing focus.

**Architecture:** Keep board synchronization and AI submission on the main actor, but make them independent of AppKit painting. Treat window repaint as a coalesced, asynchronous side effect and deliver Combine publications on `DispatchQueue.main` rather than a run-loop-mode-dependent scheduler.

**Tech Stack:** Swift 6 / SwiftUI / AppKit / Combine, qidao-core UniFFI, macOS smoke executables.

## Global Constraints

- QiDao must not activate itself or steal focus.
- No polling timer, new dependency, vision-model change, state-machine change, or KataGo strength/configuration change.
- The user-visible board and first AI result must update within one second while QiDao is inactive.
- Do not modify generated files under `QiDao/QiDao/Core`.
- Keep the production change within `BoardViewModel.swift` and `BoardViewModel+ScreenAssist.swift`.

---

### Task 1: Decouple live synchronization from window painting

**Files:**
- Modify: `tools/smoke_live_board_refresh.swift`
- Modify: `QiDao/QiDao/BoardViewModel+ScreenAssist.swift`
- Modify: `QiDao/QiDao/BoardViewModel.swift`

**Interfaces:**
- Consumes: `applyScreenPosition(_:requestAnalysis:)`, `reportQiDaoPositionApplied(board:moveNumber:sequence:)`, `refreshLiveWindowsIfNeeded(force:completion:)`.
- Produces: immediate logical acknowledgement followed by a nonblocking call to `refreshLiveWindowsIfNeeded(force: true)`.

- [ ] **Step 1: Write the failing synchronization regression**

Add a recognized 9×9 position to `tools/smoke_live_board_refresh.swift`, call `applyScreenPosition(..., requestAnalysis: false)`, and immediately assert—without advancing `RunLoop.main`—that `screenAssistManager.isSyncedToQiDao` is true and `syncedMoveNumber` matches the position. This must catch any future reintroduction of a paint-completion dependency.

```swift
viewModel.applyScreenPosition(immediatePosition, requestAnalysis: false)
guard viewModel.screenAssistManager.isSyncedToQiDao,
      viewModel.screenAssistManager.syncedMoveNumber == 25 else {
    fatalError("Live synchronization waited for an inactive-window paint")
}
```

- [ ] **Step 2: Run the smoke to verify RED**

Compile all non-entry-point app Swift sources plus `tools/smoke_live_board_refresh.swift` against `QiDao/QiDao/Core/libqidao_core.a`, using the macOS 15.4 SDK and an isolated module cache. Run the binary.

Expected: exit nonzero with `Live synchronization waited for an inactive-window paint` because acknowledgement currently occurs in an asynchronously scheduled paint completion.

- [ ] **Step 3: Implement immediate logical completion**

In `finishLivePositionSync`, after confirming `screenBoardSnapshot() == position.board`, call `reportQiDaoPositionApplied`, start response timing, and invoke `startAnalysisForLivePositionIfNeeded()` synchronously on the main actor. Then request repaint independently:

```swift
screenAssistManager.reportQiDaoPositionApplied(
    board: applied,
    moveNumber: position.moveNumber,
    sequence: position.sequence
)
if requestAnalysis {
    screenAssistManager.beginAIResponseTiming()
    startAnalysisForLivePositionIfNeeded()
}
refreshLiveWindowsIfNeeded(force: true)
```

- [ ] **Step 4: Remove synchronous paint work**

Unify both branches of `refreshLiveWindowsIfNeeded` around one coalesced `DispatchQueue.main.async` invalidation. For visible non-panel windows, set `needsLayout` and `needsDisplay`, then call only `contentView.displayIfNeeded()`; do not call `layoutSubtreeIfNeeded`, `window.displayIfNeeded`, `NSApp.updateWindows`, or `CATransaction.flush`. No logical caller may wait for this paint.

- [ ] **Step 5: Run the smoke to verify GREEN**

Recompile and run the same smoke binary.

Expected: exit 0 and output beginning `Live board refresh OK`.

---

### Task 2: Deliver AI publications independently of run-loop mode

**Files:**
- Modify: `tools/smoke_live_board_refresh.swift`
- Modify: `QiDao/QiDao/BoardViewModel.swift`

**Interfaces:**
- Consumes: `AIManager.$engineMessage`, `AIManager.$analysisResult`, `BoardViewModel.analysisResult`.
- Produces: Combine delivery scheduled through `DispatchQueue.main`, retaining 120 ms latest-result throttling.

- [ ] **Step 1: Write the failing AI-result regression**

In the smoke, construct a literal `AnalysisResult` for the view model's current node, assign it to `viewModel.aiManager.analysisResult`, and advance only `.eventTracking` run-loop mode for up to 500 ms. Assert that `viewModel.analysisResult?.rootInfo.visits == 7` and that the live manager receives the `G7` candidate.

```swift
let result = AnalysisResult(
    id: "qidao-0-\(viewModel.currentNodeId)", turnNumber: UInt32(viewModel.moveCount),
    isDuringSearch: true, noResults: false,
    rootInfo: AnalysisRootInfo(winrate: 0.61, scoreLead: 2.5, visits: 7),
    moveInfos: [AnalysisMoveInfo(moveStr: "G7", visits: 7, winrate: 0.61, scoreLead: 2.5, pv: ["G7"])],
    ownership: nil
)
viewModel.aiManager.analysisResult = result
```

- [ ] **Step 2: Run the smoke to verify RED**

Compile and run the smoke as in Task 1.

Expected: exit nonzero because `.receive(on: RunLoop.main)` / RunLoop-based throttling does not deliver during the simulated inactive event-tracking interval.

- [ ] **Step 3: Implement main-queue delivery**

Remove redundant `.receive(on: RunLoop.main)` operators from main-actor `AIManager` bindings. Change only the high-frequency analysis throttle scheduler:

```swift
.throttle(for: .milliseconds(120), scheduler: DispatchQueue.main, latest: true)
```

Keep candidate sorting, stale-result guards, and the 120 ms cap unchanged.

- [ ] **Step 4: Run the smoke to verify GREEN**

Recompile and run the smoke.

Expected: exit 0; the board acknowledgement is immediate and the AI result is relayed under `.eventTracking` mode without a QiDao click.

---

### Task 3: Regression, repository verification, commit and push

**Files:**
- Modify: `MEMO.md` only to record the completed root-cause fix after verification.
- Verify: all modified source, smoke, spec, and plan files.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: one reviewed Git commit on `main`, fast-forwarded to `origin/main`.

- [ ] **Step 1: Run focused Swift checks**

Run the standalone trust-boundary executable and the rebuilt live-board smoke. Compile the complete app Swift source set against the generated UniFFI module. If local KataGo executable/config/model paths are available, run `tools/smoke_ai_live_start.swift`; otherwise report that resource-dependent smoke as not run rather than claiming it passed.

- [ ] **Step 2: Run full regression and audits**

```bash
PYTHONPATH=vision /Users/horseli/code/.venv/bin/python -B -m unittest discover -s vision/tests -p 'test_*.py' -v
CARGO_TARGET_DIR=/private/tmp/qidao-inactive-refresh-rust cargo test --locked --manifest-path qidao-core/Cargo.toml
bash scripts/verify_repository.sh
bash scripts/verify_ci_policy.sh
bash tests/scripts/test_release_audits.sh
git diff --check
```

Expected: 94 vision tests, 41 Rust tests, repository/CI/release audits, and diff checks all pass.

- [ ] **Step 3: Review exact diff and update ledger**

Confirm no source outside `BoardViewModel.swift` and `BoardViewModel+ScreenAssist.swift` changed. Add a concise `MEMO.md` entry with the root cause, invariant, and test evidence. Ensure no generated Core files, signing material, model weights, or build output are staged.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-12-inactive-live-refresh-design.md \
  docs/superpowers/plans/2026-08-12-inactive-live-refresh.md \
  QiDao/QiDao/BoardViewModel.swift QiDao/QiDao/BoardViewModel+ScreenAssist.swift \
  tools/smoke_live_board_refresh.swift MEMO.md
git commit -m "fix: refresh live analysis while inactive"
```

- [ ] **Step 5: Push and verify remote identity**

```bash
git push origin main
git ls-remote origin refs/heads/main
git rev-parse HEAD
```

Expected: ordinary fast-forward push; remote `refs/heads/main` equals local `HEAD`. Do not force push or push `local/audit-history-do-not-push`.
