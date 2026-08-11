import SwiftUI
import qidao_coreFFI

struct LeftSidebarView: View {
    @ObservedObject var viewModel: BoardViewModel
    @Binding var showInfoEditor: Bool
    @Binding var showAIConfig: Bool
    let showsLiveAnalysis: Bool
    @ObservedObject private var langManager = LanguageManager.shared
    @State private var selectedTab = 0

    var body: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 15) {
                GameInfoView(viewModel: viewModel, showInfoEditor: $showInfoEditor)

                if showsLiveAnalysis {
                    // A dedicated top-level workspace, while intentionally
                    // reusing Analysis mode's board and AI data sources.
                    ScreenAssistView(viewModel: viewModel)

                    WinRateView(viewModel: viewModel)

                    AIEngineView(viewModel: viewModel, showAIConfig: $showAIConfig)

                    analysisNotesAndLogs
                } else {
                    switch viewModel.appMode {
                    case .analysis:
                        WinRateView(viewModel: viewModel)

                        AIEngineView(viewModel: viewModel, showAIConfig: $showAIConfig)

                        analysisNotesAndLogs

                    case .edit:
                        EditToolboxView(viewModel: viewModel)

                        GameCommentView(viewModel: viewModel)
                            .frame(minHeight: 150)

                    case .play:
                        AIEngineView(viewModel: viewModel, showAIConfig: $showAIConfig)

                        PlayControlView(viewModel: viewModel)

                        AIEngineLogView(viewModel: viewModel)
                            .frame(minHeight: 150)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .padding()
        }
        .frame(minWidth: 220, maxWidth: 350)
    }

    private var analysisNotesAndLogs: some View {
        VStack(spacing: 10) {
            Picker("", selection: $selectedTab) {
                Text("Comments".localized).tag(0)
                Text("AI Engine Logs".localized).tag(1)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .id(langManager.selectedLanguage)

            if selectedTab == 0 {
                GameCommentView(viewModel: viewModel)
            } else {
                AIEngineLogView(viewModel: viewModel)
            }
        }
        .frame(minHeight: 150)
    }
}
