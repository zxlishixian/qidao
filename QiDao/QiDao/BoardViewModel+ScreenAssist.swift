import Foundation
import qidao_coreFFI

extension BoardViewModel {
    func startLiveGameAnalysis() {
        if appMode != .analysis { appMode = .analysis }
        // Start loading KataGo while the user selects the real board and the
        // vision service establishes its baseline. Previously engine startup
        // began only after the recognized position had already reached QiDao,
        // putting the two slowest startup phases strictly in series.
        if !isAnalyzing { startAnalysis() }
        screenAssistManager.startLiveGameAnalysis()
    }

    func stopLiveGameAnalysis() {
        screenAssistManager.stopService()
    }

    func applyScreenPosition(
        _ position: ScreenBoardPosition,
        requestAnalysis: Bool = true
    ) {
        guard AITrustBoundary.isValidVisionPosition(
            board: position.board,
            lastMove: position.lastMove,
            moveNumber: position.moveNumber,
            nextPlayer: position.nextPlayer,
            confidence: position.confidence,
            sequence: position.sequence
        ) else {
            screenAssistManager.reportReconciliationError("识别服务返回了无效棋盘局面")
            return
        }

        if boardSize != position.board.count {
            performReset(size: position.board.count)
        }
        if appMode != .analysis { appMode = .analysis }

        // Treat every recognized position as one transaction, including the
        // common single-move path. Without this guard GameManager's publisher
        // launched a normal AI query during placeStone(), then this method
        // immediately cancelled it and launched a second live query. The
        // duplicate engine/UI traffic could delay the board message itself.
        let wasApplyingScreenPosition = isApplyingScreenSnapshot
        isApplyingScreenSnapshot = true
        defer { isApplyingScreenSnapshot = wasApplyingScreenPosition }

        let current = screenBoardSnapshot()
        if position.sequence > 0,
           position.sequence <= screenAssistManager.appliedSequence,
           current == position.board {
            let expected: StoneColor = position.nextPlayer == "W" ? .white : .black
            if nextColor != expected {
                gameManager.getGame().setNextPlayer(color: expected)
                gameManager.syncState(rebuildTree: true)
            }
            finishLivePositionSync(position, requestAnalysis: false)
            return
        }
        if let move = position.lastMove, move.isPass {
            if moveCount < position.moveNumber {
                let color: StoneColor = move.color == 2 ? .white : .black
                try? gameManager.getGame().pass(color: color)
                gameManager.syncState(rebuildTree: true)
            }
            finishLivePositionSync(position, requestAnalysis: requestAnalysis)
            return
        }

        if current == position.board {
            let expected: StoneColor = position.nextPlayer == "W" ? .white : .black
            if nextColor != expected {
                gameManager.getGame().setNextPlayer(color: expected)
                gameManager.syncState(rebuildTree: true)
            }
            finishLivePositionSync(position, requestAnalysis: requestAnalysis)
            return
        }

        if let move = position.lastMove,
           !move.isPass,
           move.color == (nextColor == .black ? 1 : 2) {
            do {
                _ = try gameManager.placeStone(x: move.x, y: move.y, color: nextColor)
                if screenBoardSnapshot() == position.board {
                    finishLivePositionSync(position, requestAnalysis: requestAnalysis)
                    return
                }
                _ = gameManager.goBack()
            } catch {
                // A stale or corrected screen state is rebuilt below.
            }
        }

        rebuildFromObservedPosition(position)
        finishLivePositionSync(position, requestAnalysis: requestAnalysis)
    }

    private func screenBoardSnapshot() -> [[Int]] {
        boardCells
    }

    private func rebuildFromObservedPosition(_ position: ScreenBoardPosition) {
        // Resetting and then installing up to 361 setup stones publishes
        // intermediate GameManager states. Suppress their AI queries and send
        // exactly one request from finishLivePositionSync after the complete
        // snapshot is visible.
        aiManager.resetSession()
        gameManager.reset(size: position.board.count)
        let game = gameManager.getGame()
        for y in position.board.indices {
            for x in position.board[y].indices {
                if position.board[y][x] == 1 {
                    game.addStone(x: UInt32(x), y: UInt32(y), color: .black)
                } else if position.board[y][x] == 2 {
                    game.addStone(x: UInt32(x), y: UInt32(y), color: .white)
                }
            }
        }
        game.setNextPlayer(color: position.nextPlayer == "W" ? .white : .black)
        gameManager.syncState(rebuildTree: true)
    }

    private func startAnalysisForLivePositionIfNeeded() {
        if !isAnalyzing {
            startAnalysis()
        } else {
            updateAnalysis()
        }
    }

    private func finishLivePositionSync(
        _ position: ScreenBoardPosition,
        requestAnalysis: Bool = true
    ) {
        let applied = screenBoardSnapshot()
        guard applied == position.board else {
            screenAssistManager.reportReconciliationError(
                "识别局面未能写入 QiDao 分析棋盘，下一帧将自动重建"
            )
            return
        }
        // Replayed positions must refresh presentation but must not restart
        // KataGo every 400 ms while an inactive/hidden window is recovering.
        if position.sequence <= 0 {
            pendingLivePositionSequence = 0
        }
        let isNewSequence = position.sequence <= 0
            || position.sequence > pendingLivePositionSequence
        if position.sequence > 0 {
            pendingLivePositionSequence = max(
                pendingLivePositionSequence,
                position.sequence
            )
        }
        if requestAnalysis && isNewSequence {
            awaitingFirstLiveAIResult = true
            screenAssistManager.beginAIResponseTiming()
            startAnalysisForLivePositionIfNeeded()
        }
        // Start AI immediately, but keep the protocol position unacknowledged
        // until the inactive SwiftUI tree has had a chance to present it. If
        // that presentation is missed, the service replays the same sequence
        // and this idempotent path tries again without waiting for a click.
        refreshLiveWindowsIfNeeded(force: true) { [weak self] in
            guard let self, self.screenBoardSnapshot() == position.board else { return }
            self.screenAssistManager.reportQiDaoPositionApplied(
                board: applied,
                moveNumber: position.moveNumber,
                sequence: position.sequence
            )
        }
    }
}
