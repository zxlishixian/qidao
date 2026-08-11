import SwiftUI

struct RightSidebarView: View {
    @ObservedObject var viewModel: BoardViewModel
    @ObservedObject private var langManager = LanguageManager.shared

    var body: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 15) {
                // 1. Variation Tree - Common
                GroupBox(label: Label("Variation Tree".localized, systemImage: "arrow.triangle.branch")) {
                    VariationTreeView(viewModel: viewModel)
                        .frame(maxWidth: .infinity)
                        .frame(height: 240)
                }

                switch viewModel.appMode {
                case .analysis:
                    // 2. Move Evaluation - Fixed height
                    MoveEvaluationView(viewModel: viewModel)

                    // 3. Evaluation Board - Square based on width
                    if viewModel.config.display.showOwnership {
                        EvaluationBoardView(
                            viewModel: viewModel,
                            ownership: viewModel.isAnalyzing ? viewModel.analysisResult?.ownership : nil,
                            pv: viewModel.isAnalyzing ? viewModel.analysisResult?.sortedMoves(isWhiteTurn: viewModel.nextColor == .white).first?.pv : nil
                        )
                    }
                case .edit:
                    SGFPreviewView(viewModel: viewModel)
                        .frame(height: 400)
                case .play:
                    EmptyView()
                }
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .padding()
        }
        .frame(minWidth: 220, maxWidth: 500)
    }
}
