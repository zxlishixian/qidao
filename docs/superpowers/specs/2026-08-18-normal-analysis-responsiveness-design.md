# Normal Analysis Responsiveness Design

## Context

QiDao's live screen analysis now returns and presents the first KataGo result reliably while the app is inactive, but ordinary Analysis mode can still appear stalled until the AI settings sheet is opened. Opening the settings sheet does not submit a KataGo query; it only causes another SwiftUI/AppKit presentation transaction. This proves that at least one failure mode is a result already present in the model but not presented by the ordinary analysis window.

Ordinary analysis also follows a slower engine path than live analysis. It waits through a 500 ms debounce, leaves a lower-priority full-game scan competing for the same KataGo process, uses lower query priority, and does not request a prompt partial result. Live analysis explicitly terminates the residual full-game scan, uses high priority, and forces one asynchronous window invalidation for its first result.

## Goal

Make ordinary Analysis mode show the current position's first valid KataGo result without a settings click, including while QiDao is inactive, while preserving the background win-rate graph after that first result appears.

## Chosen Approach

Use one interactive-analysis policy for both ordinary and live analysis:

1. The current board position always preempts background full-game analysis.
2. Rapid tree navigation is coalesced with a short debounce, not the existing 500 ms delay.
3. Every interactive query uses high KataGo priority and requests a prompt partial report.
4. The first valid result for the current query triggers one asynchronous AppKit invalidation in every workspace, not only live analysis.
5. Ordinary Analysis mode resumes the low-priority full-game scan after the first current-position result is visible in the model. Live analysis does not resume it.

This avoids both rejected alternatives: a UI-only patch would leave engine contention intact, while a timer that continuously redraws windows would hide the race at unnecessary CPU cost.

## Data Flow

### Query submission

`BoardViewModel.updateAnalysis()` determines whether an interactive request can be submitted. Before submitting a ready request it marks the first result as pending and stops the Swift-side full-game scan. `AIManager.updateAnalysis(...)` then debounces briefly, explicitly terminates the KataGo `fullscan-<session>` query and the previous interactive query, and submits the current `qidao-<session>-<node>` query at interactive priority.

The ordinary query continues to include ownership and policy according to user settings. Its `reportDuringSearchEvery` value is capped to a responsive upper bound for the interactive query only; the configured full-game scan budget remains unchanged.

### Result publication

`AIManager` continues to validate the session and node ID before publishing an `AnalysisResult`. The existing throttled `BoardViewModel` subscription accepts only the current node's first non-empty result as the presentation boundary. It clears the pending flag, publishes the result snapshot, and schedules one nonblocking `needsLayout`/`needsDisplay` invalidation through the existing refresh method.

No code calls `displayIfNeeded`, activates QiDao, makes a window key, or waits for WindowServer presentation.

### Background scan recovery

After the first current-position result, ordinary Analysis mode restarts `startFullGameAnalysis()` when the win-rate graph is enabled. The full scan keeps its existing negative priority and visit budget. Live screen analysis continues to suppress the full scan because new detected moves must remain latency-sensitive.

If the interactive query is replaced by a newer node before a result arrives, cancellation and ID validation discard the stale result; only the newest query may clear the pending flag or restart the full scan.

## Scope

Production changes are limited to at most three files:

- `QiDao/QiDao/AIManager.swift`: shared interactive query scheduling, full-scan preemption, debounce, priority, and report cadence.
- `QiDao/QiDao/BoardViewModel.swift`: generic first-result pending state, one-shot presentation invalidation, and delayed full-scan restart.
- `QiDao/QiDao/BoardViewModel+ScreenAssist.swift`: remove the live-only ownership of the first-result flag while retaining live timing telemetry.

No Rust, Python vision, generated bindings, model files, engine configuration, Xcode settings, UI layout, or additional timer is changed.

## Error Handling

- Engine startup and query errors continue through the existing `AIStatus` and engine log path.
- A failed or cancelled current-position query does not restart background scanning ahead of a newer current-position request.
- Session and node ID checks remain authoritative, preventing delayed results from an older game or variation from being displayed.
- Window refresh remains asynchronous; failure to paint cannot block engine progress or query acknowledgement.

## Verification

Test-driven verification will add or extend smoke coverage for all of these behaviors:

1. With a full-game query active, an ordinary `fastResponse: false` request terminates that query before submitting the interactive query.
2. The ordinary interactive query has high priority and a prompt partial-report interval.
3. The first ordinary result invalidates a non-key QiDao window without a settings click and without a direct synchronous display call.
4. Only the current node's result clears the pending state; stale results are ignored.
5. The background full-game scan resumes after the first ordinary result but remains suppressed during live analysis.
6. Existing live-board correction/capture, inactive-refresh, AI-priority, Python vision, Rust core, repository, release, and signature checks remain green.

Success means the current board's first recommendation is presented without user interaction, normally within five seconds on the configured local KataGo engine, and neither ordinary nor live analysis regresses into synchronous rendering.
