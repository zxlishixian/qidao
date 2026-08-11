//
//  ContentView.swift
//  QiDao
//
//  Created by Neo on 2025/12/24.
//

import SwiftUI

struct ContentView: View {
    /// UI-only workspace routing. Live analysis deliberately maps to the
    /// existing `.analysis` engine mode so this fourth top-level destination
    /// does not fork any board, vision or KataGo behavior.
    private enum WorkspaceMode: String, CaseIterable, Identifiable {
        case analysis
        case liveAnalysis
        case edit
        case play

        var id: String { rawValue }

        var label: String {
            switch self {
            case .analysis: return "Analysis Mode".localized
            case .liveAnalysis: return "Live Game Analysis".localized
            case .edit: return "Edit Mode".localized
            case .play: return "Play Mode".localized
            }
        }

        var appMode: AppMode {
            switch self {
            case .analysis, .liveAnalysis: return .analysis
            case .edit: return .edit
            case .play: return .play
            }
        }
    }

    @StateObject private var viewModel = BoardViewModel()
    @State private var showInfoEditor = false
    @State private var showAIConfig = false
    @State private var workspaceMode: WorkspaceMode = .analysis
    @FocusState private var isBoardFocused: Bool
    @ObservedObject private var langManager = LanguageManager.shared

    private var modeBinding: Binding<WorkspaceMode> {
        Binding(
            get: { workspaceMode },
            set: { newValue in
                DispatchQueue.main.async {
                    workspaceMode = newValue
                    viewModel.appMode = newValue.appMode
                }
            }
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            // Top Mode Switcher
            HStack {
                Spacer()

                Picker("Mode".localized, selection: modeBinding) {
                    ForEach(WorkspaceMode.allCases) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .frame(width: 500)
                .padding(.vertical, 4)
                .id(langManager.selectedLanguage)

                Spacer()

                Menu {
                    ForEach(Language.allCases) { lang in
                        Button(lang.displayName) {
                            DispatchQueue.main.async {
                                langManager.selectedLanguage = lang
                            }
                        }
                    }
                } label: {
                    ViewThatFits(in: .horizontal) {
                        Label(langManager.selectedLanguage.displayName, systemImage: "globe")
                        Image(systemName: "globe")
                    }
                }
                .buttonStyle(.plain)
                .focusable(false)
                .padding(.trailing, 10)
            }
            .background(Color(NSColor.windowBackgroundColor))

            Divider()

            HSplitView {
                LeftSidebarView(
                    viewModel: viewModel,
                    showInfoEditor: $showInfoEditor,
                    showAIConfig: $showAIConfig,
                    showsLiveAnalysis: workspaceMode == .liveAnalysis
                )
                    .frame(minWidth: 220, idealWidth: 280, maxWidth: 350)

                CenterView(viewModel: viewModel, isBoardFocused: $isBoardFocused)
                    .frame(minWidth: 420)

                RightSidebarView(viewModel: viewModel)
                    .frame(minWidth: 220, idealWidth: 280, maxWidth: 350)
            }
        }
        .frame(minWidth: 900, minHeight: 600)
        .sheet(isPresented: $showInfoEditor) {
            GameInfoEditorView(viewModel: viewModel)
        }
        .sheet(isPresented: $showAIConfig) {
            AIConfigView(viewModel: viewModel)
        }
        .onAppear {
            isBoardFocused = true
        }
        .onOpenURL { url in
            viewModel.loadSgf(url: url)
        }
    }
}

#if canImport(PreviewsMacros)
    #Preview {
        ContentView()
    }
#endif
