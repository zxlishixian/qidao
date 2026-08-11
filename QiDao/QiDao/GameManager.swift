import Foundation
import qidao_coreFFI
import Combine

@MainActor
class GameManager: ObservableObject {
    @Published var internalState = GameState()
    @Published var gameState = GameState()

    private var game: Game
    private var nodeMap: [String: SgfNode] = [:]

    init(initialSize: Int) {
        let (game, size) = Self.makeGame(requestedSize: initialSize)
        self.game = game
        self.internalState.boardSize = size
        self.internalState.metadata.size = UInt32(size)
        syncState(rebuildTree: true)
    }

    func getGame() -> Game { game }
    func setGame(_ newGame: Game) {
        self.game = newGame
        self.internalState.isSizeLocked = false
        syncState(rebuildTree: true)
    }

    func placeStone(x: Int, y: Int, color: StoneColor) throws -> Int {
        guard AITrustBoundary.isValidBoardCoordinate(
            x: x,
            y: y,
            boardSize: internalState.boardSize
        ) else {
            throw NSError(
                domain: "QiDao.GameManager",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "落子坐标超出棋盘"]
            )
        }
        let currentBoard = game.getBoard()
        let oldStoneCount = countStones(on: currentBoard)

        try game.placeStone(x: UInt32(x), y: UInt32(y), color: color)

        let newBoard = game.getBoard()
        let newStoneCount = countStones(on: newBoard)

        let captures = oldStoneCount + 1 - newStoneCount
        syncState(rebuildTree: true)
        return captures
    }

    func goBack() -> Bool {
        if game.goBack() {
            syncState()
            return true
        }
        return false
    }

    func goForward(index: Int = 0) -> Int? {
        let currentBoard = game.getBoard()
        let oldStoneCount = countStones(on: currentBoard)
        if game.goForward(index: UInt32(index)) {
            let newBoard = game.getBoard()
            let newStoneCount = countStones(on: newBoard)
            let captures = oldStoneCount + 1 - newStoneCount
            syncState()
            return captures
        }
        return nil
    }

    func jumpToMove(_ target: Int) {
        guard let target = AITrustBoundary.validatedMoveNumber(
            target,
            maximum: Int(game.getMaxMoveCount())
        ) else { return }
        game.jumpToMoveNumber(target: UInt32(target))
        syncState()
    }

    func jumpToNode(id: String) {
        if let node = nodeMap[id] {
            game.jumpToNode(target: node)
            syncState()
        }
    }

    func deleteCurrentBranch() -> Bool {
        if game.deleteCurrentBranch() {
            syncState(rebuildTree: true)
            return true
        }
        return false
    }

    func reset(size: Int) {
        self.game = Self.makeGame(requestedSize: size).game
        self.internalState.isSizeLocked = false
        syncState(rebuildTree: true)
    }

    private static func makeGame(requestedSize: Int) -> (game: Game, size: Int) {
        guard let size = AITrustBoundary.supportedBoardSize(requestedSize) else {
            print("Unsupported board size \(requestedSize); falling back to 19x19.")
            return (try! Game(size: 19), 19)
        }
        do {
            return (try Game(size: UInt32(size)), size)
        } catch {
            print("Game creation failed for \(size)x\(size): \(error). Falling back to 19x19.")
            // Rust validates 19 as a supported board size.
            return (try! Game(size: 19), 19)
        }
    }

    func updateMetadata(_ newMetadata: GameMetadata) {
        game.setMetadata(metadata: newMetadata)
        syncState(rebuildTree: true)
    }

    func syncState(rebuildTree: Bool = false) {
        var newState = internalState
        newState.board = self.game.getBoard()
        newState.nextColor = self.game.getNextColor()
        newState.initialColor = self.game.getInitialColor()
        newState.moveCount = Int(self.game.getMoveCount())
        newState.maxMoveCount = Int(self.game.getMaxMoveCount())
        newState.metadata = self.game.getMetadata()
        newState.currentNodeId = self.game.getCurrentNode().getId()
        newState.nodeComment = self.game.getComment()
        newState.sgf = self.game.getCurrentStateSgf()

        newState.boardSize = Int(newState.metadata.size)
        let stoneCount = countStones(on: newState.board)
        newState.isSizeLocked = newState.moveCount > 0 || newState.maxMoveCount > 0 || stoneCount > 0

        if let last = self.game.getLastMove(),
           let value = last.values.first,
           let coordinate = AITrustBoundary.parseSgfCoordinate(value, boardSize: newState.boardSize) {
            newState.lastMove = coordinate
        } else {
            newState.lastMove = nil
        }

        newState.moveNumbers = [:]
        let pathMoves = self.game.getCurrentPathMoves()
        for (index, moveProp) in pathMoves.enumerated() {
            if let value = moveProp.values.first,
               let coordinate = AITrustBoundary.parseSgfCoordinate(value, boardSize: newState.boardSize) {
                newState.moveNumbers["\(coordinate.x),\(coordinate.y)"] = index + 1
            }
        }

        newState.marks = []
        let currentProps = self.game.getCurrentNode().getProperties()
        for prop in currentProps {
            if ["TR", "CR", "SQ", "MA"].contains(prop.identifier) {
                for val in prop.values {
                    if let coordinate = AITrustBoundary.parseSgfCoordinate(val, boardSize: newState.boardSize) {
                        newState.marks.append(BoardMark(
                            x: coordinate.x,
                            y: coordinate.y,
                            type: prop.identifier,
                            label: nil
                        ))
                    }
                }
            } else if prop.identifier == "LB" {
                for val in prop.values {
                    let parts = val.split(separator: ":")
                    if parts.count == 2,
                       let value = parts.first,
                       let coordinate = AITrustBoundary.parseSgfCoordinate(
                           String(value),
                           boardSize: newState.boardSize
                       ) {
                        let label = String(parts[1])
                        newState.marks.append(BoardMark(
                            x: coordinate.x,
                            y: coordinate.y,
                            type: "LB",
                            label: label
                        ))
                    }
                }
            }
        }

        let children = self.game.getCurrentNode().getChildren()
        let variationChildren = children.count > 1 ? children : []
        newState.variations = variationChildren.enumerated().map { (index, node) in
            let props = node.getProperties()
            let moveProp = props.first { $0.identifier == "B" || $0.identifier == "W" }
            var vx: Int? = nil
            var vy: Int? = nil
            let moveText: String
            if let prop = moveProp,
               let value = prop.values.first,
               let coordinate = AITrustBoundary.parseSgfCoordinate(value, boardSize: newState.boardSize) {
                vx = coordinate.x
                vy = coordinate.y
                moveText = "\(prop.identifier) (\(coordinate.x), \(coordinate.y))"
            } else {
                moveText = "Node \(index + 1)"
            }
            return Variation(id: index, moveText: moveText, x: vx, y: vy)
        }

        if rebuildTree {
            let tree = self.rebuildTreeInternal()
            newState.treeNodes = tree.nodes
            newState.treeEdges = tree.edges
        }

        self.internalState = newState
        // GameManager is @MainActor, so deferring this publication adds no
        // thread safety. It only leaves views reading the new internal board
        // without receiving a matching change notification until some later
        // UI action happens. Publish the exact same snapshot immediately.
        self.gameState = newState
    }

    private func rebuildTreeInternal() -> (nodes: [TreeVisualNode], edges: [TreeVisualEdge]) {
        nodeMap = [:]
        var nodes: [TreeVisualNode] = []
        var edges: [TreeVisualEdge] = []

        let root = game.getRootNode()
        var nextXAtDepth: [Int: Int] = [:]

        func traverse(node: SgfNode, depth: Int, xOffset: Int, parentPos: CGPoint?) {
            let id = node.getId()
            nodeMap[id] = node

            let x = CGFloat(xOffset)
            let y = CGFloat(depth)
            let currentPos = CGPoint(x: x, y: y)

            let props = node.getProperties()
            var color: StoneColor? = nil
            if props.contains(where: { $0.identifier == "B" }) {
                color = .black
            } else if props.contains(where: { $0.identifier == "W" }) {
                color = .white
            }

            nodes.append(TreeVisualNode(id: id, x: x, y: y, color: color))

            if let parent = parentPos {
                edges.append(TreeVisualEdge(id: "\(id)-edge", from: parent, to: currentPos))
            }

            let children = node.getChildren()
            let currentX = xOffset
            for (index, child) in children.enumerated() {
                let childX = (index == 0) ? currentX : (nextXAtDepth[depth + 1] ?? currentX + 1)
                nextXAtDepth[depth + 1] = max(nextXAtDepth[depth + 1] ?? 0, childX + 1)
                traverse(node: child, depth: depth + 1, xOffset: childX, parentPos: currentPos)
            }
        }

        traverse(node: root, depth: 0, xOffset: 0, parentPos: nil)
        return (nodes, edges)
    }

    private func countStones(on board: Board) -> Int {
        var count = 0
        let size = board.getSize()
        for y in 0..<size {
            for x in 0..<size {
                if board.getStone(x: x, y: y) != nil {
                    count += 1
                }
            }
        }
        return count
    }
}
