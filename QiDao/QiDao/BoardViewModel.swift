import AppKit
import Foundation
import Combine
import SwiftUI
import qidao_coreFFI

@MainActor
class BoardViewModel: ObservableObject {
    @Published private(set) var gameState = GameState()
    @Published private(set) var boardRevision: UInt64 = 0
    // SwiftUI should render a value snapshot, not repeatedly cross the FFI
    // boundary through Board while the app is inactive.  This matrix changes
    // identity on every GameManager publication and therefore invalidates the
    // board immediately even without a focus/mouse event in QiDao.
    @Published private(set) var boardCells: [[Int]] = Array(
        repeating: Array(repeating: 0, count: 19),
        count: 19
    )

    // Views observe BoardViewModel, so every value rendered by SwiftUI must
    // come from this published snapshot. Reading GameManager.internalState
    // here created a split data source: the screen position was already
    // written, but the board only redrew after an unrelated UI event.
    var board: Board { gameState.board }
    var boardSize: Int {
        get { gameState.boardSize }
        set { changeBoardSize(newValue) }
    }
    var isSizeLocked: Bool { gameState.isSizeLocked }
    var nextColor: StoneColor { gameState.nextColor }
    var lastMove: (x: Int, y: Int)? { gameState.lastMove }
    var moveCount: Int { gameState.moveCount }
    var maxMoveCount: Int { gameState.maxMoveCount }
    var variations: [Variation] { gameState.variations }
    var treeNodes: [TreeVisualNode] { gameState.treeNodes }
    var treeEdges: [TreeVisualEdge] { gameState.treeEdges }
    var currentNodeId: String { gameState.currentNodeId }
    var nodeComment: String { gameState.nodeComment }
    var moveNumbers: [String: Int] { gameState.moveNumbers }
    var metadata: GameMetadata { gameState.metadata }

    func displayedStone(x: Int, y: Int) -> StoneColor? {
        guard boardCells.indices.contains(y), boardCells[y].indices.contains(x) else { return nil }
        switch boardCells[y][x] {
        case 1: return .black
        case 2: return .white
        default: return nil
        }
    }

    // Settings with AppStorage for automatic persistence and reactivity
    @AppStorage("moveNumberDisplay") var moveNumberDisplay: MoveNumberDisplay = .all
    @AppStorage("showCoordinates") var showCoordinates: Bool = true
    @AppStorage("playSound") var playSound: Bool = true
    @AppStorage("boardSize") var persistedBoardSize: Int = 19
    @AppStorage("selectedThemeId") private var selectedThemeId: String = "wood"
    @AppStorage("lastSgfDirectory") private var lastSgfDirectoryPath: String = ""

    @Published var theme: BoardTheme = .defaultWood

    @Published var currentFileUrl: URL? = nil
    var lastSgfDirectory: URL? {
        get {
            lastSgfDirectoryPath.isEmpty ? nil : URL(fileURLWithPath: lastSgfDirectoryPath)
        }
        set {
            lastSgfDirectoryPath = newValue?.path ?? ""
        }
    }

    @Published var appMode: AppMode = .analysis {
        didSet {
            if appMode == .play {
                aiManager.clearAnalysisResult()
                checkAIMove()
                startClock()
            } else if appMode == .analysis {
                aiPlayTask?.cancel()
                aiPlayTask = nil
                lastAIPlayNodeId = nil
                aiManager.cancelPlay()
                updateAnalysis()
                stopClock()
            } else {
                aiPlayTask?.cancel()
                aiPlayTask = nil
                lastAIPlayNodeId = nil
                aiManager.cancelPlay()
                aiManager.clearAnalysisResult()
                stopClock()
            }
        }
    }

    @Published var activeEditTool: EditTool = .stoneAuto
    @Published var editLabelText: String = "A"

    // MARK: - Play Mode Clock
    @Published var playTimeSettings = PlayTimeSettings()
    @Published var clockState: PlayClockState? = nil
    @Published var showTimeoutDialog = false
    @Published var showResetConfirmation = false
    var clockTimer: Timer? = nil

    @Published var aiRole: AIRole = .manual {
        didSet {
            if appMode == .play {
                checkAIMove()
            }
        }
    }

    var nextSgfMove: (x: Int, y: Int)? {
        let children = gameManager.getGame().getCurrentNode().getChildren()
        if let first = children.first {
            let props = first.getProperties()
            if let moveProp = props.first(where: { $0.identifier == "B" || $0.identifier == "W" }),
               let value = moveProp.values.first {
                return AITrustBoundary.parseSgfCoordinate(value, boardSize: boardSize)
            }
        }
        return nil
    }

    func shouldShowMoveNumber(_ moveNum: Int?) -> Bool {
        guard let moveNum = moveNum else { return false }
        switch moveNumberDisplay {
        case .all: return true
        case .none: return false
        default:
            return moveNum > (moveCount - moveNumberDisplay.rawValue)
        }
    }

    func getDisplayMoveNumber(x: Int, y: Int) -> Int? {
        if let moveNum = moveNumbers["\(x),\(y)"], shouldShowMoveNumber(moveNum) {
            return moveNum
        }
        return nil
    }

    func getMarkerType(x: Int, y: Int, moveNumber: Int?) -> MarkerType? {
        guard let moveNum = moveNumber else { return nil }
        // Only show markers if move numbers are NOT shown for this stone
        if shouldShowMoveNumber(moveNum) { return nil }

        if moveNum == moveCount { return .last1 }
        if moveNum == moveCount - 1 { return .last2 }
        if moveNum == moveCount - 2 { return .last3 }
        return nil
    }

    func handleBoardClick(x: Int, y: Int) {
        switch appMode {
        case .analysis:
            placeStone(x: x, y: y)
        case .edit:
            handleEditClick(x: x, y: y)
        case .play:
            placeStone(x: x, y: y)
        }
    }

    private func handleEditClick(x: Int, y: Int) {
        let game = gameManager.getGame()
        let currentStone = board.getStone(x: UInt32(x), y: UInt32(y))

        switch activeEditTool {
        case .stoneBlack:
            if currentStone == .black {
                game.removeStone(x: UInt32(x), y: UInt32(y))
            } else {
                game.addStone(x: UInt32(x), y: UInt32(y), color: .black)
            }
        case .stoneWhite:
            if currentStone == .white {
                game.removeStone(x: UInt32(x), y: UInt32(y))
            } else {
                game.addStone(x: UInt32(x), y: UInt32(y), color: .white)
            }
        case .stoneAuto:
            // Toggle: Empty -> Black -> White -> Empty
            if currentStone == nil {
                game.addStone(x: UInt32(x), y: UInt32(y), color: .black)
            } else if currentStone == .black {
                game.addStone(x: UInt32(x), y: UInt32(y), color: .white)
            } else {
                game.removeStone(x: UInt32(x), y: UInt32(y))
            }
        case .markTriangle, .markCircle, .markSquare, .markCross:
            let markType = activeEditTool.markType!
            if gameState.marks.contains(where: { $0.x == x && $0.y == y && $0.type == markType }) {
                game.clearMarks(x: UInt32(x), y: UInt32(y))
            } else {
                game.addMark(x: UInt32(x), y: UInt32(y), markType: markType)
            }
        case .markLabel:
            if gameState.marks.contains(where: { $0.x == x && $0.y == y && $0.type == "LB" && $0.label == editLabelText }) {
                game.clearMarks(x: UInt32(x), y: UInt32(y))
            } else {
                game.addLabel(x: UInt32(x), y: UInt32(y), label: editLabelText)
                // Auto increment label if it's a number
                if let num = Int(editLabelText) {
                    editLabelText = "\(num + 1)"
                } else if editLabelText.count == 1, let char = editLabelText.first, char.isLetter {
                    if let scalar = char.unicodeScalars.first {
                        if let next = UnicodeScalar(scalar.value + 1), Character(next).isLetter {
                            editLabelText = String(next)
                        }
                    }
                }
            }
        case .clear:
            game.removeStone(x: UInt32(x), y: UInt32(y))
            game.clearMarks(x: UInt32(x), y: UInt32(y))
        }

        // Refresh game state
        gameManager.syncState()
    }

    func deleteCurrentBranch() {
        if gameManager.deleteCurrentBranch() {
            SoundManager.shared.play(name: "stone")
        }
    }

    func setNextPlayer(_ color: StoneColor) {
        gameManager.getGame().setNextPlayer(color: color)
        gameManager.syncState()
    }

    // AI Analysis
    @Published var isAnalyzing: Bool = false
    @Published var aiStatus: AIStatus = .idle
    @Published var engineMessage: String = "AI Not Started".localized
    @Published var analysisResult: AnalysisResult? = nil
    @Published var logEntries: [EngineLog] = []
    @Published var showAllLogs: Bool = false {
        didSet {
            aiManager.showAllLogs = showAllLogs
        }
    }
    @Published var winRateHistory: [Int: Double] = [:]
    @Published var scoreLeadHistory: [Int: Double] = [:]
    @Published var blunders: [Int: BlunderType] = [:]
    @Published var hoveredMoveStr: String? = nil
    @Published var config = ConfigManager.shared.config {
        didSet {
            if !config.display.showWinRateGraph {
                stopFullGameAnalysis()
            } else if isAnalyzing && !oldValue.display.showWinRateGraph {
                startFullGameAnalysis()
            }
        }
    }
    @Published var isFullGameScanning: Bool = false

    var gameManager: GameManager
    var aiManager: AIManager
    let screenAssistManager: ScreenAssistManager
    var isApplyingScreenSnapshot = false
    var aiPlayTask: Task<Void, Never>? = nil
    var lastAIPlayNodeId: String? = nil
    var sgfManager = SgfManager()
    private var cancellables = Set<AnyCancellable>()
    private var lastLiveWindowRefreshAt = Date.distantPast
    private var liveWindowRefreshScheduled = false
    var pendingLivePositionSequence = 0
    var awaitingFirstAnalysisResult = false
    var pendingAnalysisIsLive = false

    var treeWidth: CGFloat {
        let maxX = treeNodes.map { $0.x }.max() ?? 0
        return maxX
    }

    var treeHeight: CGFloat {
        let maxY = treeNodes.map { $0.y }.max() ?? 0
        return maxY
    }

    var langManager = LanguageManager.shared

    init() {
        // Use UserDefaults directly in init to avoid 'self' access before full initialization
        let savedSize = UserDefaults.standard.integer(forKey: "boardSize")
        let requestedSize = savedSize > 0 ? savedSize : 19
        let initialSize = AITrustBoundary.supportedBoardSize(requestedSize) ?? 19
        if requestedSize != initialSize {
            print("Unsupported saved board size \(requestedSize); using 19x19.")
            UserDefaults.standard.set(initialSize, forKey: "boardSize")
        }

        self.gameManager = GameManager(initialSize: initialSize)
        self.aiManager = AIManager()
        self.screenAssistManager = ScreenAssistManager()
        self.gameState = gameManager.gameState
        self.boardCells = Self.makeBoardCells(from: gameManager.gameState)

        let themeId = UserDefaults.standard.string(forKey: "selectedThemeId") ?? "wood"
        self.theme = (themeId == "bw") ? .bwPrint : .defaultWood

        setupBindings()
    }

    private func setupBindings() {
        screenAssistManager.onPosition = { [weak self] position in
            self?.applyScreenPosition(position)
        }
        screenAssistManager.onRequestReanalysis = { [weak self] in
            self?.updateAnalysis()
        }

        gameManager.$gameState
            .sink { [weak self] newState in
                guard let self else { return }
                self.boardCells = Self.makeBoardCells(from: newState)
                self.gameState = newState
                // Board is backed by an FFI reference type. A monotonically
                // changing identity guarantees that SwiftUI rebuilds the 361
                // intersections even when two snapshots have similar object
                // identity or updates are coalesced in one run-loop turn.
                self.boardRevision &+= 1
                if !self.isApplyingScreenSnapshot {
                    self.updateAnalysis()
                    self.refreshLiveWindowsIfNeeded(force: true)
                }
                if self.appMode == .play {
                    self.checkAIMove()
                    self.resetClockForCurrentTurn()
                }
            }
            .store(in: &cancellables)

        aiManager.$isAnalyzing.assign(to: &$isAnalyzing)
        aiManager.$aiStatus.assign(to: &$aiStatus)

        // Trigger AI move check when engine becomes ready or analysis starts
        Publishers.CombineLatest(aiManager.$isAnalyzing, aiManager.$isEngineReady)
            .sink { [weak self] analyzing, ready in
                if analyzing && ready && self?.appMode == .play {
                    self?.checkAIMove()
                }
            }
            .store(in: &cancellables)

        aiManager.$isEngineReady
            .sink { [weak self] ready in
                if ready {
                    self?.updateAnalysis()
                }
            }
            .store(in: &cancellables)
        aiManager.$engineMessage.assign(to: &$engineMessage)
        aiManager.$engineMessage
            .sink { [weak self] message in
                guard let self, self.analysisResult == nil else { return }
                self.screenAssistManager.updateAI(
                    message: message,
                    blackWinrate: nil,
                    scoreLead: nil,
                    visits: 0,
                    candidates: []
                )
                self.refreshLiveWindowsIfNeeded()
            }
            .store(in: &cancellables)
        aiManager.$analysisResult
            // KataGo can publish partial search results around 20 times per
            // second. Relaying each one through BoardViewModel invalidated the
            // entire 361-intersection SwiftUI board and could starve a live
            // vision position waiting on the same main actor. Preserve the
            // first/latest result while bounding presentation work to 8 Hz.
            .throttle(for: .milliseconds(120), scheduler: DispatchQueue.main, latest: true)
            .sink { [weak self] result in
                guard let self else { return }
                let isFirstCurrentResult = self.awaitingFirstAnalysisResult
                    && result?.moveInfos.isEmpty == false
                    && result?.id.hasSuffix("-\(self.currentNodeId)") == true
                let wasLiveRequest = self.pendingAnalysisIsLive
                if isFirstCurrentResult {
                    self.awaitingFirstAnalysisResult = false
                    self.pendingAnalysisIsLive = false
                }
                self.analysisResult = result
                guard let result else {
                    self.screenAssistManager.updateAI(
                        message: self.engineMessage,
                        blackWinrate: nil,
                        scoreLead: nil,
                        visits: 0,
                        candidates: []
                    )
                    self.refreshLiveWindowsIfNeeded()
                    return
                }
                let candidates = result
                    .sortedMoves(isWhiteTurn: self.nextColor == .white)
                    .prefix(8)
                    .enumerated()
                    .compactMap { index, info -> ScreenAICandidate? in
                        guard let point = self.decodeKataGoMove(info.moveStr) else { return nil }
                        return ScreenAICandidate(
                            vertex: info.moveStr,
                            x: point.x,
                            y: point.y,
                            winrate: info.winrate,
                            scoreLead: info.scoreLead,
                            visits: Int(info.visits),
                            rank: index + 1
                        )
                    }
                self.screenAssistManager.updateAI(
                    message: self.engineMessage,
                    blackWinrate: result.rootInfo.winrate,
                    scoreLead: result.rootInfo.scoreLead,
                    visits: Int(result.rootInfo.visits),
                    candidates: candidates
                )
                self.refreshLiveWindowsIfNeeded(force: isFirstCurrentResult)
                if isFirstCurrentResult,
                   !wasLiveRequest,
                   self.appMode == .analysis,
                   self.config.display.showWinRateGraph {
                    self.startFullGameAnalysis()
                }
            }
            .store(in: &cancellables)
        aiManager.$logEntries.assign(to: &$logEntries)
        aiManager.$winRateHistory.assign(to: &$winRateHistory)
        aiManager.$scoreLeadHistory.assign(to: &$scoreLeadHistory)
        aiManager.$blunders.assign(to: &$blunders)
        aiManager.$isFullGameScanning.assign(to: &$isFullGameScanning)
    }

    func refreshLiveWindowsIfNeeded(force: Bool = false) {
        if !force {
            guard screenAssistManager.isMonitoring || screenAssistManager.isReRecognizing else {
                return
            }
            let now = Date()
            guard now.timeIntervalSince(lastLiveWindowRefreshAt) >= 0.10 else { return }
            lastLiveWindowRefreshAt = now
        }

        // Published state is authoritative. Only invalidate the visible
        // content tree here and let AppKit/SwiftUI present it asynchronously.
        // Synchronous display waits can block on RenderBox while QiDao is
        // inactive, starving the vision messages that must advance the model.
        guard !liveWindowRefreshScheduled else { return }
        liveWindowRefreshScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.liveWindowRefreshScheduled = false
            for window in NSApp.windows where window.isVisible && !(window is NSPanel) {
                window.contentView?.needsLayout = true
                window.contentView?.needsDisplay = true
            }
        }
    }

    private static func makeBoardCells(from state: GameState) -> [[Int]] {
        (0..<state.boardSize).map { y in
            (0..<state.boardSize).map { x in
                switch state.board.getStone(x: UInt32(x), y: UInt32(y)) {
                case .black: return 1
                case .white: return 2
                case nil: return 0
                }
            }
        }
    }

    func placeStone(x: Int, y: Int, isAI: Bool = false) {
        if appMode == .play && !isAI {
            guard isHumanTurn else {
                SoundManager.shared.playAlert()
                return
            }
        }

        do {
            let captures = try gameManager.placeStone(x: x, y: y, color: nextColor)
            updateClockOnMove()
            if captures == 0 {
                SoundManager.shared.play(name: "stone")
            } else if captures == 1 {
                SoundManager.shared.play(name: "dead-stone")
            } else {
                SoundManager.shared.play(name: "dead-stones")
            }
        } catch {
            SoundManager.shared.playAlert()
        }
    }

    func goBack(playSound: Bool = true) {
        if gameManager.goBack() {
            if playSound {
                SoundManager.shared.play(name: "stone")
            }
        }
    }

    func goForward(index: Int = 0) {
        if let captures = gameManager.goForward(index: index) {
            if captures == 0 {
                SoundManager.shared.play(name: "stone")
            } else if captures == 1 {
                SoundManager.shared.play(name: "dead-stone")
            } else {
                SoundManager.shared.play(name: "dead-stones")
            }
        }
    }

    func nextVariation() {
        let game = gameManager.getGame()
        let count = Int(game.getVariationCount())
        if count > 1 {
            let currentIndex = Int(game.getCurrentVariationIndex())
            let nextIndex = (currentIndex + 1) % count
            if game.goBack() {
                goForward(index: nextIndex)
            }
        }
    }

    func previousVariation() {
        let game = gameManager.getGame()
        let count = Int(game.getVariationCount())
        if count > 1 {
            let currentIndex = Int(game.getCurrentVariationIndex())
            let prevIndex = (currentIndex - 1 + count) % count
            if game.goBack() {
                goForward(index: prevIndex)
            }
        }
    }

    func selectVariation(_ index: Int) {
        goForward(index: index)
    }

    func goToStart() {
        gameManager.jumpToMove(0)
    }

    func goToEnd() {
        gameManager.jumpToMove(maxMoveCount)
    }

    func jumpToMove(_ target: Int) {
        gameManager.jumpToMove(target)
    }

    func jumpToNode(id: String) {
        gameManager.jumpToNode(id: id)
    }

    // MARK: - AI Analysis

    func toggleAnalysis() {
        if isAnalyzing {
            stopAnalysis()
        } else {
            startAnalysis()
        }
    }

    func startAnalysis() {
        let profile = ConfigManager.shared.currentProfile
        let executable = profile.path
        var args = profile.extraArgs.split(separator: " ").map(String.init)

        if args.isEmpty {
            args = ["analysis"]
        }

        if !profile.config.isEmpty {
            args.append("-config")
            args.append(profile.config)
        }
        if !profile.model.isEmpty {
            args.append("-model")
            args.append(profile.model)
        }

        if args.contains("analysis") && profile.config.isEmpty {
            aiManager.addLog("Error: Config file is required for analysis mode".localized, isError: true)
            return
        }

        aiManager.start(executable: executable, args: args, config: config)
    }

    func stopAnalysis() {
        aiManager.stop()
    }

    func updateAnalysis() {
        self.hoveredMoveStr = nil
        guard appMode == .analysis else {
            // In non-analysis mode, we clear the current analysis result to hide overlays
            aiManager.clearAnalysisResult()
            return
        }

        // The baseline callback arrives just before the service confirms its
        // running state. Treat the whole calibrated screen-assist session as
        // live so its first query is not debounced behind a full-game scan.
        let isLiveScreenAnalysis = screenAssistManager.hasBaseline
            || screenAssistManager.isMonitoring
            || screenAssistManager.isReRecognizing
        if isAnalyzing {
            awaitingFirstAnalysisResult = true
            pendingAnalysisIsLive = isLiveScreenAnalysis
            // Full-game scanning has lower protocol priority but still shares
            // neural-network batches with the interactive query. Pause it so
            // the current position receives the first KataGo result first.
            stopFullGameAnalysis()
        }
        let game = gameManager.getGame()
        aiManager.updateAnalysis(
            currentNodeId: game.getCurrentNode().getId(),
            initialStones: game.getInitialStones(),
            moves: game.getAnalysisMoves(),
            nextPlayer: nextColor == .black ? "B" : "W",
            initialPlayer: gameState.initialColor == .black ? "B" : "W",
            turnNumber: moveCount,
            metadata: game.getMetadata(),
            config: config,
            fastResponse: isLiveScreenAnalysis
        )

        // Ordinary full-game scanning restarts after this node's first result,
        // so it cannot delay the interactive result that the board is waiting
        // to present.
    }

    func startFullGameAnalysis() {
        guard appMode == .analysis,
              config.display.showWinRateGraph,
              !screenAssistManager.isMonitoring else { return }
        let game = gameManager.getGame()

        let mainLineMoves = game.getMainLineMoves()
        let initialStones = game.getInitialStones()

        // Determine initial player without jumping
        var initialPlayer = "B"
        if let firstMove = mainLineMoves.first, firstMove.count > 0 {
            initialPlayer = firstMove[0]
        }

        aiManager.startFullGameAnalysis(
            mainLineMoves: mainLineMoves,
            initialStones: initialStones,
            metadata: game.getMetadata(),
            config: config,
            initialPlayer: initialPlayer
        )
    }

    func stopFullGameAnalysis() {
        aiManager.stopFullGameAnalysis()
    }

    func toggleTheme() {
        selectedThemeId = (selectedThemeId == "wood") ? "bw" : "wood"
        theme = (selectedThemeId == "bw") ? .bwPrint : .defaultWood
    }

    func decodeKataGoMove(_ move: String) -> (x: Int, y: Int)? {
        if case .move(let x, let y) = AITrustBoundary.parseMove(
            move.uppercased(),
            boardSize: Int(metadata.size)
        ) {
            return (x, y)
        }
        return nil
    }
}
