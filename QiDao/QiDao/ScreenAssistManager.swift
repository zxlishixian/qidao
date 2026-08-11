import AppKit
import CoreGraphics
import Darwin
import Foundation

private enum VisionProcessReaper {
    static func terminateAndReap(_ process: Process) {
        if process.isRunning { process.terminate() }
        DispatchQueue.global(qos: .utility).async {
            let terminateDeadline = Date().addingTimeInterval(1)
            while process.isRunning && Date() < terminateDeadline {
                Thread.sleep(forTimeInterval: 0.01)
            }
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
            process.waitUntilExit()
        }
    }
}

struct ScreenBoardPosition {
    let board: [[Int]]
    let lastMove: ScreenMove?
    let moveNumber: Int
    let nextPlayer: String
    let confidence: Double
    let sequence: Int
    let confirmation: String
}

struct ScreenAICandidate: Identifiable, Equatable {
    var id: String { vertex }
    let vertex: String
    let x: Int
    let y: Int
    let winrate: Double
    let scoreLead: Double
    let visits: Int
    let rank: Int
}

@MainActor
final class ScreenAssistManager: ObservableObject {
    enum Phase: String {
        case idle
        case launching
        case ready
        case selecting
        case calibrated
        case baseline
        case monitoring
        case error
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var status = "实时对弈分析尚未启动"
    @Published private(set) var isServiceReady = false
    @Published private(set) var hasScreenCapturePermission = CGPreflightScreenCaptureAccess()
    @Published private(set) var isCalibrated = false
    @Published private(set) var hasBaseline = false
    @Published private(set) var isMonitoring = false
    @Published private(set) var isReRecognizing = false
    @Published private(set) var confidence = 0.0
    @Published private(set) var trackingScore = 0.0
    @Published private(set) var anchorScore = 0.0
    @Published private(set) var gridScore = 0.0
    @Published private(set) var trackingMode = "idle"
    @Published private(set) var trackingFailures = 0
    @Published private(set) var isSyncedToQiDao = false
    @Published private(set) var syncedMoveNumber = 0
    @Published private(set) var candidate = "—"
    @Published private(set) var hoverPreview = "—"
    @Published private(set) var reconciliationDifferences = 0
    @Published private(set) var latestMove = "—"
    @Published private(set) var confirmedBoard: [[Int]] = []
    @Published private(set) var observedBoard: [[Int]] = []
    @Published private(set) var observedConfidence = 0.0
    @Published private(set) var boardAgreement = 1.0
    @Published private(set) var stableFrames = 0
    @Published private(set) var moveNumber = 0
    @Published private(set) var nextPlayer = "B"
    @Published private(set) var aiMessage = "KataGo 尚未启动"
    @Published private(set) var blackWinrate: Double?
    @Published private(set) var scoreLead: Double?
    @Published private(set) var aiVisits = 0
    @Published private(set) var aiCandidates: [ScreenAICandidate] = []
    @Published private(set) var captureLatencyMs = 0.0
    @Published private(set) var recognitionLatencyMs = 0.0
    @Published private(set) var verificationLatencyMs = 0.0
    @Published private(set) var aiResponseLatencyMs = 0.0
    @Published private(set) var scanSequence = 0
    @Published private(set) var positionSequence = 0
    @Published private(set) var appliedSequence = 0
    @Published private(set) var lastConfirmation = "—"
    @Published var boardSize = 19
    @Published var rotation = 0
    @Published var scanInterval = 0.12

    var onPosition: ((ScreenBoardPosition) -> Void)?
    var onRequestReanalysis: (() -> Void)?

    private var process: Process?
    private var input: FileHandle?
    private var outputFramer = VisionJSONLineFramer()
    private var processGeneration = VisionProcessGeneration()
    private var quad: [[Double]]?
    private let overlay = ScreenAssistOverlayController()
    private var permissionRequestAttempted = false
    private var autoStartAfterBaseline = false
    private var aiResponseStartedAt: Date?
    private var liveActivity: NSObjectProtocol?

    deinit {
        if let process {
            VisionProcessReaper.terminateAndReap(process)
        }
        if let liveActivity {
            ProcessInfo.processInfo.endActivity(liveActivity)
        }
    }

    func startServiceIfNeeded() {
        guard process == nil else { return }
        refreshScreenCapturePermission()
        if !hasScreenCapturePermission && !permissionRequestAttempted {
            requestScreenCapturePermission()
        }
        // A newly granted permission may synchronously start the service from
        // requestScreenCapturePermission(). Do not launch a second child.
        guard process == nil else { return }
        guard hasScreenCapturePermission else {
            phase = .error
            status = "当前签名身份尚未获得录屏权限；请点“重新请求权限”，授权后重新打开 QiDao"
            return
        }
        guard let service = locateVisionService(), let python = locatePython() else {
            fail("找不到视觉服务或带 OpenCV 的 Python；请先运行 qidao/setup.command")
            return
        }

        phase = .launching
        status = "正在启动屏幕识别服务…"
        outputFramer.reset()
        let child = Process()
        let generation = processGeneration.begin()
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        child.executableURL = python
        // The vision sources live inside the signed app bundle. Python's
        // default bytecode cache would modify sealed resources after launch,
        // invalidate the signature, and destabilize macOS TCC identity.
        child.arguments = ["-B", service.path]
        child.currentDirectoryURL = service.deletingLastPathComponent()
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = stderrPipe
        child.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                self?.finishVisionProcess(
                    process,
                    generation: generation,
                    message: "屏幕识别服务已退出（代码 \(process.terminationStatus)）",
                    shouldReap: false
                )
            }
        }
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self, weak child] handle in
            let data = handle.availableData
            DispatchQueue.main.async { [weak self, weak child] in
                guard let self, let child,
                      self.processGeneration.accepts(generation),
                      self.process === child else { return }
                guard !data.isEmpty else {
                    self.finishVisionProcess(
                        child,
                        generation: generation,
                        message: "屏幕识别服务输出已关闭",
                        shouldReap: true
                    )
                    return
                }
                self.consume(data)
            }
        }
        stderrPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if !data.isEmpty, let line = String(data: data, encoding: .utf8) {
                NSLog("QiDao vision: %@", line)
            }
        }
        do {
            try child.run()
            process = child
            input = stdinPipe.fileHandleForWriting
        } catch {
            processGeneration.invalidate()
            detachHandlers(child)
            outputFramer.reset()
            fail("无法启动屏幕识别服务：\(error.localizedDescription)")
        }
    }

    func startLiveGameAnalysis() {
        let previousPhase = phase
        let previousStatus = status
        startServiceIfNeeded()
        refreshScreenCapturePermission()
        guard hasScreenCapturePermission else { return }

        phase = .selecting
        status = "请拖选完整的实战棋盘网格"
        ScreenSelectionController.shared.select { [weak self] selected in
            guard let self else { return }
            guard let selected else {
                if self.isMonitoring {
                    self.phase = .monitoring
                    self.status = "正在持续识别实战棋盘"
                } else if self.hasBaseline {
                    self.phase = .baseline
                    self.status = "屏幕识别已暂停"
                } else if self.isServiceReady {
                    self.phase = .ready
                    self.status = "识别服务就绪；请拖选实战棋盘"
                } else {
                    self.phase = previousPhase
                    self.status = previousStatus
                }
                return
            }
            if self.isMonitoring { self.send(["command": "stop"]) }
            self.overlay.hideAll()
            self.quad = selected
            self.isCalibrated = true
            self.hasBaseline = false
            self.isMonitoring = false
            self.isReRecognizing = false
            self.confirmedBoard = []
            self.observedBoard = []
            self.moveNumber = 0
            self.latestMove = "—"
            self.candidate = "—"
            self.hoverPreview = "—"
            self.reconciliationDifferences = 0
            self.trackingScore = 0
            self.anchorScore = 0
            self.gridScore = 0
            self.trackingFailures = 0
            self.isSyncedToQiDao = false
            self.syncedMoveNumber = 0
            self.captureLatencyMs = 0
            self.recognitionLatencyMs = 0
            self.verificationLatencyMs = 0
            self.aiResponseLatencyMs = 0
            self.aiResponseStartedAt = nil
            self.scanSequence = 0
            self.positionSequence = 0
            self.appliedSequence = 0
            self.lastConfirmation = "—"
            self.phase = .calibrated
            self.status = "正在识别并载入当前棋盘…"
            self.trackingMode = "calibrating"
            self.refreshTrackingOverlay()
            self.captureBaseline(autoStart: true)
        }
    }

    func captureBaseline() {
        captureBaseline(autoStart: false)
    }

    /// Discard the visual state machine's remembered position and recognize a
    /// fresh full-board snapshot without asking the user to draw another box.
    /// `quad` is continuously updated from the tracker, so this also uses the
    func reRecognizeBoard() {
        guard isCalibrated, quad != nil else {
            status = "请先框选一次实战棋盘"
            return
        }
        guard !isReRecognizing, phase != .selecting, phase != .launching else { return }

        let resumeAfterRecognition = isMonitoring
        beginLiveActivity()
        if isMonitoring { send(["command": "stop"]) }
        overlay.hideAll()
        isReRecognizing = true
        isSyncedToQiDao = false
        candidate = "—"
        hoverPreview = "—"
        reconciliationDifferences = 0
        scanSequence = 0
        positionSequence = 0
        appliedSequence = 0
        lastConfirmation = "—"
        trackingMode = "calibrating"
        phase = .calibrated
        status = "正在重新识别完整棋盘并覆盖 QiDao…"
        captureBaseline(autoStart: resumeAfterRecognition, reuseCalibration: true)
    }

    private func captureBaseline(autoStart: Bool) {
        captureBaseline(autoStart: autoStart, reuseCalibration: false)
    }

    private func captureBaseline(autoStart: Bool, reuseCalibration: Bool) {
        guard quad != nil else {
            status = "请先拖选实战棋盘"
            return
        }
        autoStartAfterBaseline = autoStart
        let canReuseCalibration = reuseCalibration && process != nil && hasBaseline
        startServiceIfNeeded()
        if canReuseCalibration {
            // Manual refresh keeps the tracker's latest corrected quad and
            // jumps straight to a fresh full-board consensus. Re-running the
            // locator and rebuilding both templates made the button slower
            // than an ordinary move even though the board was already locked.
            send(["command": "rebaseline"])
        } else {
            configureService()
            send(["command": "baseline"])
        }
    }

    func toggleMonitoring() {
        if isMonitoring {
            send(["command": "stop"])
        } else {
            guard hasBaseline else {
                status = "请先识别并载入当前棋盘"
                return
            }
            send(["command": "start"])
        }
    }

    func refreshScreenCapturePermission() {
        hasScreenCapturePermission = CGPreflightScreenCaptureAccess()
        if hasScreenCapturePermission, phase == .error, process == nil {
            phase = .idle
            status = "录屏权限已生效，可以启动实时对弈分析"
        }
    }

    func requestScreenCapturePermission() {
        permissionRequestAttempted = true
        let granted = CGRequestScreenCaptureAccess()
        hasScreenCapturePermission = granted || CGPreflightScreenCaptureAccess()
        if hasScreenCapturePermission {
            status = "录屏权限已获得，正在启动识别服务…"
            startServiceIfNeeded()
        } else {
            phase = .error
            status = "macOS 尚未向当前 QiDao 构建授权；请在系统设置中启用后重新打开应用"
        }
    }

    func openScreenCaptureSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") else { return }
        NSWorkspace.shared.open(url)
    }

    func requestReanalysis() {
        onRequestReanalysis?()
    }

    func passTurn() {
        isSyncedToQiDao = false
        status = "正在记录停一手并切换下一手颜色…"
        send(["command": "pass"])
    }

    func undo() {
        isSyncedToQiDao = false
        status = "正在撤销最近一手视觉识别…"
        send(["command": "undo"])
    }

    func setNextPlayer(_ color: String) {
        let normalized = color.uppercased()
        guard hasBaseline, normalized == "B" || normalized == "W" else { return }
        nextPlayer = normalized
        isSyncedToQiDao = false
        status = normalized == "B"
            ? "正在校正为黑方下一手并同步到 QiDao…"
            : "正在校正为白方下一手并同步到 QiDao…"
        send(["command": "setNextPlayer", "color": normalized])
    }

    var blackStoneCount: Int {
        confirmedBoard.flatMap { $0 }.filter { $0 == 1 }.count
    }

    var whiteStoneCount: Int {
        confirmedBoard.flatMap { $0 }.filter { $0 == 2 }.count
    }

    func applySettings() {
        let wasRunning = isMonitoring
        if wasRunning { send(["command": "stop"]) }
        hasBaseline = false
        isSyncedToQiDao = false
        syncedMoveNumber = 0
        confirmedBoard = []
        observedBoard = []
        moveNumber = 0
        candidate = "—"
        hoverPreview = "—"
        reconciliationDifferences = 0
        scanSequence = 0
        positionSequence = 0
        appliedSequence = 0
        lastConfirmation = "—"
        if quad != nil {
            status = "规格已更新，正在重新载入棋盘…"
            captureBaseline(autoStart: wasRunning)
        }
    }

    func stopService() {
        overlay.hideAll()
        autoStartAfterBaseline = false
        processGeneration.invalidate()
        if let process {
            send(["command": "shutdown"])
            detachAndReap(process)
        }
        self.process = nil
        input = nil
        quad = nil
        phase = .idle
        isServiceReady = false
        isCalibrated = false
        hasBaseline = false
        isMonitoring = false
        confirmedBoard = []
        observedBoard = []
        outputFramer.reset()
        moveNumber = 0
        candidate = "—"
        hoverPreview = "—"
        reconciliationDifferences = 0
        latestMove = "—"
        trackingMode = "idle"
        trackingFailures = 0
        trackingScore = 0
        anchorScore = 0
        gridScore = 0
        isSyncedToQiDao = false
        syncedMoveNumber = 0
        captureLatencyMs = 0
        recognitionLatencyMs = 0
        verificationLatencyMs = 0
        aiResponseLatencyMs = 0
        aiResponseStartedAt = nil
        scanSequence = 0
        positionSequence = 0
        appliedSequence = 0
        lastConfirmation = "—"
        isReRecognizing = false
        endLiveActivity()
        status = "实时对弈分析已结束"
    }

    func showSuggestion(move: String?) {
        guard isMonitoring, let move, let point = point(for: move) else {
            overlay.hideSuggestion()
            return
        }
        overlay.showSuggestion(
            at: point,
            label: move,
            diameter: boardMarkerDiameter()
        )
    }

    func reportReconciliationError(_ message: String) {
        isSyncedToQiDao = false
        status = message
    }

    func reportQiDaoPositionApplied(board: [[Int]], moveNumber: Int, sequence: Int) {
        guard board == confirmedBoard else {
            reportReconciliationError("视觉局面与 QiDao 分析棋盘不一致，正在重新同步")
            return
        }
        isSyncedToQiDao = true
        syncedMoveNumber = moveNumber
        appliedSequence = max(appliedSequence, sequence)
        if sequence > 0 {
            send(["command": "ackPosition", "sequence": sequence])
        }
        let stones = board.flatMap { $0 }.filter { $0 == 1 || $0 == 2 }.count
        status = "已同步到 QiDao 分析棋盘：第 \(moveNumber) 手，共 \(stones) 子"
    }

    func updateAI(
        message: String,
        blackWinrate: Double?,
        scoreLead: Double?,
        visits: Int,
        candidates: [ScreenAICandidate]
    ) {
        aiMessage = message
        self.blackWinrate = blackWinrate
        self.scoreLead = scoreLead
        aiVisits = visits
        aiCandidates = candidates
        if !candidates.isEmpty, let started = aiResponseStartedAt {
            aiResponseLatencyMs = Date().timeIntervalSince(started) * 1000
            aiResponseStartedAt = nil
        }
        showSuggestion(move: candidates.first?.vertex)
    }

    func beginAIResponseTiming() {
        aiResponseStartedAt = Date()
        aiResponseLatencyMs = 0
    }

    var visualMismatchCount: Int {
        guard confirmedBoard.count == boardSize, observedBoard.count == boardSize else { return 0 }
        var count = 0
        for y in 0..<boardSize where confirmedBoard[y].count == boardSize && observedBoard[y].count == boardSize {
            for x in 0..<boardSize {
                let observed = observedBoard[y][x]
                if observed != 3 && observed != confirmedBoard[y][x] { count += 1 }
            }
        }
        return count
    }

    var trackingModeText: String {
        switch trackingMode {
        case "tracking": return "Following board".localized
        case "reanchored": return "Grid corrected".localized
        case "verified": return "Move verified".localized
        case "recovered": return "Board reacquired".localized
        case "recovering": return "Reacquiring board".localized
        case "degraded": return "Verifying board lock".localized
        case "fallback": return "Using verified grid".localized
        case "calibrated", "calibrating": return "Locating board".localized
        default: return "Waiting".localized
        }
    }

    private func configureService() {
        guard let quad else { return }
        send([
            "command": "configure",
            "quad": quad,
            "size": boardSize,
            "rotation": rotation,
            "interval": scanInterval,
            "threshold": 0.61,
            "stableFrames": 2,
        ])
    }

    private func send(_ object: [String: Any]) {
        startServiceIfNeeded()
        guard let input else { return }
        do {
            var data = try JSONSerialization.data(withJSONObject: object)
            data.append(0x0A)
            try input.write(contentsOf: data)
        } catch {
            fail("无法向识别服务发送命令：\(error.localizedDescription)")
        }
    }

    private func consume(_ data: Data) {
        do {
            for line in try outputFramer.append(data) {
                guard !line.isEmpty,
                      let value = try? JSONSerialization.jsonObject(with: line),
                      let message = value as? [String: Any] else { continue }
                handleVisionMessage(message)
            }
        } catch {
            processGeneration.invalidate()
            outputFramer.reset()
            if let process {
                self.process = nil
                input = nil
                detachAndReap(process)
            }
            isServiceReady = false
            isMonitoring = false
            fail("屏幕识别服务输出超过 64 KiB 协议上限，已安全终止")
        }
    }

    /// Main-actor boundary between the vision protocol and QiDao state.
    /// Keeping this independently exercisable lets the end-to-end smoke test
    /// prove that consecutive service messages reach the rendered board
    /// without a focus or settings click.
    func handleVisionMessage(_ message: [String: Any]) {
        let event = message["event"] as? String ?? ""
        if event != "baseline" && event != "position" && event != "undo" {
            updateQuad(message["quad"])
        }
        switch event {
        case "ready":
            let captureReady = message["captureReady"] as? Bool ?? false
            guard captureReady else {
                isServiceReady = false
                fail(message["message"] as? String ?? "真实录屏测试失败；请检查当前应用的录屏权限")
                return
            }
            isServiceReady = true
            if phase != .selecting {
                phase = isCalibrated ? .calibrated : .ready
                status = "识别服务就绪；请拖选实战棋盘"
            }
        case "configured":
            phase = .calibrated
        case "status":
            status = message["message"] as? String ?? status
        case "baseline":
            guard let snapshot = AITrustBoundary.validatedVisionSnapshot(
                message,
                event: .baseline,
                boardSize: boardSize
            ) else { return }
            updateQuad(message["quad"])
            isReRecognizing = false
            reconciliationDifferences = 0
            hoverPreview = "—"
            hasBaseline = true
            phase = .baseline
            status = message["message"] as? String ?? "当前棋盘局面已载入"
            confirmedBoard = snapshot.board
            observedBoard = snapshot.observedBoard
            observedConfidence = message["observedConfidence"] as? Double ?? observedConfidence
            moveNumber = snapshot.moveNumber
            nextPlayer = snapshot.nextPlayer
            latestMove = snapshot.lastMove?.vertex ?? "—"
            updateTrackingState(message)
            refreshTrackingOverlay()
            onPosition?(ScreenBoardPosition(
                board: confirmedBoard,
                lastMove: nil,
                moveNumber: moveNumber,
                nextPlayer: nextPlayer,
                confidence: 1,
                sequence: 0,
                confirmation: "baseline"
            ))
            if autoStartAfterBaseline {
                autoStartAfterBaseline = false
                status = "当前局面已同步，正在启动持续跟踪…"
                send(["command": "start"])
            } else {
                endLiveActivity()
            }
        case "running":
            isMonitoring = message["running"] as? Bool ?? false
            if isMonitoring {
                beginLiveActivity()
            } else if !isReRecognizing {
                endLiveActivity()
            }
            updateTrackingState(message)
            if isReRecognizing && !isMonitoring {
                phase = .calibrated
                status = "正在重新识别完整棋盘并覆盖 QiDao…"
            } else {
                phase = isMonitoring ? .monitoring : .baseline
                status = isMonitoring ? "正在持续识别实战棋盘" : "屏幕识别已暂停"
            }
            if isMonitoring {
                showSuggestion(move: aiCandidates.first?.vertex)
                refreshTrackingOverlay()
            }
            if !isMonitoring { overlay.hideAll() }
        case "scan":
            guard AITrustBoundary.isValidVisionScan(message, boardSize: boardSize),
                  let nextScanSequence = message["scanSequence"] as? Int,
                  let nextMoveNumber = message["moveNumber"] as? Int,
                  let nextPlayerValue = message["nextPlayer"] as? String else { return }
            if nextScanSequence != scanSequence { scanSequence = nextScanSequence }
            updateTrackingState(message)
            updatePerformance(message)
            if message["unchanged"] as? Bool == true {
                // Heartbeats keep the tracking frame aligned but contain no
                // board data. Avoid publishing a dozen identical properties
                // on the main actor while another app has focus.
                refreshTrackingOverlay()
                break
            }
            if let value = message["confidence"] as? Double, value != confidence { confidence = value }
            if let value = message["observedConfidence"] as? Double, value != observedConfidence {
                observedConfidence = value
            }
            if let value = message["boardAgreement"] as? Double, value != boardAgreement {
                boardAgreement = value
            }
            if let value = message["stableFrames"] as? Int, value != stableFrames { stableFrames = value }
            if let board = decodeBoard(message["observedBoard"], allowsUnknown: true), board != observedBoard {
                observedBoard = board
            }
            if let board = decodeBoard(message["confirmedBoard"]), board != confirmedBoard { confirmedBoard = board }
            if nextMoveNumber != moveNumber { moveNumber = nextMoveNumber }
            if nextPlayerValue != nextPlayer { nextPlayer = nextPlayerValue }
            let nextCandidate = decodeMove(message["candidate"])?.vertex ?? "—"
            if nextCandidate != candidate { candidate = nextCandidate }
            if let previews = message["hoverPreviews"] as? [[String: Any]],
               let first = previews.first {
                let nextPreview = decodeMove(first)?.vertex ?? "—"
                if nextPreview != hoverPreview { hoverPreview = nextPreview }
            } else {
                if hoverPreview != "—" { hoverPreview = "—" }
            }
            let differences = message["reconciliationDifferences"] as? Int ?? 0
            if differences != reconciliationDifferences { reconciliationDifferences = differences }
            refreshTrackingStatus()
            refreshTrackingOverlay()
        case "position":
            guard let snapshot = AITrustBoundary.validatedVisionSnapshot(
                message,
                event: .position,
                boardSize: boardSize
            ) else { return }
            updateQuad(message["quad"])
            reconciliationDifferences = 0
            hoverPreview = "—"
            candidate = "—"
            stableFrames = 0
            if snapshot.scanSequence != scanSequence { scanSequence = snapshot.scanSequence }
            if snapshot.sequence != positionSequence { positionSequence = snapshot.sequence }
            if snapshot.confirmation != lastConfirmation { lastConfirmation = snapshot.confirmation }
            confidence = snapshot.confidence
            trackingScore = message["trackingScore"] as? Double ?? trackingScore
            anchorScore = message["anchorScore"] as? Double ?? anchorScore
            updateTrackingState(message)
            let board = snapshot.board
            confirmedBoard = board
            observedBoard = snapshot.observedBoard
            let last = snapshot.lastMove
            latestMove = last?.vertex ?? "PASS"
            moveNumber = snapshot.moveNumber
            nextPlayer = snapshot.nextPlayer
            updatePerformance(message)
            let position = ScreenBoardPosition(
                board: board,
                lastMove: last,
                moveNumber: moveNumber,
                nextPlayer: nextPlayer,
                confidence: confidence,
                sequence: positionSequence,
                confirmation: lastConfirmation
            )
            onPosition?(position)
            if let last, !last.isPass, let point = point(x: last.x, y: last.y) {
                overlay.showLatest(
                    at: point,
                    label: last.vertex,
                    diameter: boardMarkerDiameter()
                )
            }
            refreshTrackingStatus()
            refreshTrackingOverlay()
        case "undo":
            guard let snapshot = AITrustBoundary.validatedVisionSnapshot(
                message,
                event: .undo,
                boardSize: boardSize
            ) else { return }
            updateQuad(message["quad"])
            reconciliationDifferences = 0
            hoverPreview = "—"
            confirmedBoard = snapshot.board
            observedBoard = snapshot.observedBoard
            moveNumber = snapshot.moveNumber
            nextPlayer = snapshot.nextPlayer
            onPosition?(ScreenBoardPosition(
                board: snapshot.board,
                lastMove: nil,
                moveNumber: snapshot.moveNumber,
                nextPlayer: snapshot.nextPlayer,
                confidence: 1,
                sequence: 0,
                confirmation: "undo"
            ))
            overlay.hideLatest()
        case "rejected", "warning":
            status = message["message"] as? String ?? "识别结果已被稳定性检查丢弃"
            updateTrackingState(message)
            refreshTrackingOverlay()
        case "error":
            fail(message["message"] as? String ?? "屏幕识别发生错误")
        default:
            break
        }
    }

    private func decodeMove(_ value: Any?) -> ScreenMove? {
        AITrustBoundary.validatedVisionMove(value, boardSize: boardSize)
    }

    private func decodeBoard(_ value: Any?, allowsUnknown: Bool = false) -> [[Int]]? {
        AITrustBoundary.validatedVisionBoard(
            value,
            boardSize: boardSize,
            allowsUnknown: allowsUnknown
        )
    }

    private func detachAndReap(_ process: Process) {
        detachHandlers(process)
        VisionProcessReaper.terminateAndReap(process)
    }

    private func detachHandlers(_ process: Process) {
        process.terminationHandler = nil
        (process.standardOutput as? Pipe)?.fileHandleForReading.readabilityHandler = nil
        (process.standardError as? Pipe)?.fileHandleForReading.readabilityHandler = nil
    }

    private func finishVisionProcess(
        _ process: Process,
        generation: VisionProcessToken,
        message: String,
        shouldReap: Bool
    ) {
        guard processGeneration.accepts(generation), self.process === process else { return }
        processGeneration.invalidate()
        self.process = nil
        input = nil
        outputFramer.reset()
        isServiceReady = false
        isMonitoring = false
        isReRecognizing = false
        endLiveActivity()
        overlay.hideAll()
        if shouldReap {
            detachAndReap(process)
        } else {
            detachHandlers(process)
        }
        if phase != .idle { fail(message) }
    }

    private func emptyBoard() -> [[Int]] {
        Array(repeating: Array(repeating: 0, count: boardSize), count: boardSize)
    }

    private func updateQuad(_ value: Any?) {
        guard let rows = value as? [[Any]], rows.count == 4 else { return }
        let parsed = rows.compactMap { row -> [Double]? in
            guard row.count == 2,
                  let x = (row[0] as? NSNumber)?.doubleValue,
                  let y = (row[1] as? NSNumber)?.doubleValue else { return nil }
            return [x, y]
        }
        if parsed.count == 4 { quad = parsed }
    }

    private func updateTrackingState(_ message: [String: Any]) {
        if let value = message["trackingScore"] as? Double, value != trackingScore { trackingScore = value }
        if let value = message["anchorScore"] as? Double, value != anchorScore { anchorScore = value }
        if let value = message["gridScore"] as? Double, value != gridScore { gridScore = value }
        if let value = message["trackingMode"] as? String, value != trackingMode { trackingMode = value }
        if let value = message["trackingFailures"] as? Int, value != trackingFailures {
            trackingFailures = value
        }
    }

    private func updatePerformance(_ message: [String: Any]) {
        if let value = message["captureMs"] as? Double, value != captureLatencyMs { captureLatencyMs = value }
        if let value = message["recognitionMs"] as? Double, value != recognitionLatencyMs {
            recognitionLatencyMs = value
        }
        if let value = message["verificationMs"] as? Double, value != verificationLatencyMs {
            verificationLatencyMs = value
        }
    }

    private func refreshTrackingStatus() {
        guard isMonitoring else { return }
        if hoverPreview != "—" {
            status = "已忽略鼠标悬停棋子虚影：\(hoverPreview)"
            return
        }
        if reconciliationDifferences > 0 {
            status = "正在核对并恢复棋盘：发现 \(reconciliationDifferences) 个差异点"
            return
        }
        switch trackingMode {
        case "recovering":
            status = "正在扩大范围找回棋盘，识别不会停止"
        case "degraded":
            status = "单帧定位置信度偏低，正在复核"
        case "fallback":
            status = "外观跟踪置信度偏低；已使用校准网格继续识别并后台重定位"
        default:
            if isSyncedToQiDao {
                status = "棋盘跟踪稳定；已同步到 QiDao 第 \(syncedMoveNumber) 手"
            } else {
                status = "正在持续识别并同步实战棋盘"
            }
        }
    }

    private func refreshTrackingOverlay() {
        guard isCalibrated,
              phase != .idle,
              phase != .selecting,
              let points = trackingQuadPoints() else {
            overlay.hideTrackingFrame()
            return
        }
        let frameConfidence = min(trackingScore, anchorScore)
        overlay.showTrackingFrame(
            points: points,
            confidence: frameConfidence,
            mode: trackingMode,
            label: trackingModeText
        )

        // Markers use the same moving quad. Reposition them on every scan so
        // they remain attached to their intersections when the game window is
        // dragged without waiting for the next KataGo result.
        guard isMonitoring else { return }
        showSuggestion(move: aiCandidates.first?.vertex)
        if let point = point(for: latestMove), latestMove != "—" {
            overlay.showLatest(
                at: point,
                label: latestMove,
                diameter: boardMarkerDiameter()
            )
        }
    }

    /// Match screen markers to the actual client stone size instead of using
    /// one fixed 42-point circle for every board and window scale.
    private func boardMarkerDiameter() -> CGFloat {
        guard let quad, quad.count == 4, boardSize > 1 else { return 24 }
        func distance(_ first: [Double], _ second: [Double]) -> Double {
            hypot(second[0] - first[0], second[1] - first[1])
        }
        let edgeLength = distance(quad[0], quad[1])
            + distance(quad[1], quad[2])
            + distance(quad[2], quad[3])
            + distance(quad[3], quad[0])
        let averageSpacing = edgeLength / (4 * Double(boardSize - 1))
        return CGFloat(min(56, max(14, averageSpacing * 0.90)))
    }

    private func trackingQuadPoints() -> [NSPoint]? {
        guard let quad, quad.count == 4 else { return nil }
        let globalTop = NSScreen.screens.map { NSMaxY($0.frame) }.max()
            ?? NSScreen.main?.frame.maxY
            ?? 0
        return quad.map { NSPoint(x: $0[0], y: globalTop - $0[1]) }
    }

    private func point(for vertex: String) -> NSPoint? {
        let text = vertex.uppercased()
        let columns = "ABCDEFGHJKLMNOPQRST"
        guard text != "PASS", let column = text.first,
              let columnIndex = columns.firstIndex(of: column),
              let row = Int(text.dropFirst()) else { return nil }
        return point(x: columns.distance(from: columns.startIndex, to: columnIndex),
                     y: boardSize - row)
    }

    private func point(x: Int, y: Int) -> NSPoint? {
        guard let quad, quad.count == 4, boardSize > 1 else { return nil }
        var logicalX = x
        var logicalY = y
        if rotation == 180 {
            logicalX = boardSize - 1 - logicalX
            logicalY = boardSize - 1 - logicalY
        }
        let tx = Double(logicalX) / Double(boardSize - 1)
        let ty = Double(logicalY) / Double(boardSize - 1)
        let xPosition = (1 - tx) * (1 - ty) * quad[0][0]
            + tx * (1 - ty) * quad[1][0]
            + tx * ty * quad[2][0]
            + (1 - tx) * ty * quad[3][0]
        let captureY = (1 - tx) * (1 - ty) * quad[0][1]
            + tx * (1 - ty) * quad[1][1]
            + tx * ty * quad[2][1]
            + (1 - tx) * ty * quad[3][1]
        let globalTop = NSScreen.screens.map { NSMaxY($0.frame) }.max() ?? NSScreen.main?.frame.maxY ?? 0
        return NSPoint(x: xPosition, y: globalTop - captureY)
    }

    private func locateVisionService() -> URL? {
        // A built app must use the service and signed capture helper sealed in
        // its own bundle.  Looking at #filePath first accidentally selected
        // the source-tree service whenever the repository was still present,
        // which in turn launched the ad-hoc .build/screen-tool and lost TCC.
        if let bundled = Bundle.main.url(forResource: "vision_service", withExtension: "py", subdirectory: "vision") {
            return bundled
        }
        let file = URL(fileURLWithPath: #filePath)
        let source = file.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("vision/vision_service.py")
        if FileManager.default.fileExists(atPath: source.path) { return source }
        return nil
    }

    private func locatePython() -> URL? {
        let file = URL(fileURLWithPath: #filePath)
        let project = file.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        let candidates = [
            project.appendingPathComponent(".venv/bin/python"),
            URL(fileURLWithPath: "/opt/homebrew/bin/python3"),
            URL(fileURLWithPath: "/usr/local/bin/python3"),
            URL(fileURLWithPath: "/usr/bin/python3"),
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    private func fail(_ message: String) {
        phase = .error
        status = message
        isMonitoring = false
        isReRecognizing = false
        autoStartAfterBaseline = false
        endLiveActivity()
        overlay.hideAll()
    }

    private func beginLiveActivity() {
        guard liveActivity == nil else { return }
        liveActivity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .latencyCritical],
            reason: "QiDao realtime board recognition and analysis"
        )
    }

    private func endLiveActivity() {
        guard let liveActivity else { return }
        ProcessInfo.processInfo.endActivity(liveActivity)
        self.liveActivity = nil
    }
}

private final class SelectionCanvas: NSView {
    var completion: ((NSRect?) -> Void)?
    private var start = NSPoint.zero
    private var current = NSPoint.zero
    private var dragging = false

    override var acceptsFirstResponder: Bool { true }

    override func mouseDown(with event: NSEvent) {
        start = convert(event.locationInWindow, from: nil)
        current = start
        dragging = true
        needsDisplay = true
    }

    override func mouseDragged(with event: NSEvent) {
        current = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseUp(with event: NSEvent) {
        current = convert(event.locationInWindow, from: nil)
        dragging = false
        let rect = NSRect(x: min(start.x, current.x), y: min(start.y, current.y),
                          width: abs(start.x - current.x), height: abs(start.y - current.y))
        completion?(rect.width >= 160 && rect.height >= 160 ? rect : nil)
    }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 { completion?(nil) }
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.withAlphaComponent(0.48).setFill()
        bounds.fill()
        if start != current {
            let rect = NSRect(x: min(start.x, current.x), y: min(start.y, current.y),
                              width: abs(start.x - current.x), height: abs(start.y - current.y))
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(rect: rect).addClip()
            NSColor.clear.setFill()
            rect.fill(using: .copy)
            NSGraphicsContext.restoreGraphicsState()
            NSColor.systemMint.setStroke()
            let path = NSBezierPath(rect: rect)
            path.lineWidth = 3
            path.stroke()
        }
        let title = "拖选完整实战棋盘网格 · Esc 取消"
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 20, weight: .semibold),
            .foregroundColor: NSColor.white,
        ]
        let size = title.size(withAttributes: attributes)
        title.draw(at: NSPoint(x: bounds.midX - size.width / 2, y: bounds.maxY - 72), withAttributes: attributes)
    }
}

@MainActor
private final class ScreenSelectionController {
    static let shared = ScreenSelectionController()
    private var window: NSWindow?

    func select(completion: @escaping ([[Double]]?) -> Void) {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        guard let screen else { completion(nil); return }
        let panel = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        panel.title = "QiDao 棋盘框选"
        panel.identifier = NSUserInterfaceItemIdentifier("QiDao.ScreenSelection")
        panel.setAccessibilityTitle("QiDao 棋盘框选")
        panel.isOpaque = false
        panel.backgroundColor = .clear
        // `screenSaver` windows block macOS accessibility automation. A
        // pop-up-menu level is still above every normal game/browser window
        // while allowing real mouse-driven end-to-end tests.
        panel.level = .popUpMenu
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        // Keep the selection surface visible to Screen Recording and UI
        // automation. It is ordered out before baseline capture below, so it
        // cannot contaminate recognition; hiding it from WindowServer made
        // end-to-end mouse-driven validation blind and unreliable.
        panel.sharingType = .readOnly
        let canvas = SelectionCanvas(frame: NSRect(origin: .zero, size: screen.frame.size))
        canvas.setAccessibilityElement(true)
        canvas.setAccessibilityRole(.group)
        canvas.setAccessibilityLabel("拖选完整实战棋盘网格")
        canvas.completion = { [weak self, weak panel] rect in
            guard let self else { return }
            guard let rect, let panel else {
                panel?.orderOut(nil)
                self.window = nil
                completion(nil)
                return
            }
            let bottomLeft = panel.convertPoint(toScreen: rect.origin)
            let topRight = panel.convertPoint(toScreen: NSPoint(x: rect.maxX, y: rect.maxY))
            let maxY = NSScreen.screens.map { NSMaxY($0.frame) }.max() ?? screen.frame.maxY
            let quad: [[Double]] = [
                [Double(bottomLeft.x), Double(maxY - topRight.y)],
                [Double(topRight.x), Double(maxY - topRight.y)],
                [Double(topRight.x), Double(maxY - bottomLeft.y)],
                [Double(bottomLeft.x), Double(maxY - bottomLeft.y)],
            ]

            // The previous implementation invoked completion while the dark
            // selection canvas was still on screen. The automatic baseline
            // capture could therefore analyze the overlay instead of the
            // selected board. Remove it first, then allow WindowServer one
            // compositor cycle before starting screen capture.
            panel.orderOut(nil)
            self.window = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.30) {
                completion(quad)
            }
        }
        panel.contentView = canvas
        window = panel
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(canvas)
    }
}

private final class ScreenMarkerView: NSView {
    var label = ""
    var latest = false
    var diameter: CGFloat = 24

    private static let captionFont = NSFont.systemFont(ofSize: 11, weight: .semibold)

    static func caption(_ label: String, latest: Bool) -> String {
        latest ? "最新 \(label)" : "AI \(label)"
    }

    static func preferredSize(label: String, latest: Bool, diameter: CGFloat) -> NSSize {
        let text = caption(label, latest: latest)
        let textSize = text.size(withAttributes: [.font: captionFont])
        let circleArea = diameter + 8
        return NSSize(
            width: max(circleArea, ceil(textSize.width) + 10),
            height: circleArea + ceil(textSize.height) + 7
        )
    }

    override func draw(_ dirtyRect: NSRect) {
        let color = latest ? NSColor.systemOrange : NSColor.systemMint
        let lineWidth: CGFloat = 2.2
        let circleArea = diameter + 8
        let ringRect = NSRect(
            x: (bounds.width - diameter) / 2,
            y: 4,
            width: diameter,
            height: diameter
        ).insetBy(dx: lineWidth / 2, dy: lineWidth / 2)
        let ring = NSBezierPath(ovalIn: ringRect)
        ring.lineWidth = lineWidth
        let dash = max(3, diameter * 0.15)
        let gap = max(2, diameter * 0.10)
        ring.setLineDash([dash, gap], count: 2, phase: 0)
        color.withAlphaComponent(0.96).setStroke()
        ring.stroke()

        let caption = Self.caption(label, latest: latest)
        let attributes: [NSAttributedString.Key: Any] = [
            .font: Self.captionFont,
            .foregroundColor: NSColor.white,
        ]
        let textSize = caption.size(withAttributes: attributes)
        let badgeRect = NSRect(
            x: (bounds.width - textSize.width - 10) / 2,
            y: circleArea + 2,
            width: textSize.width + 10,
            height: textSize.height + 4
        )
        NSColor.black.withAlphaComponent(0.78).setFill()
        NSBezierPath(roundedRect: badgeRect, xRadius: 4, yRadius: 4).fill()
        caption.draw(
            at: NSPoint(x: badgeRect.minX + 5, y: badgeRect.minY + 2),
            withAttributes: attributes
        )
    }
}

private final class ScreenTrackingFrameView: NSView {
    var points: [NSPoint] = []
    var confidence = 0.0
    var mode = "calibrating"
    var label = ""
    var dashPhase: CGFloat = 0

    override func draw(_ dirtyRect: NSRect) {
        guard points.count == 4 else { return }
        let color: NSColor
        if mode == "recovering" || mode == "degraded" || mode == "fallback" {
            color = .systemOrange
        } else if mode == "recovered" || mode == "reanchored" || confidence >= 0.58 {
            color = .systemGreen
        } else {
            color = .systemCyan
        }

        let glow = NSBezierPath()
        glow.move(to: points[0])
        for point in points.dropFirst() { glow.line(to: point) }
        glow.close()
        glow.lineWidth = 6
        color.withAlphaComponent(0.18).setStroke()
        glow.stroke()

        let frame = NSBezierPath()
        frame.move(to: points[0])
        for point in points.dropFirst() { frame.line(to: point) }
        frame.close()
        frame.lineWidth = 2.5
        frame.setLineDash([10, 6], count: 2, phase: dashPhase)
        color.setStroke()
        frame.stroke()

        for point in points {
            let handle = NSRect(x: point.x - 4, y: point.y - 4, width: 8, height: 8)
            color.withAlphaComponent(0.92).setFill()
            NSBezierPath(ovalIn: handle).fill()
        }

        let percentage = confidence > 0 ? " · \(Int((confidence * 100).rounded()))%" : ""
        let caption = "● \(label)\(percentage)"
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12, weight: .semibold),
            .foregroundColor: NSColor.white,
            .backgroundColor: NSColor.black.withAlphaComponent(0.78),
        ]
        let captionSize = caption.size(withAttributes: attributes)
        let topLeft = points.min { lhs, rhs in
            lhs.y == rhs.y ? lhs.x < rhs.x : lhs.y > rhs.y
        } ?? points[0]
        let captionPoint = NSPoint(
            x: max(4, min(bounds.maxX - captionSize.width - 4, topLeft.x)),
            y: max(4, min(bounds.maxY - captionSize.height - 4, topLeft.y + 8))
        )
        caption.draw(at: captionPoint, withAttributes: attributes)
    }
}

@MainActor
private final class ScreenAssistOverlayController {
    private var suggestionPanel: NSPanel?
    private var latestPanel: NSPanel?
    private var trackingPanel: NSPanel?

    func showSuggestion(at point: NSPoint, label: String, diameter: CGFloat) {
        suggestionPanel = show(
            panel: suggestionPanel,
            at: point,
            label: label,
            latest: false,
            diameter: diameter
        )
    }

    func showLatest(at point: NSPoint, label: String, diameter: CGFloat) {
        latestPanel = show(
            panel: latestPanel,
            at: point,
            label: label,
            latest: true,
            diameter: diameter
        )
    }

    func showTrackingFrame(points: [NSPoint], confidence: Double, mode: String, label: String) {
        guard points.count == 4 else {
            hideTrackingFrame()
            return
        }
        let padding: CGFloat = 34
        let minX = points.map(\.x).min() ?? 0
        let maxX = points.map(\.x).max() ?? 0
        let minY = points.map(\.y).min() ?? 0
        let maxY = points.map(\.y).max() ?? 0
        let panelFrame = NSRect(
            x: minX - padding,
            y: minY - padding,
            width: max(80, maxX - minX + padding * 2),
            height: max(80, maxY - minY + padding * 2)
        )
        let panel = trackingPanel ?? makePanel()
        let translatedPoints = points.map {
            NSPoint(x: $0.x - panelFrame.minX, y: $0.y - panelFrame.minY)
        }
        if let view = panel.contentView as? ScreenTrackingFrameView,
           approximatelyEqual(panel.frame, panelFrame, tolerance: 1.25),
           approximatelyEqual(view.points, translatedPoints, tolerance: 1.25),
           Int((view.confidence * 20).rounded()) == Int((confidence * 20).rounded()),
           view.mode == mode,
           view.label == label {
            // The panel is already at screen-saver level and does not hide on
            // deactivation. Re-ordering and repainting the full board-sized
            // transparent window on every 120 ms heartbeat consumed a full
            // CPU core even while both board and AI were idle.
            return
        }
        let view = (panel.contentView as? ScreenTrackingFrameView)
            ?? ScreenTrackingFrameView(frame: NSRect(origin: .zero, size: panelFrame.size))
        view.frame = NSRect(origin: .zero, size: panelFrame.size)
        view.points = translatedPoints
        view.confidence = confidence
        view.mode = mode
        view.label = label
        view.dashPhase -= 3
        view.needsDisplay = true
        panel.contentView = view
        panel.setFrame(panelFrame, display: true)
        panel.orderFrontRegardless()
        trackingPanel = panel
    }

    func hideSuggestion() { suggestionPanel?.orderOut(nil) }
    func hideLatest() { latestPanel?.orderOut(nil) }
    func hideTrackingFrame() { trackingPanel?.orderOut(nil) }
    func hideAll() { hideSuggestion(); hideLatest(); hideTrackingFrame() }

    private func show(
        panel existing: NSPanel?,
        at point: NSPoint,
        label: String,
        latest: Bool,
        diameter: CGFloat
    ) -> NSPanel {
        let panel = existing ?? makePanel()
        let markerDiameter = min(56, max(14, diameter))
        let panelSize = ScreenMarkerView.preferredSize(
            label: label,
            latest: latest,
            diameter: markerDiameter
        )
        let circleArea = markerDiameter + 8
        let panelFrame = NSRect(
            x: point.x - panelSize.width / 2,
            y: point.y - circleArea / 2,
            width: panelSize.width,
            height: panelSize.height
        )
        if let view = panel.contentView as? ScreenMarkerView,
           view.label == label,
           view.latest == latest,
           abs(view.diameter - markerDiameter) < 0.5,
           approximatelyEqual(panel.frame, panelFrame) {
            return panel
        }
        let view = (panel.contentView as? ScreenMarkerView)
            ?? ScreenMarkerView(frame: NSRect(origin: .zero, size: panelFrame.size))
        view.frame = NSRect(origin: .zero, size: panelFrame.size)
        view.label = label
        view.latest = latest
        view.diameter = markerDiameter
        view.needsDisplay = true
        panel.contentView = view
        panel.setFrame(panelFrame, display: true)
        panel.orderFrontRegardless()
        return panel
    }

    private func approximatelyEqual(
        _ lhs: NSRect,
        _ rhs: NSRect,
        tolerance: CGFloat = 0.5
    ) -> Bool {
        abs(lhs.origin.x - rhs.origin.x) < tolerance
            && abs(lhs.origin.y - rhs.origin.y) < tolerance
            && abs(lhs.size.width - rhs.size.width) < tolerance
            && abs(lhs.size.height - rhs.size.height) < tolerance
    }

    private func approximatelyEqual(
        _ lhs: [NSPoint],
        _ rhs: [NSPoint],
        tolerance: CGFloat = 0.5
    ) -> Bool {
        lhs.count == rhs.count && zip(lhs, rhs).allSatisfy { first, second in
            abs(first.x - second.x) < tolerance && abs(first.y - second.y) < tolerance
        }
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 32, height: 32),
                            styleMask: .borderless, backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        // NSPanel hides on app deactivation by default. These overlays must
        // remain visible above the actual Go client while QiDao is inactive.
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.isReleasedWhenClosed = false
        panel.animationBehavior = .none
        panel.level = .screenSaver
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .fullScreenAuxiliary,
            .stationary,
            .ignoresCycle,
        ]
        panel.sharingType = .none
        return panel
    }
}
