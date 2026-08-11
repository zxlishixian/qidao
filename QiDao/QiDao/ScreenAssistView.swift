import SwiftUI

/// Inline live-board capture controls for Analysis mode. The recognized
/// position is rendered by QiDao's main board, so no separate board window is
/// needed here.
struct ScreenAssistView: View {
    @ObservedObject var viewModel: BoardViewModel
    @ObservedObject private var manager: ScreenAssistManager
    @ObservedObject private var langManager = LanguageManager.shared
    @State private var showsSettings = false

    init(viewModel: BoardViewModel) {
        self.viewModel = viewModel
        self.manager = viewModel.screenAssistManager
    }

    var body: some View {
        GroupBox(label: Label("Live Game Analysis".localized, systemImage: "viewfinder")) {
            VStack(alignment: .leading, spacing: 9) {
                statusRow

                if !manager.hasScreenCapturePermission {
                    permissionNotice
                }

                if manager.hasBaseline {
                    nextPlayerControl
                    positionSummary
                    aiSummary
                } else if manager.phase != .selecting {
                    Text("Select a board once. QiDao will copy the current position and follow every new move automatically.".localized)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                controls
                if manager.hasBaseline {
                    recoveryControls
                }
                settings
            }
            .padding(5)
            .frame(maxWidth: .infinity)
        }
        .id(langManager.selectedLanguage)
        .onAppear { manager.refreshScreenCapturePermission() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            manager.refreshScreenCapturePermission()
        }
    }

    private var statusRow: some View {
        HStack(alignment: .top, spacing: 7) {
            Circle()
                .fill(statusColor)
                .frame(width: 9, height: 9)
                .padding(.top, 3)
            Text(manager.status)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(3)
                .frame(maxWidth: .infinity, alignment: .leading)
            if manager.isMonitoring {
                Text("LIVE")
                    .font(.system(size: 9, weight: .bold, design: .rounded))
                    .foregroundStyle(.green)
            }
        }
    }

    private var permissionNotice: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Screen Recording permission is required".localized, systemImage: "record.circle")
                .font(.caption)
                .foregroundStyle(.orange)
            HStack {
                Button("Request Permission".localized) { manager.requestScreenCapturePermission() }
                Button("Open Settings".localized) { manager.openScreenCaptureSettings() }
            }
            .controlSize(.small)
        }
        .padding(8)
        .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 7))
    }

    private var positionSummary: some View {
        Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 5) {
            GridRow {
                Text("Latest move".localized).foregroundStyle(.secondary)
                Text(manager.latestMove).font(.system(.caption, design: .monospaced).bold())
                Text("Recognized stones".localized).foregroundStyle(.secondary)
                Text("● \(manager.blackStoneCount)  ○ \(manager.whiteStoneCount)")
                    .monospacedDigit()
            }
            GridRow {
                Text("Tracking state".localized).foregroundStyle(.secondary)
                Text(manager.trackingModeText)
                    .foregroundStyle(
                        manager.trackingMode == "recovering" || manager.trackingMode == "fallback"
                            ? .orange : .primary
                    )
                Text("Detected candidate".localized).foregroundStyle(.secondary)
                Text(manager.candidate).font(.system(.caption, design: .monospaced).bold())
            }
            GridRow {
                Text("QiDao board".localized).foregroundStyle(.secondary)
                Label(
                    manager.isSyncedToQiDao ? "Synced".localized : "Waiting".localized,
                    systemImage: manager.isSyncedToQiDao ? "checkmark.circle.fill" : "clock"
                )
                .foregroundStyle(manager.isSyncedToQiDao ? .green : .orange)
                Text("Synced move".localized).foregroundStyle(.secondary)
                Text("\(manager.syncedMoveNumber)").monospacedDigit()
            }
        }
        .font(.caption)
    }

    private var nextPlayerControl: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Side to move".localized)
                .font(.caption.weight(.semibold))
            HStack(spacing: 6) {
                sideButton(color: "B", title: "Black to move".localized, foreground: .white, background: .black)
                sideButton(color: "W", title: "White to move".localized, foreground: .black, background: .white)
            }
        }
        .padding(7)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 7))
    }

    private func sideButton(
        color: String,
        title: String,
        foreground: Color,
        background: Color
    ) -> some View {
        Button {
            manager.setNextPlayer(color)
        } label: {
            HStack(spacing: 5) {
                Circle()
                    .fill(background)
                    .overlay(Circle().stroke(.secondary.opacity(0.5), lineWidth: 1))
                    .frame(width: 12, height: 12)
                Text(title)
                    .lineLimit(1)
            }
            .foregroundStyle(foreground)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 3)
        }
        .buttonStyle(.borderedProminent)
        .tint(manager.nextPlayer == color ? (color == "B" ? .black : .gray) : .secondary.opacity(0.45))
        .disabled(manager.isReRecognizing)
    }

    @ViewBuilder
    private var aiSummary: some View {
        if let best = manager.aiCandidates.first {
            HStack(spacing: 8) {
                Label("KataGo Suggestion".localized, systemImage: "sparkles")
                    .font(.caption)
                Text(best.vertex)
                    .font(.system(.headline, design: .monospaced).bold())
                    .foregroundStyle(.green)
                Spacer()
                if let black = manager.blackWinrate {
                    Text("\("Black".localized) \(black, format: .percent.precision(.fractionLength(0)))")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            .padding(7)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 7))
        } else if viewModel.isAnalyzing {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(manager.aiMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }

    private var controls: some View {
        HStack(spacing: 6) {
            Button {
                if manager.isMonitoring || manager.hasBaseline {
                    manager.toggleMonitoring()
                } else {
                    viewModel.startLiveGameAnalysis()
                }
            } label: {
                Label(primaryActionTitle, systemImage: primaryActionIcon)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(
                manager.phase == .selecting
                || manager.phase == .launching
                || manager.isReRecognizing
                || !manager.hasScreenCapturePermission
            )

            if manager.isCalibrated || manager.isServiceReady {
                Button(role: .destructive) {
                    viewModel.stopLiveGameAnalysis()
                } label: {
                    Image(systemName: "stop.fill")
                }
                .help("End Live Analysis".localized)
            }
        }
        .controlSize(.small)
    }

    private var settings: some View {
        DisclosureGroup("Recognition Settings".localized, isExpanded: $showsSettings) {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Picker("Size".localized, selection: $manager.boardSize) {
                        Text("19 × 19").tag(19)
                        Text("13 × 13").tag(13)
                        Text("9 × 9").tag(9)
                    }
                    Picker("Orientation".localized, selection: $manager.rotation) {
                        Text("0°").tag(0)
                        Text("180°").tag(180)
                    }
                }
                .pickerStyle(.menu)

                if manager.isCalibrated {
                    Button("Apply".localized) { manager.applySettings() }
                        .disabled(manager.isReRecognizing)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            .padding(.top, 5)
        }
        .font(.caption)
    }

    private var recoveryControls: some View {
        VStack(spacing: 6) {
            Button {
                manager.reRecognizeBoard()
            } label: {
                HStack(spacing: 6) {
                    if manager.isReRecognizing {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: "arrow.clockwise.circle")
                    }
                    Text(manager.isReRecognizing
                         ? "Re-recognizing Board".localized
                         : "Re-recognize Board".localized)
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)

            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 6) {
                Button("Reselect Board".localized, systemImage: "rectangle.dashed") {
                    viewModel.startLiveGameAnalysis()
                }
                Button("Record Pass".localized, systemImage: "hand.raised") {
                    manager.passTurn()
                }
                Button("Undo Recognition".localized, systemImage: "arrow.uturn.backward") {
                    manager.undo()
                }
                Button("Refresh AI".localized, systemImage: "sparkles") {
                    manager.requestReanalysis()
                }
            }
            .buttonStyle(.bordered)
        }
        .controlSize(.small)
        .disabled(manager.isReRecognizing || manager.phase == .selecting || manager.phase == .launching)
    }

    private var primaryActionTitle: String {
        if manager.isMonitoring { return "Pause Tracking".localized }
        if manager.hasBaseline { return "Resume Tracking".localized }
        if manager.phase == .selecting { return "Drag to Select Board".localized }
        return "Select Board and Start".localized
    }

    private var primaryActionIcon: String {
        if manager.isMonitoring { return "pause.fill" }
        if manager.hasBaseline { return "play.fill" }
        return "rectangle.dashed"
    }

    private var statusColor: Color {
        switch manager.phase {
        case .monitoring: return .green
        case .error: return .red
        case .baseline, .calibrated, .ready, .selecting: return .orange
        default: return .gray
        }
    }
}
