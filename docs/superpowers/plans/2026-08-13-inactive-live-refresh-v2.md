# QiDao Inactive Live Refresh V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render each confirmed screen-board position within one second and the first valid warm KataGo result within five seconds while QiDao remains inactive and never steals focus.

**Architecture:** Preserve the existing main-actor position reconciliation, but split inactive-window presentation into an invalidation phase and a delayed layout/display phase so SwiftUI can commit its state transaction first. Treat every screen-assist baseline/monitoring position as a fast interactive query and explicitly terminate the session's `fullscan-*` query before submitting the new `qidao-*` query.

**Tech Stack:** Swift 6, SwiftUI, AppKit, Combine, qidao-core UniFFI, a Python JSON-lines fake Analysis Engine used only by a Swift smoke executable.

## Global Constraints

- QiDao must remain inactive and must not steal focus.
- A confirmed position must be visible within one second.
- With KataGo already warm, the first valid candidate must be visible within five seconds.
- Do not add polling timers, dependencies, model changes, or KataGo strength changes.
- Do not modify generated files under `QiDao/QiDao/Core`.
- Do not upload model weights, signing material, generated Core files, build output, or `local/audit-history-do-not-push`.

---

### Task 1: Prove and fix deferred SwiftUI presentation

**Files:**
- Modify: `tools/smoke_live_board_refresh.swift`
- Modify: `QiDao/QiDao/BoardViewModel.swift`
- Modify: `QiDao/QiDao/BoardViewModel+ScreenAssist.swift`

**Interfaces:**
- Consumes: `BoardViewModel.boardRevision`, `refreshLiveWindowsIfNeeded(force:)`, `finishLivePositionSync(_:requestAnalysis:)`, `AIManager.$analysisResult`.
- Produces: coalesced two-phase inactive-window presentation and a forced presentation for the first current-position AI result.

- [x] **Step 1: Replace the outer draw-count probe with a real SwiftUI revision probe**

Add an `NSViewRepresentable` to `tools/smoke_live_board_refresh.swift`. Its `updateNSView` stores the `BoardViewModel.boardRevision` that SwiftUI actually committed:

```swift
private final class LiveRevisionProbeView: NSView {
    var renderedRevision: UInt64 = 0
}

private struct LiveRevisionProbe: NSViewRepresentable {
    let revision: UInt64
    let probe: LiveRevisionProbeView

    func makeNSView(context: Context) -> LiveRevisionProbeView { probe }

    func updateNSView(_ nsView: LiveRevisionProbeView, context: Context) {
        nsView.renderedRevision = revision
    }
}

private struct LiveBoardSmokeRoot: View {
    @ObservedObject var viewModel: BoardViewModel
    let probe: LiveRevisionProbeView

    var body: some View {
        ZStack {
            GameBoardView(viewModel: viewModel, size: 500)
            LiveRevisionProbe(revision: viewModel.boardRevision, probe: probe)
                .frame(width: 1, height: 1)
        }
    }
}
```

After the protocol `position`, service only `.eventTracking` mode for at most one second and require `probe.renderedRevision == viewModel.boardRevision`. Keep the existing immediate logical-sync assertion.

- [x] **Step 2: Run the live-board smoke and verify RED**

Compile all app Swift sources except `QiDaoApp.swift` plus the smoke against `QiDao/QiDao/Core/libqidao_core.a`, with `swiftc -j 1`, the macOS 15.4 SDK, target `arm64-apple-macosx14.0`, and an isolated module cache.

Observed: the real SwiftUI revision committed under `.eventTracking`, disproving a pure SwiftUI-publication failure. The regression was refined to require that `positionSequence` remain unacknowledged until presentation. It then failed with `Vision position was acknowledged before inactive-window presentation`, exposing the missing replay/self-recovery window.

- [x] **Step 3: Implement the two-phase presentation**

Keep `liveWindowRefreshScheduled` true across both phases. In `refreshLiveWindowsIfNeeded(force:)`, phase one asynchronously marks visible non-panel content views as needing layout/display. Schedule phase two approximately 20ms later; phase two calls only:

```swift
contentView.layoutSubtreeIfNeeded()
contentView.needsDisplay = true
contentView.displayIfNeeded()
```

Do not call `NSApp.activate`, `makeKeyAndOrderFront`, `window.displayIfNeeded`, `NSApp.updateWindows`, or `CATransaction.flush`.

Add `awaitingFirstLiveAIResult` to `BoardViewModel`. Set it in `finishLivePositionSync` before requesting analysis. In the analysis-result sink, clear it and call `refreshLiveWindowsIfNeeded(force: true)` only when the result has candidates and its ID suffix matches the current node; subsequent partial results keep the ordinary throttled refresh.

- [x] **Step 4: Rebuild and verify GREEN**

Recompile and run the smoke.

Expected: exit 0; immediate logical synchronization still passes, the real SwiftUI revision commits within one second in `.eventTracking` mode, and the first AI result does not require a click.

---

### Task 2: Prove and fix live-query starvation

**Files:**
- Create: `tools/smoke_live_ai_priority.swift`
- Modify: `QiDao/QiDao/BoardViewModel.swift`
- Modify: `QiDao/QiDao/AIManager.swift`

**Interfaces:**
- Consumes: `AIManager.start`, `startFullGameAnalysis`, `updateAnalysis(...fastResponse:)`, `AnalysisEngine.terminate(id:)`, `ScreenAssistManager.hasBaseline`.
- Produces: deterministic `terminate fullscan-* → terminate old qidao-* → current qidao-* query` order and a first current-position result within five seconds.

- [x] **Step 1: Add a fake Analysis Engine smoke**

Create `tools/smoke_live_ai_priority.swift`. Start `AIManager` with `/usr/bin/python3 -u -c <program>`. The embedded Python program must:

- append every received JSON object to a temporary JSON-lines log;
- keep `fullscan-*` queries open without emitting a result;
- accept terminate commands;
- immediately emit one valid `isDuringSearch` result with a `D4` candidate for every `qidao-*` query.

Use this protocol body, with the temporary log path passed as `sys.argv[1]`:

```python
import json
import sys

log_path = sys.argv[1]
for line in sys.stdin:
    message = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(message, separators=(",", ":")) + "\n")
        log.flush()
    query_id = message.get("id", "")
    if message.get("action") is None and query_id.startswith("qidao-"):
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

The Swift smoke starts one full-game scan, waits until its query is logged, calls `updateAnalysis(...fastResponse: true)`, then waits no longer than five seconds for `analysisResult`. It asserts that a terminate command whose `terminateId` is the session's `fullscan-*` occurs after the logged fullscan query and before the current qidao query.

Use `AIConfig.default` with `maxVisits = 50` and `reportDuringSearchEvery = 0.05`. Call `manager.stopFullGameAnalysis()` immediately before `manager.updateAnalysis(...fastResponse: true)` to reproduce the production path. Parse the temporary log with `JSONSerialization`, find the fullscan query and current qidao query indices, and require a message satisfying:

```swift
message["action"] as? String == "terminate"
    && message["terminateId"] as? String == "fullscan-\(manager.analysisSessionId)"
```

strictly between those two indices.

- [x] **Step 2: Run the priority smoke and verify RED**

Compile the same app source set plus `tools/smoke_live_ai_priority.swift` and run it.

Expected: nonzero exit with `Live analysis did not terminate the active full scan before querying` because current `stopFullGameAnalysis()` cancels only the Swift task.

- [x] **Step 3: Implement deterministic live-query priority**

In `BoardViewModel.updateAnalysis`, define live mode as:

```swift
let isLiveScreenAnalysis = screenAssistManager.hasBaseline
    || screenAssistManager.isMonitoring
    || screenAssistManager.isReRecognizing
```

This makes the initial baseline query fast and prevents starting a full scan while the screen-assist session owns the analysis board.

In `AIManager.updateAnalysis`, inside the existing `interactiveTask` and before terminating `currentAnalysisId`, add:

```swift
if fastResponse {
    try? await engine.terminate(id: "fullscan-\(self.analysisSessionId)")
}
```

The terminate and current query therefore use one task and one ordered stdin path. Keep the existing 20ms debounce, priority 30, 50ms partial-report interval, visits, and user configuration.

- [x] **Step 4: Rebuild and verify GREEN**

Recompile and run the priority smoke.

Expected: exit 0, logged command ordering is correct, and the first valid candidate is published in less than five seconds.

---

### Task 3: Full verification, build, ledger, commit, and push

**Files:**
- Modify: `MEMO.md`
- Verify: all files changed by Tasks 1 and 2 plus the design and plan documents.

**Interfaces:**
- Consumes: the two green task-level regressions.
- Produces: a signed local `.build/QiDao.app` and an ordinary fast-forward `origin/main` update.

- [x] **Step 1: Run focused Swift verification**

Run the trust-boundary executable, live-board smoke, live-AI-priority smoke, and a full Swift compile including `QiDaoApp.swift`. Run the existing real KataGo smoke only if the environment can create a Metal device; otherwise report the precise environmental failure without claiming it passed.

- [x] **Step 2: Run full regressions and audits**

```bash
PYTHONPATH=vision /Users/horseli/code/.venv/bin/python -B -m unittest discover -s vision/tests -p 'test_*.py' -v
CARGO_TARGET_DIR=/private/tmp/qidao-inactive-v2-rust cargo test --locked --manifest-path qidao-core/Cargo.toml
bash scripts/verify_repository.sh
bash scripts/verify_ci_policy.sh
bash tests/scripts/test_release_audits.sh
git diff --check
```

Expected: 94 vision tests, 41 Rust tests, all audits, and the diff check pass.

- [x] **Step 3: Build and inspect the app**

Run `./build_app.command`. Confirm `.build/QiDao.app/Contents/MacOS/QiDao` is newer than the modified sources and inspect its demangled symbols for the final refresh method. Do not copy the app into `/Applications` or activate it unless separately requested.

- [x] **Step 4: Update the project ledger and review the exact diff**

Add one concise `MEMO.md` progress item describing the two root causes and the timing regressions. Confirm the staged set contains only production source, smoke tests, docs, and `MEMO.md`; exclude ignored models, signing files, generated Core files, build output, and the local audit branch.

- [x] **Step 5: Commit and push**

```bash
git add QiDao/QiDao/BoardViewModel.swift \
  QiDao/QiDao/BoardViewModel+ScreenAssist.swift \
  QiDao/QiDao/AIManager.swift \
  QiDao/QiDao/ScreenAssistManager.swift \
  tools/smoke_live_board_refresh.swift \
  tools/smoke_live_ai_priority.swift \
  docs/superpowers/specs/2026-08-12-inactive-live-refresh-design.md \
  docs/superpowers/plans/2026-08-12-inactive-live-refresh.md \
  docs/superpowers/specs/2026-08-13-inactive-live-refresh-v2-design.md \
  docs/superpowers/plans/2026-08-13-inactive-live-refresh-v2.md \
  MEMO.md
git commit -m "fix: keep live board and analysis responsive"
git push origin main
```

Verify `git ls-remote origin refs/heads/main` equals `git rev-parse HEAD`. Never force push and never push `local/audit-history-do-not-push`.
