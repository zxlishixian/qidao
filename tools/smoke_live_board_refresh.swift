import Combine
import Foundation
import AppKit
import SwiftUI
import qidao_coreFFI

private final class LivePresentationProbeView: NSView {
    private(set) var drawCount = 0

    override func draw(_ dirtyRect: NSRect) {
        drawCount += 1
        super.draw(dirtyRect)
    }
}

@main
struct LiveBoardRefreshSmoke {
    @MainActor
    static func main() {
        let viewModel = BoardViewModel()
        let initialRevision = viewModel.boardRevision
        var notifications = 0
        let subscription = viewModel.objectWillChange.sink {
            notifications += 1
        }

        do {
            _ = try viewModel.gameManager.placeStone(x: 3, y: 3, color: .black)
        } catch {
            fatalError("Unable to install smoke-test move: \(error)")
        }

        guard viewModel.board.getStone(x: 3, y: 3) == .black else {
            fatalError("Published BoardViewModel snapshot did not contain the new stone")
        }
        guard viewModel.displayedStone(x: 3, y: 3) == .black else {
            fatalError("Value-backed display snapshot did not contain the new stone")
        }
        guard viewModel.moveCount == 1 else {
            fatalError("Published move count remained at \(viewModel.moveCount)")
        }
        guard viewModel.boardRevision > initialRevision else {
            fatalError("Board revision did not advance")
        }
        guard notifications > 0 else {
            fatalError("BoardViewModel emitted no SwiftUI invalidation")
        }

        // Screen recognition sometimes has to replace the complete position
        // after correcting one or more earlier intersections. Exercise that
        // exact publish path as well as the ordinary one-move path above.
        let revisionBeforeRebuild = viewModel.boardRevision
        viewModel.gameManager.reset(size: 19)
        let rebuiltGame = viewModel.gameManager.getGame()
        rebuiltGame.addStone(x: 15, y: 3, color: .black)
        rebuiltGame.addStone(x: 14, y: 4, color: .white)
        rebuiltGame.setNextPlayer(color: .black)
        viewModel.gameManager.syncState(rebuildTree: true)

        guard viewModel.board.getStone(x: 15, y: 3) == .black,
              viewModel.board.getStone(x: 14, y: 4) == .white else {
            fatalError("Published BoardViewModel snapshot did not contain rebuilt position")
        }
        guard viewModel.displayedStone(x: 15, y: 3) == .black,
              viewModel.displayedStone(x: 14, y: 4) == .white else {
            fatalError("Value-backed display snapshot did not contain rebuilt position")
        }
        guard viewModel.nextColor == .black else {
            fatalError("Published next player was not refreshed after rebuilt position")
        }
        guard viewModel.boardRevision > revisionBeforeRebuild else {
            fatalError("Board revision did not advance after rebuilt position")
        }

        // The live position path normally applies the detected move through
        // qidao-core. Verify that its published snapshot contains both sides
        // of a capture transition: the new stone and the removed dead group.
        viewModel.gameManager.reset(size: 9)
        let captureGame = viewModel.gameManager.getGame()
        captureGame.addStone(x: 1, y: 1, color: .white)
        captureGame.addStone(x: 1, y: 0, color: .black)
        captureGame.addStone(x: 0, y: 1, color: .black)
        captureGame.addStone(x: 1, y: 2, color: .black)
        captureGame.setNextPlayer(color: .black)
        viewModel.gameManager.syncState(rebuildTree: true)
        do {
            _ = try viewModel.gameManager.placeStone(x: 2, y: 1, color: .black)
        } catch {
            fatalError("Unable to install capture smoke-test move: \(error)")
        }
        guard viewModel.board.getStone(x: 2, y: 1) == .black,
              viewModel.board.getStone(x: 1, y: 1) == nil else {
            fatalError("Published qidao-core position did not remove captured group")
        }
        guard viewModel.displayedStone(x: 2, y: 1) == .black,
              viewModel.displayedStone(x: 1, y: 1) == nil else {
            fatalError("Value-backed display snapshot did not publish capture removal")
        }

        // Exercise the actual screen-assist reconciliation entry point across
        // consecutive messages. This catches regressions where recognition is
        // correct but the recognized position never reaches qidao-core.
        var firstPosition = Array(repeating: Array(repeating: 0, count: 9), count: 9)
        firstPosition[3][3] = 1
        viewModel.gameManager.reset(size: 9)
        viewModel.applyScreenPosition(
            ScreenBoardPosition(
                board: firstPosition,
                lastMove: ScreenMove(x: 3, y: 3, color: 1, vertex: "D6", isPass: false),
                moveNumber: 1,
                nextPlayer: "W",
                confidence: 0.99,
                sequence: 1,
                confirmation: "smoke"
            ),
            requestAnalysis: false
        )
        guard viewModel.displayedStone(x: 3, y: 3) == .black,
              viewModel.nextColor == .white else {
            fatalError("First recognized screen move did not reach the analysis board")
        }

        var secondPosition = firstPosition
        secondPosition[4][4] = 2
        viewModel.applyScreenPosition(
            ScreenBoardPosition(
                board: secondPosition,
                lastMove: ScreenMove(x: 4, y: 4, color: 2, vertex: "E5", isPass: false),
                moveNumber: 2,
                nextPlayer: "B",
                confidence: 0.99,
                sequence: 2,
                confirmation: "smoke"
            ),
            requestAnalysis: false
        )
        guard viewModel.displayedStone(x: 3, y: 3) == .black,
              viewModel.displayedStone(x: 4, y: 4) == .white,
              viewModel.nextColor == .black else {
            fatalError("Consecutive recognized screen move did not update automatically")
        }

        // A corrected complete snapshot must overwrite several earlier
        // mistakes immediately; it must not require a UI click or reframe.
        var correctedPosition = Array(repeating: Array(repeating: 0, count: 9), count: 9)
        correctedPosition[2][6] = 1
        correctedPosition[6][2] = 2
        viewModel.applyScreenPosition(
            ScreenBoardPosition(
                board: correctedPosition,
                lastMove: nil,
                moveNumber: 24,
                nextPlayer: "B",
                confidence: 0.92,
                sequence: 3,
                confirmation: "grid-snapshot"
            ),
            requestAnalysis: false
        )
        guard viewModel.displayedStone(x: 3, y: 3) == nil,
              viewModel.displayedStone(x: 4, y: 4) == nil,
              viewModel.displayedStone(x: 6, y: 2) == .black,
              viewModel.displayedStone(x: 2, y: 6) == .white else {
            fatalError("Corrected recognized snapshot did not replace the analysis board")
        }

        // Exercise the actual JSON-message boundary and a real, inactive
        // SwiftUI/AppKit window. This is the regression for the user-visible
        // bug where the internal board changed but was only painted after the
        // user clicked QiDao's settings button.
        let window = NSWindow(
            contentRect: NSRect(x: -1800, y: -1800, width: 520, height: 520),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        let hosting = NSHostingView(rootView: GameBoardView(viewModel: viewModel, size: 500))
        hosting.frame = NSRect(x: 0, y: 0, width: 520, height: 520)
        let presentationProbe = LivePresentationProbeView(
            frame: NSRect(x: 0, y: 0, width: 520, height: 520)
        )
        presentationProbe.addSubview(hosting)
        window.contentView = presentationProbe
        window.orderFrontRegardless()
        window.displayIfNeeded()
        let drawsBeforePosition = presentationProbe.drawCount

        let manager = viewModel.screenAssistManager
        manager.boardSize = 9
        manager.onPosition = { [weak viewModel] position in
            viewModel?.applyScreenPosition(position, requestAnalysis: false)
        }
        let emptyNine = Array(repeating: Array(repeating: 0, count: 9), count: 9)
        manager.handleVisionMessage([
            "event": "baseline",
            "board": emptyNine,
            "observedBoard": emptyNine,
            "moveNumber": 0,
            "nextPlayer": "B",
        ])
        manager.handleVisionMessage(["event": "running", "running": true])

        var protocolPosition = emptyNine
        protocolPosition[2][5] = 1
        manager.handleVisionMessage([
            "event": "position",
            "board": protocolPosition,
            "observedBoard": protocolPosition,
            "lastMove": [
                "x": 5, "y": 2, "color": 1, "vertex": "F7", "boardSize": 9, "pass": false,
            ],
            "moveNumber": 1,
            "nextPlayer": "W",
            "confidence": 0.99,
            "scanSequence": 1,
            "positionSequence": 0,
            "confirmation": "inactive-window-smoke",
        ])
        RunLoop.main.run(until: Date().addingTimeInterval(0.20))

        guard viewModel.displayedStone(x: 5, y: 2) == .black else {
            fatalError("Vision protocol position did not reach the displayed board")
        }
        guard manager.isSyncedToQiDao else {
            fatalError("Vision position was acknowledged before/without a committed UI frame")
        }
        guard presentationProbe.drawCount > drawsBeforePosition else {
            fatalError("Inactive AppKit window was not drawn before the vision position acknowledgement")
        }
        window.orderOut(nil)

        withExtendedLifetime(subscription) {}
        print(
            "Live board refresh OK: move, consecutive screen events, full-position correction and capture, revision "
                + "\(initialRevision) -> \(viewModel.boardRevision), "
                + "notifications \(notifications)"
        )
    }
}
