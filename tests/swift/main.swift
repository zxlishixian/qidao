import Foundation

assert(AITrustBoundary.parseMove("Q16", boardSize: 19) == .move(x: 15, y: 3))
assert(AITrustBoundary.parseMove("PASS", boardSize: 19) == .pass)
assert(AITrustBoundary.parseMove("pass", boardSize: 19) == .failure("AI 返回无效坐标：pass"))
assert(AITrustBoundary.parseMove("A20", boardSize: 19) == .failure("AI 返回越界坐标：A20"))
assert(AITrustBoundary.parseMove("I10", boardSize: 19) == .failure("AI 返回无效坐标：I10"))
assert(AITrustBoundary.parseMove("", boardSize: 19) == .failure("AI 未返回落子"))
assert(AITrustBoundary.parseMove("A1", boardSize: 20) == .failure("不支持的棋盘尺寸：20"))

let sgfOrigin = AITrustBoundary.parseSgfCoordinate("aa", boardSize: 19)
assert(sgfOrigin?.x == 0 && sgfOrigin?.y == 0)
let sgfNineEdge = AITrustBoundary.parseSgfCoordinate("ii", boardSize: 9)
assert(sgfNineEdge?.x == 8 && sgfNineEdge?.y == 8)
let sgfThirteenEdge = AITrustBoundary.parseSgfCoordinate("mm", boardSize: 13)
assert(sgfThirteenEdge?.x == 12 && sgfThirteenEdge?.y == 12)
let sgfNineteenEdge = AITrustBoundary.parseSgfCoordinate("ss", boardSize: 19)
assert(sgfNineteenEdge?.x == 18 && sgfNineteenEdge?.y == 18)
assert(AITrustBoundary.parseSgfCoordinate("é", boardSize: 19) == nil)
assert(AITrustBoundary.parseSgfCoordinate("AA", boardSize: 19) == nil)
assert(AITrustBoundary.parseSgfCoordinate("a!", boardSize: 19) == nil)
assert(AITrustBoundary.parseSgfCoordinate("a", boardSize: 19) == nil)
assert(AITrustBoundary.parseSgfCoordinate("aaa", boardSize: 19) == nil)
assert(AITrustBoundary.parseSgfCoordinate("aa", boardSize: 10) == nil)
assert(AITrustBoundary.parseSgfCoordinate("ja", boardSize: 9) == nil)
assert(AITrustBoundary.parseSgfCoordinate("an", boardSize: 13) == nil)
assert(AITrustBoundary.isSgfByteCountAllowed(8 * 1024 * 1024))
assert(!AITrustBoundary.isSgfByteCountAllowed(8 * 1024 * 1024 + 1))
assert(!AITrustBoundary.isSgfByteCountAllowed(-1))

assert(AITrustBoundary.validatedMoveNumber(-1, maximum: 100) == nil)
assert(AITrustBoundary.validatedMoveNumber(0, maximum: 100) == 0)
assert(AITrustBoundary.validatedMoveNumber(100, maximum: 100) == 100)
assert(AITrustBoundary.validatedMoveNumber(101, maximum: 100) == nil)

assert(AITrustBoundary.candidateCount(-5) == 1)
assert(AITrustBoundary.candidateCount(12) == 12)
assert(AITrustBoundary.candidateCount(101) == 100)

assert(AITrustBoundary.supportedBoardSize(9) == 9)
assert(AITrustBoundary.supportedBoardSize(13) == 13)
assert(AITrustBoundary.supportedBoardSize(19) == 19)
assert(AITrustBoundary.supportedBoardSize(10) == nil)

assert(AITrustBoundary.shouldContinuePolling(after: "Parse error: Timeout"))
assert(!AITrustBoundary.shouldContinuePolling(after: "Engine closed stdout"))
assert(AITrustBoundary.shouldRestoreReady(isThinking: true, isEngineStarted: true, isEngineReady: true))
assert(!AITrustBoundary.shouldRestoreReady(isThinking: true, isEngineStarted: false, isEngineReady: false))

let successfulMove: AIMoveDecision = .move(x: 3, y: 4)
let ffiFailure: AIMoveDecision = .failure("FFI error")
assert(AITrustBoundary.prioritizingCancellation(successfulMove, isCancelled: true) == .cancelled)
assert(AITrustBoundary.prioritizingCancellation(ffiFailure, isCancelled: true) == .cancelled)
assert(AITrustBoundary.prioritizingCancellation(successfulMove, isCancelled: false) == successfulMove)
assert(AITrustBoundary.prioritizingCancellation(ffiFailure, isCancelled: false) == ffiFailure)

let visionLineLimit = 64 * 1024
var partialFramer = VisionJSONLineFramer(maxLineBytes: visionLineLimit)
do {
    _ = try partialFramer.append(Data(repeating: 0x78, count: visionLineLimit + 1))
    assertionFailure("an unterminated over-limit frame must be rejected")
} catch VisionJSONLineFramer.Error.lineTooLong {
    assert(partialFramer.bufferedByteCount == 0)
} catch {
    assertionFailure("unexpected framing error: \(error)")
}

var completeFramer = VisionJSONLineFramer(maxLineBytes: visionLineLimit)
var oversizedComplete = Data(repeating: 0x78, count: visionLineLimit + 1)
oversizedComplete.append(0x0A)
do {
    _ = try completeFramer.append(oversizedComplete)
    assertionFailure("a complete over-limit frame must be rejected")
} catch VisionJSONLineFramer.Error.lineTooLong {
    assert(completeFramer.bufferedByteCount == 0)
} catch {
    assertionFailure("unexpected framing error: \(error)")
}

var restartFramer = VisionJSONLineFramer(maxLineBytes: visionLineLimit)
_ = try restartFramer.append(Data("stale partial".utf8))
assert(restartFramer.bufferedByteCount == 13)
restartFramer.reset()
let restartedLines = try restartFramer.append(Data("{\"event\":\"ready\"}\n".utf8))
assert(restartedLines == [Data("{\"event\":\"ready\"}".utf8)])
assert(restartFramer.bufferedByteCount == 0)

func visionBoard(_ size: Int = 9, value: Int = 0) -> [[Int]] {
    Array(repeating: Array(repeating: value, count: size), count: size)
}

func movePayload(
    x: Int = 3,
    y: Int = 4,
    color: Int = 1,
    vertex: String = "D5",
    isPass: Bool = false
) -> [String: Any] {
    [
        "x": x,
        "y": y,
        "color": color,
        "vertex": vertex,
        "boardSize": 9,
        "pass": isPass,
    ]
}

func positionPayload() -> [String: Any] {
    [
        "event": "position",
        "board": visionBoard(),
        "observedBoard": visionBoard(),
        "lastMove": movePayload(),
        "moveNumber": 1,
        "nextPlayer": "W",
        "confidence": 0.95,
        "scanSequence": 2,
        "positionSequence": 1,
        "trackingFailures": 0,
        "confirmation": "temporal",
    ]
}

func baselinePayload() -> [String: Any] {
    [
        "event": "baseline",
        "board": visionBoard(),
        "observedBoard": visionBoard(),
        "moveNumber": 0,
        "nextPlayer": "B",
    ]
}

func jsonRoundTrip(_ value: [String: Any]) throws -> [String: Any] {
    let data = try JSONSerialization.data(withJSONObject: value)
    return try JSONSerialization.jsonObject(with: data) as! [String: Any]
}

func framedJSON(_ value: [String: Any]) throws -> Data {
    var data = try JSONSerialization.data(withJSONObject: value)
    data.append(0x0A)
    return data
}

// A failed child may already have queued more stdout work on the main actor.
// Those chunks must remain tied to that exact process generation, even after
// the framer is reset and a replacement child starts.
let oldVisionChild = Process()
let newVisionChild = Process()
var activeVisionChild: Process? = oldVisionChild
var visionProcessGeneration = VisionProcessGeneration()
let oldVisionToken = visionProcessGeneration.begin()
var guardedFramer = VisionJSONLineFramer(maxLineBytes: visionLineLimit)
var guardedState = "unchanged"
var guardedCallbackCount = 0
var guardedError = ""

func deliverVisionOutput(
    _ data: Data,
    from child: Process,
    token: VisionProcessToken
) {
    guard visionProcessGeneration.accepts(token), activeVisionChild === child else { return }

    do {
        for line in try guardedFramer.append(data) {
            guard let message = try JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let event = message["event"] as? String else { continue }
            guardedState = event
            guardedCallbackCount += 1
        }
    } catch VisionJSONLineFramer.Error.lineTooLong {
        visionProcessGeneration.invalidate()
        activeVisionChild = nil
        guardedFramer.reset()
        guardedError = "overflow"
    } catch {
        assertionFailure("unexpected guarded framing error: \(error)")
    }
}

deliverVisionOutput(
    Data(repeating: 0x78, count: visionLineLimit + 1),
    from: oldVisionChild,
    token: oldVisionToken
)
deliverVisionOutput(try framedJSON(baselinePayload()), from: oldVisionChild, token: oldVisionToken)
deliverVisionOutput(try framedJSON(positionPayload()), from: oldVisionChild, token: oldVisionToken)
assert(guardedError == "overflow")
assert(guardedState == "unchanged")
assert(guardedCallbackCount == 0)

activeVisionChild = newVisionChild
let newVisionToken = visionProcessGeneration.begin()
deliverVisionOutput(try framedJSON(baselinePayload()), from: oldVisionChild, token: oldVisionToken)
assert(guardedState == "unchanged")
assert(guardedCallbackCount == 0)
deliverVisionOutput(try framedJSON(positionPayload()), from: newVisionChild, token: newVisionToken)
assert(guardedState == "position")
assert(guardedCallbackCount == 1)

let validMove = AITrustBoundary.validatedVisionMove(movePayload(), boardSize: 9)
assert(validMove == ScreenMove(x: 3, y: 4, color: 1, vertex: "D5", isPass: false))
assert(AITrustBoundary.validatedVisionMove(
    movePayload(x: -1, vertex: "PASS"),
    boardSize: 9
) == nil)
assert(AITrustBoundary.validatedVisionMove(
    movePayload(x: 9, vertex: "K5"),
    boardSize: 9
) == nil)
assert(AITrustBoundary.validatedVisionMove(
    movePayload(color: 3),
    boardSize: 9
) == nil)
assert(AITrustBoundary.validatedVisionMove(
    movePayload(x: -1, y: 0, vertex: "PASS", isPass: true),
    boardSize: 9
) == nil)
assert(AITrustBoundary.validatedVisionMove(
    movePayload(x: -1, y: -1, color: 2, vertex: "PASS", isPass: true),
    boardSize: 9
) == ScreenMove(x: -1, y: -1, color: 2, vertex: "PASS", isPass: true))
assert(AITrustBoundary.validatedVisionMove(
    movePayload(vertex: "A1"),
    boardSize: 9
) == nil)

var confirmedWithUnknown = visionBoard()
confirmedWithUnknown[0][0] = 3
assert(AITrustBoundary.validatedVisionBoard(
    confirmedWithUnknown,
    boardSize: 9,
    allowsUnknown: false
) == nil)
assert(AITrustBoundary.validatedVisionBoard(
    confirmedWithUnknown,
    boardSize: 9,
    allowsUnknown: true
) == confirmedWithUnknown)

let validPosition = AITrustBoundary.validatedVisionSnapshot(
    try jsonRoundTrip(positionPayload()),
    event: .position,
    boardSize: 9
)
assert(validPosition?.moveNumber == 1)
assert(validPosition?.sequence == 1)

var appliedBoard = visionBoard(value: 2)
var callbackCount = 0
func applyVisionTransaction(_ message: [String: Any], event: VisionSnapshotEvent) {
    guard let snapshot = AITrustBoundary.validatedVisionSnapshot(
        try! jsonRoundTrip(message),
        event: event,
        boardSize: 9
    ) else { return }
    appliedBoard = snapshot.board
    callbackCount += 1
}

let untouchedBoard = appliedBoard
var malformedMessages: [([String: Any], VisionSnapshotEvent)] = []
var negativeCoordinate = positionPayload()
negativeCoordinate["lastMove"] = movePayload(x: -1, vertex: "PASS")
malformedMessages.append((negativeCoordinate, .position))
var outOfBoundsCoordinate = positionPayload()
outOfBoundsCoordinate["lastMove"] = movePayload(y: 9, vertex: "D0")
malformedMessages.append((outOfBoundsCoordinate, .position))
var invalidColor = positionPayload()
invalidColor["lastMove"] = movePayload(color: 0)
malformedMessages.append((invalidColor, .position))
var invalidPass = positionPayload()
invalidPass["lastMove"] = movePayload(x: -1, y: 0, vertex: "PASS", isPass: true)
malformedMessages.append((invalidPass, .position))
var invalidPositionCells = positionPayload()
invalidPositionCells["board"] = confirmedWithUnknown
malformedMessages.append((invalidPositionCells, .position))
var invalidNextPlayer = positionPayload()
invalidNextPlayer["nextPlayer"] = "black"
malformedMessages.append((invalidNextPlayer, .position))
var invalidMoveCounter = positionPayload()
invalidMoveCounter["moveNumber"] = -1
malformedMessages.append((invalidMoveCounter, .position))
var invalidPositionSequence = positionPayload()
invalidPositionSequence["positionSequence"] = -1
malformedMessages.append((invalidPositionSequence, .position))

var invalidBaseline: [String: Any] = [
    "event": "baseline",
    "board": confirmedWithUnknown,
    "observedBoard": visionBoard(),
    "moveNumber": 0,
    "nextPlayer": "B",
]
malformedMessages.append((invalidBaseline, .baseline))

var invalidUndo: [String: Any] = [
    "event": "undo",
    "board": visionBoard(),
    "observedBoard": visionBoard(),
    "removed": NSNull(),
    "moveNumber": -1,
    "nextPlayer": "B",
]
malformedMessages.append((invalidUndo, .undo))

for (message, event) in malformedMessages {
    applyVisionTransaction(message, event: event)
}
assert(appliedBoard == untouchedBoard)
assert(callbackCount == 0)
assert(!AITrustBoundary.isValidBoardCoordinate(x: -1, y: 0, boardSize: 9))
assert(!AITrustBoundary.isValidBoardCoordinate(x: 0, y: 9, boardSize: 9))
assert(AITrustBoundary.isValidBoardCoordinate(x: 8, y: 8, boardSize: 9))
