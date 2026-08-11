import Foundation
import CoreFoundation

enum AIMoveDecision: Equatable {
    case move(x: Int, y: Int)
    case pass
    case failure(String)
    case cancelled
}

struct ScreenMove: Equatable {
    let x: Int
    let y: Int
    let color: Int
    let vertex: String
    let isPass: Bool
}

enum VisionSnapshotEvent: String {
    case baseline
    case position
    case undo
}

struct ValidatedVisionSnapshot: Equatable {
    let board: [[Int]]
    let observedBoard: [[Int]]
    let lastMove: ScreenMove?
    let moveNumber: Int
    let nextPlayer: String
    let confidence: Double
    let scanSequence: Int
    let sequence: Int
    let confirmation: String
}

/// Incremental newline framer for the vision JSON protocol. The producer caps
/// output at 16 KiB; the consumer permits 64 KiB for protocol headroom while
/// ensuring an unterminated or complete hostile frame cannot grow storage.
struct VisionJSONLineFramer {
    enum Error: Swift.Error {
        case lineTooLong
    }

    static let maximumLineBytes = 64 * 1024

    private let maxLineBytes: Int
    private var pending = Data()

    init(maxLineBytes: Int = maximumLineBytes) {
        self.maxLineBytes = max(0, maxLineBytes)
    }

    var bufferedByteCount: Int { pending.count }

    mutating func append(_ data: Data) throws -> [Data] {
        var lines: [Data] = []
        var start = data.startIndex

        while start < data.endIndex {
            if let newline = data[start...].firstIndex(of: 0x0A) {
                let fragment = data[start..<newline]
                guard pending.count <= maxLineBytes - fragment.count else {
                    pending.removeAll()
                    throw Error.lineTooLong
                }
                pending.append(contentsOf: fragment)
                lines.append(pending)
                pending.removeAll(keepingCapacity: true)
                start = data.index(after: newline)
            } else {
                let fragment = data[start...]
                guard pending.count <= maxLineBytes - fragment.count else {
                    pending.removeAll()
                    throw Error.lineTooLong
                }
                pending.append(contentsOf: fragment)
                break
            }
        }

        return lines
    }

    mutating func reset() {
        pending.removeAll()
    }
}

struct VisionProcessToken: Equatable {
    fileprivate let generation: UInt64
}

/// Invalidates stdout work that was queued by a previous vision child.
struct VisionProcessGeneration {
    private var generation: UInt64 = 0

    mutating func begin() -> VisionProcessToken {
        generation &+= 1
        return VisionProcessToken(generation: generation)
    }

    mutating func invalidate() {
        generation &+= 1
    }

    func accepts(_ token: VisionProcessToken) -> Bool {
        token.generation == generation
    }
}

enum AITrustBoundary {
    private static let columns = Array("ABCDEFGHJKLMNOPQRST")
    private static let maximumSgfBytes = 8 * 1024 * 1024

    static func parseMove(_ move: String, boardSize: Int) -> AIMoveDecision {
        guard supportedBoardSize(boardSize) != nil else {
            return .failure("不支持的棋盘尺寸：\(boardSize)")
        }
        if move == "PASS" { return .pass }
        guard !move.isEmpty else { return .failure("AI 未返回落子") }
        guard move.count >= 2,
              let column = move.first,
              let x = columns.firstIndex(of: column),
              let row = Int(move.dropFirst()) else {
            return .failure("AI 返回无效坐标：\(move)")
        }

        let y = boardSize - row
        guard (0..<boardSize).contains(x), (0..<boardSize).contains(y) else {
            return .failure("AI 返回越界坐标：\(move)")
        }
        return .move(x: x, y: y)
    }

    static func validatedMoveNumber(_ value: Int, maximum: Int) -> Int? {
        guard maximum >= 0 else { return nil }
        return (0...maximum).contains(value) ? value : nil
    }

    static func candidateCount(_ value: Int) -> Int {
        min(100, max(1, value))
    }

    static func supportedBoardSize(_ value: Int) -> Int? {
        [9, 13, 19].contains(value) ? value : nil
    }

    static func parseSgfCoordinate(_ coordinate: String, boardSize: Int) -> (x: Int, y: Int)? {
        guard supportedBoardSize(boardSize) != nil else { return nil }
        let bytes = Array(coordinate.utf8)
        guard bytes.count == 2,
              bytes.allSatisfy({ (UInt8(ascii: "a")...UInt8(ascii: "z")).contains($0) }) else {
            return nil
        }
        let x = Int(bytes[0] - UInt8(ascii: "a"))
        let y = Int(bytes[1] - UInt8(ascii: "a"))
        guard x < boardSize, y < boardSize else { return nil }
        return (x, y)
    }

    static func isSgfByteCountAllowed(_ byteCount: Int) -> Bool {
        (0...maximumSgfBytes).contains(byteCount)
    }

    static func shouldContinuePolling(after errorDescription: String) -> Bool {
        errorDescription.contains("Timeout")
    }

    static func shouldRestoreReady(
        isThinking: Bool,
        isEngineStarted: Bool,
        isEngineReady: Bool
    ) -> Bool {
        isThinking && isEngineStarted && isEngineReady
    }

    static func prioritizingCancellation(
        _ decision: AIMoveDecision,
        isCancelled: Bool
    ) -> AIMoveDecision {
        isCancelled ? .cancelled : decision
    }

    static func isValidBoardCoordinate(x: Int, y: Int, boardSize: Int) -> Bool {
        supportedBoardSize(boardSize) != nil
            && (0..<boardSize).contains(x)
            && (0..<boardSize).contains(y)
    }

    static func validatedVisionMove(_ value: Any?, boardSize: Int) -> ScreenMove? {
        guard supportedBoardSize(boardSize) != nil,
              let object = value as? [String: Any],
              let x = strictInt(object["x"]),
              let y = strictInt(object["y"]),
              let color = strictInt(object["color"]),
              color == 1 || color == 2,
              strictInt(object["boardSize"]) == boardSize,
              let suppliedVertex = object["vertex"] as? String,
              let isPass = object["pass"] as? Bool else { return nil }

        let vertex: String
        if isPass {
            guard x == -1, y == -1, suppliedVertex == "PASS" else { return nil }
            vertex = "PASS"
        } else {
            guard isValidBoardCoordinate(x: x, y: y, boardSize: boardSize) else { return nil }
            vertex = "\(columns[x])\(boardSize - y)"
            guard suppliedVertex == vertex else { return nil }
        }

        return ScreenMove(x: x, y: y, color: color, vertex: vertex, isPass: isPass)
    }

    static func validatedVisionBoard(
        _ value: Any?,
        boardSize: Int,
        allowsUnknown: Bool
    ) -> [[Int]]? {
        guard supportedBoardSize(boardSize) != nil,
              let rows = value as? [Any],
              rows.count == boardSize else { return nil }

        let allowed = allowsUnknown ? 0...3 : 0...2
        var board: [[Int]] = []
        board.reserveCapacity(boardSize)
        for rowValue in rows {
            guard let values = rowValue as? [Any], values.count == boardSize else { return nil }
            var row: [Int] = []
            row.reserveCapacity(boardSize)
            for value in values {
                guard let cell = strictInt(value), allowed.contains(cell) else { return nil }
                row.append(cell)
            }
            board.append(row)
        }
        return board
    }

    static func validatedVisionSnapshot(
        _ message: [String: Any],
        event: VisionSnapshotEvent,
        boardSize: Int
    ) -> ValidatedVisionSnapshot? {
        guard message["event"] as? String == event.rawValue,
              let board = validatedVisionBoard(
                message["board"],
                boardSize: boardSize,
                allowsUnknown: false
              ),
              let observedBoard = validatedVisionBoard(
                message["observedBoard"],
                boardSize: boardSize,
                allowsUnknown: true
              ),
              let moveNumber = nonnegativeInt(message["moveNumber"]),
              let nextPlayer = message["nextPlayer"] as? String,
              nextPlayer == "B" || nextPlayer == "W",
              hasValidNonnegativeCounters(message) else { return nil }

        let moveValue = event == .undo ? message["removed"] : message["lastMove"]
        let lastMove: ScreenMove?
        if moveValue == nil || moveValue is NSNull {
            lastMove = nil
        } else {
            guard let parsed = validatedVisionMove(moveValue, boardSize: boardSize) else { return nil }
            lastMove = parsed
        }

        switch event {
        case .baseline:
            return ValidatedVisionSnapshot(
                board: board,
                observedBoard: observedBoard,
                lastMove: lastMove,
                moveNumber: moveNumber,
                nextPlayer: nextPlayer,
                confidence: 1,
                scanSequence: 0,
                sequence: 0,
                confirmation: "baseline"
            )
        case .position:
            guard let confidence = finiteDouble(message["confidence"]),
                  (0...1).contains(confidence),
                  let scanSequence = nonnegativeInt(message["scanSequence"]),
                  let sequence = nonnegativeInt(message["positionSequence"]) else { return nil }
            let confirmation = message["confirmation"] as? String ?? "—"
            return ValidatedVisionSnapshot(
                board: board,
                observedBoard: observedBoard,
                lastMove: lastMove,
                moveNumber: moveNumber,
                nextPlayer: nextPlayer,
                confidence: confidence,
                scanSequence: scanSequence,
                sequence: sequence,
                confirmation: confirmation
            )
        case .undo:
            return ValidatedVisionSnapshot(
                board: board,
                observedBoard: observedBoard,
                lastMove: nil,
                moveNumber: moveNumber,
                nextPlayer: nextPlayer,
                confidence: 1,
                scanSequence: 0,
                sequence: 0,
                confirmation: "undo"
            )
        }
    }

    static func isValidVisionPosition(
        board: [[Int]],
        lastMove: ScreenMove?,
        moveNumber: Int,
        nextPlayer: String,
        confidence: Double,
        sequence: Int
    ) -> Bool {
        let boardSize = board.count
        guard validatedVisionBoard(board, boardSize: boardSize, allowsUnknown: false) != nil,
              moveNumber >= 0,
              sequence >= 0,
              nextPlayer == "B" || nextPlayer == "W",
              confidence.isFinite,
              (0...1).contains(confidence) else { return false }
        guard let lastMove else { return true }
        if lastMove.isPass {
            return lastMove.x == -1 && lastMove.y == -1
                && (lastMove.color == 1 || lastMove.color == 2)
                && lastMove.vertex == "PASS"
        }
        guard isValidBoardCoordinate(x: lastMove.x, y: lastMove.y, boardSize: boardSize),
              lastMove.color == 1 || lastMove.color == 2 else { return false }
        return lastMove.vertex == "\(columns[lastMove.x])\(boardSize - lastMove.y)"
    }

    static func isValidVisionScan(_ message: [String: Any], boardSize: Int) -> Bool {
        guard message["event"] as? String == "scan",
              nonnegativeInt(message["scanSequence"]) != nil,
              nonnegativeInt(message["moveNumber"]) != nil,
              let nextPlayer = message["nextPlayer"] as? String,
              nextPlayer == "B" || nextPlayer == "W",
              hasValidNonnegativeCounters(message) else { return false }

        if let board = message["observedBoard"],
           validatedVisionBoard(board, boardSize: boardSize, allowsUnknown: true) == nil {
            return false
        }
        if let board = message["confirmedBoard"],
           validatedVisionBoard(board, boardSize: boardSize, allowsUnknown: false) == nil {
            return false
        }
        if let candidate = message["candidate"], !(candidate is NSNull),
           validatedVisionMove(candidate, boardSize: boardSize) == nil {
            return false
        }
        if let previews = message["hoverPreviews"] as? [Any],
           previews.contains(where: { validatedVisionMove($0, boardSize: boardSize) == nil }) {
            return false
        }
        return true
    }

    private static let nonnegativeCounterKeys = [
        "moveNumber",
        "scanSequence",
        "positionSequence",
        "trackingFailures",
        "stableFrames",
        "reconciliationDifferences",
        "unexpectedStones",
        "unknownPoints",
        "snapshotStableFrames",
    ]

    private static func hasValidNonnegativeCounters(_ message: [String: Any]) -> Bool {
        nonnegativeCounterKeys.allSatisfy { key in
            guard let value = message[key] else { return true }
            return nonnegativeInt(value) != nil
        }
    }

    private static func nonnegativeInt(_ value: Any?) -> Int? {
        guard let value = strictInt(value), value >= 0 else { return nil }
        return value
    }

    private static func strictInt(_ value: Any?) -> Int? {
        if let number = value as? NSNumber,
           CFGetTypeID(number) == CFBooleanGetTypeID() {
            return nil
        }
        return value as? Int
    }

    private static func finiteDouble(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let result = number.doubleValue
        return result.isFinite ? result : nil
    }
}
