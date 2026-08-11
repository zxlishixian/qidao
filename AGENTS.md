# AGENTS.md

This AGENTS.md provides context and instructions for AI agents (e.g., Copilot, Aider, Codex, Claude Code, etc.) working on this project. It supplements the README.md by offering project-specific setup, guidelines, and technical details to enable efficient code generation, debugging, and contributions without cluttering human-facing docs.

## Project Overview
This repository is the non-official [`zxlishixian/qidao`](https://github.com/zxlishixian/qidao) fork of the upstream [`neolee/qidao`](https://github.com/neolee/qidao) project. QiDao (Tao of Go 棋道) is an Apple Silicon/macOS Go (Weiqi) tool with Analysis, Edit, Play, and Live Screen Analysis workspaces. Key features include SGF handling, local KataGo Analysis/GTP integration, graphical variation trees, screen-board recognition, and high-performance rendering. The project uses a hybrid Swift-Rust architecture plus a local Python/OpenCV vision service.

## Directory Structure
- `QiDao/`: Xcode project for the SwiftUI application.
- `qidao-core/`: Rust-based core logic.
  - `src/lib.rs`: Main entry point for UniFFI exports.
  - `src/bin/uniffi-bindgen.rs`: CLI tool for generating bindings.
  - `out/`: Generated Swift/C bindings.
- `resources`: Miscellaneous resources (samples and local reference material).
- `tools`: Retained portable smoke tests and diagnostics.
- `INIT.md`: Historical project vision; current requirements are documented in `README.md`.
- `MEMO.md`: Detailed information for coding agents, including specification, technical architecture, project progression, and other useful notes.
- `TODO.md`: specification for highly complex tasks, written by human.
- `AGENTS.md`: this document, for coding agents use.

## Code Style Guidelines
- **General**:
  - Naming and Style: follow programming language's best practice and most idiomatic usage.
  - Consistent indentation: 4 spaces (no tabs).
  - No trailing whitespace, no unnecessary blank line.
  - Comments: Doc comments for public APIs; inline for complex logic.
- **Swift**:
  - Swift language version 5+.
  - Follow SwiftUI best practices: Use `@State`, `@ObservedObject`, and `@FocusState` for reactive UI.
  - Prefer declarative views; avoid imperative loops in rendering.
  - Prefer SwiftUI native solutions over AppKit/Foundation (e.g., `@AppStorage` over `UserDefaults`, `fileImporter` over `NSOpenPanel`).
  - **Ranking Logic**: Always use `AnalysisResult.sortedMoves(isWhiteTurn:)` for displaying AI candidate moves to ensure UI consistency.
  - Concurrency: Use `nonisolated` actors minimally; prefer `MainActor` for UI updates.
  - Localization: Use customized language manager (`Localization.swift`).
  - Performance: Optimize for 60/120fps rendering; use `Canvas` over heavy View hierarchies.
- **Rust**:
  - Use Rust 1.70+ idioms: Prefer `Arc<Mutex<>>` for shared state, async with Tokio for engine I/O.
  - Error handling: Use `Result` with custom errors; avoid panics in library code.
  - UniFFI exports: Keep interfaces simple; use structs/enums for data transfer; support proc-macro (`[uniffi::export]` on exporting function and structure interfaces).
- **Error Handling**:
  - **Rust**: Use `thiserror` and `#[derive(uniffi::Error)]` for all fallible APIs.
  - **Swift**: Managers propagate errors using `throws`; `BoardViewModel` catches them to update UI state.
  - **Display States**:
    1. **AI Engine Errors**: Displayed in `AIEngineLogView` (readable logs).
    2. **Non-AI Core Errors**: Displayed concisely in the engine message area via `model.engineMessage`.
    3. **GUI Errors**: Logged to the app console.
  - **L10n**: Always use `.localized` for user-facing error strings.

## Developing Environment
- **Fork Repository**: `https://github.com/zxlishixian/qidao`
- **Upstream Repository**: `https://github.com/neolee/qidao`
- **Toolchain**:
  - Rust: project-pinned Rust 1.97.1 via `rustup`; `cargo`; `uniffi-bindgen`.
  - Swift: Xcode 16+ on macOS (Apple Silicon recommended).
- **Editor**: VS Code for Swift and Rust code generating and editing, Xcode for SwiftUI app building and debugging, `cargo` for Rust syntax checking, building and testing.
- **Workflow**:
  - **Swift-Rust Bridge**：Run `./build_core.sh` to build Rust `qidao-core`, generate Swift bindings and copy necessary file to the SwiftUI project.
  - **Build macOS App**: Open `QiDao.xcodeproj` in Xcode and run `Product > Build` (or `xcodebuild build`)
  - **Run App**: In Xcode, select QiDao scheme and run (⌘R); ensure KataGo engine path is configured for AI features
  - **Unit Tests**:
    - Rust: `cargo test --locked` in `qidao-core/` for core logic tests.
    - Swift: *TBD*
    - **Integration Tests**: Manually run app and check features.
    - **CI**: `.github/workflows/ci.yml` validates public source only; it does not use signing material or publish releases.
  - **Test Rust Core**: use Cargo unit and integration tests; diagnostics that need a locally installed engine live under `tools/`.
  - **Performance Profiling**: Use Instruments.app for CPU/GPU spikes; focus on engine buffer draining and lock contention.
- **Settings**
  - **Xcode Settings** (for QiDao target):
    - Architectures: arm64 only (Apple Silicon).
    - Swift Language Version: 5.
    - App Sandbox: Disabled.
    - Strict Concurrency Checking: Minimal.
    - Default Actor Isolation: nonisolated.
    - Link Rust lib: Add `libqidao_core.a` to "Link Binary With Libraries".
  - **AI Engine**: Download a KataGo executable and model, then set their paths in app preferences. To run the retained Python smoke diagnostic, pass `--katago` and `--model` to `tools/smoke_katago.py` (or set `KATAGO_EXECUTABLE` and `KATAGO_MODEL`). To compile and run the Swift startup smoke without the normal app entry point:
    ```bash
    ./build_core.sh
    mkdir -p .build/smoke-ai-module-cache
    sdk_path="$(xcrun --sdk macosx --show-sdk-path)"
    find QiDao/QiDao -name '*.swift' ! -name 'QiDaoApp.swift' -print0 | xargs -0 swiftc \
        -parse-as-library -sdk "$sdk_path" -target arm64-apple-macosx14.0 \
        -module-cache-path .build/smoke-ai-module-cache \
        -I QiDao/QiDao/Core/qidao_coreFFI \
        -Xcc -fmodule-map-file=QiDao/QiDao/Core/qidao_coreFFI/module.modulemap \
        QiDao/QiDao/Core/libqidao_core.a \
        -framework SwiftUI -framework AppKit -framework Foundation \
        tools/smoke_ai_live_start.swift -o .build/smoke-ai-live-start
    .build/smoke-ai-live-start \
        --katago "$(command -v katago)" \
        --config "$PWD/katago/analysis.cfg" \
        --model /absolute/path/to/model.bin.gz
    ```
    The Swift tool also accepts `KATAGO_EXECUTABLE`, `KATAGO_CONFIG`, and `KATAGO_MODEL` instead of the three CLI options.

## Architecture Overview
- **Layers**:
  - UI: SwiftUI views (e.g., `CenterView`, `VariationTreeView`).
  - State: `BoardViewModel` manages game state, engine life-cycle.
  - Core: Rust `qidao-core` handles SGF tree, rules, GTP/Analysis APIs via UniFFI.
- **Key Patterns**:
  - MVVM for Swift.
  - Async engine communication with Tokio in Rust.
  - High-performance rendering: SwiftUI `Canvas` for trees/boards.

## Boundaries
- **Always do**:
  - Keep code clean and modular, easy to understand and maintainable.
  - Update project progression in `MEMO.md` after user confirmation of major task completion.
  - Run `./build_core.sh` after any change in `qidao-core` source code.
  - L10n: Add to Localizable.strings before using new UI strings.
- **Ask before doing**:
  - Before modifying existing source code in a major way (i.e. more than 3 source files).
  - Change Xcode project settings.
- **Never do**:
  - Commit more than one major task in a single iteration.
  - Modify UniFFI generated bindings directly (files under `QiDao/QiDao/Core`), use `./build_core.sh` instead.
  - Use `cat` to create or edit files.

## Current Work
The current release work is public-source hardening for this fork. Use `README.md` for supported platforms and user-facing status, and `MEMO.md` for the detailed development ledger.

This document is a living reference—update as the project evolves. For contributions, prioritize TODOs and maintain macOS-native performance.
