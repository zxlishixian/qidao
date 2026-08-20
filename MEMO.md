# QiDao Project Status & Guidelines (AGENTS.md)

This document serves as a source of truth for AI agents working on the QiDao project. It tracks confirmed requirements, technical decisions, architecture, and progress.

## 1. Project Overview
QiDao (Tao of Go) is a modern Go (Weiqi) board editor and AI analysis tool, primarily for macOS, inspired by Lizzieyzy.

## 2. Technical Stack
- **UI Layer**: SwiftUI (macOS Native)
- **Core Logic Layer**: Rust (`qidao-core`)
  - **Scope**: SGF parsing & tree management, GTP/Analysis API orchestration, Go rules engine (validation, capture logic).
- **Interoperability**: **UniFFI** (Swift-Rust bridging).
  - **Status**: Initialized with proc-macro support and Swift binding generation.

## 3. Directory Structure
- `QiDao/`: SwiftUI application source code.
- `qidao-core/`: Rust-based core logic.
  - `src/lib.rs`: Main entry point for UniFFI exports.
  - `src/bin/uniffi-bindgen.rs`: CLI tool for generating bindings.
  - `out/`: Generated Swift/C bindings.
- `screens/`: UI reference images.
- `init-spec.md`: Detailed functional and non-functional requirements.

## 4. Key Confirmed Requirements
- **SGF Handling**: Full support for SGF tree, editing, and saving.
- **AI Integration**:
  - Support for GTP and KataGo Analysis API.
  - Real-time analysis, win-rate graphs, and blunder detection.
- **UI/UX**:
  - Modern macOS native look and feel.
  - Graphical Variation Tree visualization.
  - GPU-accelerated board rendering (60/120fps).
  - **Localization**: Architecture must support i18n; Chinese-only for the initial phase.
- **Performance**: Multi-threaded engine communication, low latency.

## 5. Architecture Design
1. **Core Logic (Rust)**: SGF Tree, Rules Engine, Engine Communication.
2. **Application State (Swift)**: ViewModel layer, managing engine life-cycle and UI state.
3. **UI (SwiftUI)**: View layer, high-performance board rendering.

## 6. Project Progress
- [x] Initial requirements defined ([init-spec.md](init-spec.md)).
- [x] Requirements updated with AI Analysis API, Variation Tree, and GPU acceleration.
- [x] Project structure initialized with `QiDao/` and `qidao-core/`.
- [x] **Swift-Rust Bridge**: Confirmed UniFFI as the bridging solution.
- [x] **Core Setup**: Initialized Rust library in `qidao-core` with UniFFI support.
- [x] **GUI Prototype**: Created basic `CenterView` and `BoardViewModel` in SwiftUI.
- [x] **Core Integration**: Built Rust static library and generated Swift bindings; integrated into Xcode with a modular `qidao_coreFFI` structure.
- [x] **SGF Parsing**: Implemented basic SGF parsing in Rust using `sgf-parse` 4.2 and exported via UniFFI.
- [x] **GUI Framework**: Built interactive `CenterView` and `BoardViewModel` in SwiftUI, supporting stone placement, real-time board updates, and a three-column layout with theme support.
- [x] **SGF Navigation & Persistence**: Implemented `Game` controller in Rust for tree navigation and branch management. Added SGF loading and saving functionality with macOS file picker integration.

## 7. Technical Notes & Best Practices
### Xcode Build Settings for UniFFI
To avoid concurrency warnings and architecture mismatches (e.g., "symbol(s) not found for architecture x86_64"), use the following settings in the Xcode target:
- **Architectures**: `arm64` (Restricted to Apple Silicon to match Rust core build)
- **Default Actor Isolation**: `nonisolated`
- **Strict Concurrency Checking**: `Minimal` (or `Targeted`)
- **Swift Language Version**: `5`

### Variation Tree Implementation
- **Rendering**: Uses SwiftUI `Canvas` (GraphicsContext) for high-performance drawing. This avoids the overhead of thousands of individual `View` objects in large SGF trees.
- **Auto-Positioning**: Implemented `centerCurrentNode` logic. It calculates the coordinate of the active node and updates the `offset` of the tree container. Triggered via `onChange(of: viewModel.currentNodeId)`.
- **Navigation**: Supports global "Jump to Move". The Rust core performs a DFS search across all branches to find the first occurrence of a specific move number, allowing navigation outside the current branch.

### Focus & Keyboard Shortcuts
- **Global Shortcuts**: Keyboard listeners (`.onKeyPress`) are attached to the root `HSplitView` to ensure they capture events regardless of which sub-view is active.
- **Delete Command**: Use `.onDeleteCommand` instead of raw `.onKeyPress` for the Delete/Backspace key. This is the standard macOS way to handle deletion and avoids system beeps or conflicts with text input.
- **Focus Restoration**: SwiftUI focus can be lost when a focused element (like an inline `TextField`) is removed from the hierarchy. We use `@FocusState` combined with `DispatchQueue.main.asyncAfter(deadline: .now() + 0.05)` to explicitly restore focus to the main container after operations like "Jump to Move" or closing dialogs.

### AI Engine Performance & Stability
- **Concurrency Model**: Rust core uses independent `Arc<Mutex<Option<...>>>` for `stdin`, `stdout`, and `stderr`. This prevents `get_next_result` (reading) from blocking `analyze` (writing), eliminating deadlocks during high-frequency navigation.
- **Buffer Management**: The `analyze` method in Rust automatically drains the `stdout` buffer before sending a new query. This prevents "Backlog Spin" where the GUI wastes CPU parsing thousands of obsolete JSON results from previous board states.
- **Lifecycle Control**: The GUI explicitly sends `terminate_all` when reaching `maxVisits` or switching moves. This forces KataGo to drop pending GPU batches and clear its internal queue immediately.
- **Logging Optimization**: Communication logs (`>>>`/`<<<`) are truncated to 500 chars in the core and completely skipped (including string formatting/serialization) when `logging_enabled` is false.

### SwiftUI State Management & UI Updates
- **Avoiding "Publishing changes from within view updates"**: When binding a `@Published` property from a ViewModel to a SwiftUI control (like `Picker` or `Toggle`), it may trigger a state change during the view's rendering cycle, leading to runtime warnings or undefined behavior.
  - **Solution**: Use a custom `Binding` to wrap the property. In the `set` block, perform the update inside `DispatchQueue.main.async`.
  - **Example**:
    ```swift
    private var modeBinding: Binding<AppMode> {
        Binding(
            get: { viewModel.appMode },
            set: { newValue in
                DispatchQueue.main.async {
                    viewModel.appMode = newValue
                }
            }
        )
    }
    ```

### Modernizing AppKit/Foundation Usage (Jan 2026)
- **UserDefaults to @AppStorage**: Prefer `@AppStorage` in ViewModels and Views for reactive and automatic persistence. This eliminates manual `didSet` and `init` loading logic.
- **NSError to Swift Error**: Use custom `enum` conforming to `LocalizedError` instead of `NSError`. This provides better type safety and idiomatic Swift error handling.
- **NSCursor Management**: Use `NSCursor.push()` and `NSCursor.pop()` instead of `set()` for more robust cursor state management, especially in hover scenarios.
- **AppKit Colors**: Use `Color(NSColor.windowBackgroundColor)` for system-specific semantic colors that SwiftUI doesn't natively expose yet.

### AI Status & Logging Design (Dec 30, 2025)
- **Status-Driven UI**: Replace `AIState` with a more expressive `AIStatus` enum that drives the icon, color, and localized message in `AIEngineView`.
    - States: `idle`, `starting`, `ready`, `analyzing` (Analysis mode), `thinking` (Play mode), `error`.
- **Event-Based Logging**: `AIManager` generates human-readable "Event Logs" instead of raw stdout/stderr.
    - Examples: "Engine started successfully", "Analyzing move 42", "AI found move D4 (1.2k visits)".
- **Developer Mode**: Raw JSON communication (`>>>`/`<<<`) is hidden by default and only accessible via a "Developer Mode" toggle in settings (output to Xcode console or a hidden view).
- **Separation of Concerns**:
    - `AIEngineView`: Instantaneous status ("What is AI doing now?").
    - `AIEngineLogView`: Historical context ("What just happened?").

## 8. Immediate TODOs

1. **Performance Optimization**: Profile the board rendering and engine communication for potential bottlenecks.
2. **UI Polish**: Refine the visual feedback for stone placement and AI suggestions.
3. **User Documentation**: Create a basic user guide for the new Play and Edit modes.

## 9. Implementation Plan (Dec 30, 2025)

### Phase 13: Multi-Mode Support (Completed)
- **AppMode State**: Defined `AppMode` enum and implemented a `Segmented Picker` in the toolbar.
- **Dynamic Sidebars**: Implemented context-aware sidebars for Analysis, Edit, and Play modes.
- **Edit Mode Logic**: Added tools for stone placement and basic marks (TR, CR, SQ, MA, LB).
- **Play Mode Logic**: Implemented Human-vs-AI flow with game controls (Pass, Resign, Undo).

### Phase 14: AI Status & Logging Implementation (Completed)
- **AIStatus Enum**: Implemented a state machine for AI lifecycle (idle, starting, ready, analyzing, thinking, error).
- **Event-Based Logging**: Refactored logging to use human-readable events with color coding for different tasks (Play, Analysis, Full Scan).
- **Modular AIManager**: Split `AIManager` into functional extensions for better maintainability.
- **Full Game Analysis**: Added progress tracking and configurable visit limits for background analysis.

### Phase 15: Advanced Edit & Play Features (Completed)
- **SGF Export**: Implemented SGF string generation and file export with support for marks and comments.
- **Game Setup**: Added "New Game" dialog with board size, handicap, and komi settings.
- **Clock System**: Implemented a robust timing system for Play Mode.
    - **UI**: Redesigned human clock to `30 + 5:00` format (Seconds per move + Reserve time).
    - **Logic**: Automatic reset on turn change, immediate stop on human move, and skip on first move.
- **AI Robustness**:
    - **Unique PlayID**: Implemented a `playCounter` to ensure every AI request has a unique ID, preventing "ghost moves" from outdated tasks.
    - **Smart Undo**: Context-aware undo that backtracks 1 or 2 steps to maintain human-to-move state.
- **UI Stability**: Fixed layout glitches (height jumps) by using fixed-height frames for status indicators.

### Phase 16: Analysis Logic Refinement & Code Quality (Completed)
- **Win Rate Ranking**: Refactored AI candidate move ranking to prioritize **Win Rate** over **Visits**. This ensures the interface (both board markers and sidebar table) more intuitively reflects the current best move during analysis.
- **Logic Unification**: Moved sorting for `AnalysisMoveInfo` into a dedicated Swift extension (`AnalysisResult+Extensions.swift`). Guaranteed consistent ranking across `GameBoardView`, `MoveEvaluationView`, and `RightSidebarView`.
- **Tie-breaker & Stability**: Implemented a two-tier sorting system (Win Rate as primary, Visits as tie-breaker) with a `0.001` epsilon threshold, aligning with the UI's display precision and preventing ranking flicker when win rates are nearly identical.
- **Project Branding**: Corrected project's Chinese name to **“棋道” (Tao of Go)** throughout internal documentation and communications.

### Phase 17: QiDao Screen Assist (Completed, Aug 2026)
- **Single-board workflow**: Removed the former online dual-board mirroring concept. The application reads only the user-selected actual board and never clicks another application or website.
- **Vision service boundary**: Added a JSON Lines Python sidecar under `vision/` for ScreenCaptureKit capture, full-grid fitting, perspective normalization, black/white classification, stable-frame confirmation, and small-translation tracking.
- **Authoritative reconciliation**: Added `BoardViewModel+ScreenAssist.swift` so a confirmed legal move is appended to QiDao's game tree; corrected or discontinuous positions are rebuilt as setup stones before a new KataGo query.
- **Coordinate safety**: A move is committed only after an independent full-grid re-anchor returns the same intersection. The UI shows both the latest recognized move and KataGo's best move on the actual screen board.
- **Native UI**: Added a localized Screen Assist sheet with QQ-style rectangle selection, board-size/orientation selection, confidence and tracking telemetry, pass/undo, and start/pause controls.
- **Observable recognition loop**: Each scan now publishes both the raw observed 9/13/19 board and the stable confirmed board. An always-on-top replica board renders pending observations, unknown intersections, visual conflicts, the latest accepted move, and recognition telemetry so failures are visible instead of silent.
- **AI dashboard**: Screen Assist starts the configured KataGo engine automatically and feeds the reconciled position into qidao analysis. The floating dashboard shows ranked candidates, black/white win rates, score lead, visit count, and a manual reanalysis action.
- **Screen Recording identity fix**: Permission preflight/request now runs in the signed QiDao process instead of trusting the Python/helper child. A project-only trusted certificate signs both the app and bundled capture helper, producing a certificate-anchored designated requirement that survives rebuilds. The app always selects the bundled service first, and service readiness now requires a real ScreenCaptureKit pixel probe rather than a settings flag.
- **Local engine configuration**: The source tree includes an Analysis Engine config and retains optional real-engine smoke diagnostics. KataGo executables and network weights are user-provided and are not bundled.
- **Build fallback**: Added `build_app.command`, which builds and signs `QiDao.app` with Apple Command Line Tools when full Xcode is unavailable.

## 10. Progress Log
- [x] **Phase 1: Board Logic & Rules**: Implemented `Board` struct in Rust with capture logic, suicide prevention, and simple Ko rule. Exported to Swift via UniFFI.
- [x] **Phase 2: UI/UX Foundation**: Refined 3D stone visuals, sound effects system, and multi-language support. Fixed sandbox-related permission issues.
- [x] **Phase 3: Variation Tree & Navigation**: Implemented graphical variation tree using `Canvas`, keyboard-based branch switching, and optimized sound feedback. Added global "Jump to Move" with inline UI and focus management.
- [x] **Phase 4: AI Engine Integration (Core)**: Implemented `GtpEngine` and `AnalysisEngine` in Rust. Added support for KataGo Analysis API with JSON-based queries. Verified with standalone test tools.
- [x] **Phase 5: AI UI Integration**: Integrated `AnalysisEngine` into `BoardViewModel`. Added real-time win rate analysis, score lead display, and AI suggested moves overlay on the board. Implemented engine lifecycle management and localization.
- [x] **Bug Fix: AI Engine Stability**: Resolved KataGo startup issues (missing model path, log directory permissions) and coordinate format errors (SGF vs GTP). Fixed SwiftUI `ProgressView` layout crashes by using custom drawing.
- [x] **Bug Fix: Tokio Runtime Integration**: Resolved "no reactor running" and "future not Send" errors by implementing a global Tokio runtime and using `spawn` to ensure async operations run in the correct context.
- [x] **Phase 6: AI UI Refinement & Visualization**: Refined AI move markers with transparency and rank styling. Implemented dynamic Win Rate Graph with history persistence. Added PV preview on hover and stabilized sidebar layouts to prevent flickering. Optimized variation marker visibility.
- [x] **Phase 7: Core Optimization & Evaluation Board**: Refactored Rust engine locks for zero-latency navigation. Implemented "Evaluation Board" (mini-board) with grayscale ownership map and PV sequence. Added centralized logging control and buffer draining to prevent CPU spikes.
- [x] **Phase 8: Branch Management & UX Refinement**: Implemented "Delete Current Branch" with confirmation dialog. Optimized file dialogs to be non-blocking and path-aware. Synchronized and cleaned up localization files. Refined keyboard focus and shortcut handling using `.onDeleteCommand`.
- [x] **Phase 9: Legacy release automation cleanup**: Removed the former secret-reading publishing workflow and updater metadata. Current CI validates public source only and does not publish releases.
- [x] **Phase 10: 13x13 and 9x9 Board Support**.
- [x] **Phase 11: Refactoring and Enhancements**: Improved code organization in views and view model. Win rate graph and ownership map toggles, incremental full game analysis, etc.
- [x] **Phase 12: View Improvement**: Separated logs and comments into a tabbed view; implemented `GameCommentView` for SGF comment editing.
- [x] **Phase 13: Multi-Mode Support**: Implemented `AppMode` with dynamic sidebars for Analysis, Edit, and Play modes. Added stone placement and mark tools in Edit mode. Implemented basic Human-vs-AI gameplay flow in Play mode with game controls.
- [x] **Phase 14: AI Status & Logging Implementation**: Implemented `AIStatus` state machine and event-based logging system. Modularized `AIManager` into functional extensions. Added color-coded logs for different AI tasks (Play, Analysis, Full Scan). Implemented background analysis progress tracking and configurable visit limits.
- [x] **Phase 15: Advanced Edit & Play Features**: Implemented SGF export, "New Game" setup, and a robust clock system. Refined Play mode logic with unique PlayIDs and smart undo to prevent ghost moves and state desync. Fixed UI layout glitches during AI transitions.
- [x] **Phase 16: Analysis Logic Refinement**: Unified AI move sorting logic to prioritize win rate over visits. Implemented `AnalysisResult+Extensions` for consistent ranking across all UI components. Fixed project name references to "棋道".
- [x] **Phase 17: QiDao Screen Assist**: Integrated stable real-time screen-board recognition, a live replica/diagnostics window, QiDao position reconciliation, local KataGo win-rate and move analysis, and actual-board overlays. Removed all click automation and web synchronization paths.
- [x] **Inactive live refresh hardening (Aug 13, 2026)**: A real inactive-process sample traced the remaining stall to synchronous `displayIfNeeded`/RenderBox waits, delayed ACKs, 400 ms position replay, and unchanged scan heartbeats publishing the whole UI. Position ACK now follows the authoritative model commit immediately, window refresh is asynchronous invalidation only, unchanged heartbeats are UI-silent, and stdout delivery explicitly wakes the main run loop without activating QiDao. Regressions cover immediate ACK, zero direct synchronous display calls, 30 quiet heartbeats, correction/capture replay, ordered full-scan termination, and a first fake-engine candidate within five seconds.
- [x] **Ordinary analysis responsiveness (Aug 18, 2026)**: Ordinary Analysis now terminates residual full-game scans before every interactive query, uses priority 30 with a 120 ms debounce and at most 250 ms partial-result cadence, and restarts the background scan only after the current node's first result. `BoardViewModel` uses one generic first-result gate for live and ordinary analysis and requests asynchronous window invalidation even while QiDao is inactive; it never activates the app or calls synchronous `displayIfNeeded`. `smoke_live_ai_priority.swift` and `smoke_live_board_refresh.swift` cover protocol preemption, a result within five seconds, and the former settings-click presentation stall.
