import Foundation
import Darwin
import qidao_coreFFI

@main
struct LiveAIPrioritySmoke {
    private enum SmokeError: LocalizedError {
        case engineNotReady
        case fullScanNotSubmitted
        case missingLiveResult
        case missingQueryOrder
        case fullScanNotTerminated
        case ordinaryFullScanNotSubmitted
        case missingOrdinaryQuery
        case ordinaryFullScanNotTerminated
        case wrongOrdinaryPriority
        case slowOrdinaryReportInterval
        case missingOrdinaryResult

        var errorDescription: String? {
            switch self {
            case .engineNotReady:
                return "Fake Analysis Engine did not become ready"
            case .fullScanNotSubmitted:
                return "Full-game scan was not submitted"
            case .missingLiveResult:
                return "Live analysis produced no candidate within five seconds"
            case .missingQueryOrder:
                return "Unable to find fullscan and qidao queries in the protocol log"
            case .fullScanNotTerminated:
                return "Live analysis did not terminate the active full scan before querying"
            case .ordinaryFullScanNotSubmitted:
                return "Ordinary analysis setup did not submit a full-game scan"
            case .missingOrdinaryQuery:
                return "Ordinary analysis did not submit a current-position query"
            case .ordinaryFullScanNotTerminated:
                return "Ordinary analysis did not terminate the active full scan before querying"
            case .wrongOrdinaryPriority:
                return "Ordinary analysis did not use interactive priority"
            case .slowOrdinaryReportInterval:
                return "Ordinary analysis did not request a prompt partial result"
            case .missingOrdinaryResult:
                return "Ordinary analysis produced no candidate within five seconds"
            }
        }
    }

    private static let fakeEngine = #"""
import json
import sys

log_path = sys.argv[1]
full_scan_active = False
for line in sys.stdin:
    message = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(message, separators=(",", ":")) + "\n")
        log.flush()
    query_id = message.get("id", "")
    action = message.get("action")
    if action == "terminate" and message.get("terminateId", "").startswith("fullscan-"):
        full_scan_active = False
        continue
    if action is None and query_id.startswith("fullscan-"):
        full_scan_active = True
        continue
    if action is None and query_id.startswith("qidao-") and not full_scan_active:
        turn = message.get("analyzeTurns", [0])[0]
        result = {
            "id": query_id,
            "turnNumber": turn,
            "isDuringSearch": True,
            "noResults": False,
            "rootInfo": {"winrate": 0.62, "scoreLead": 2.0, "visits": 8},
            "moveInfos": [{
                "move": "D4", "visits": 8, "winrate": 0.62,
                "scoreLead": 2.0, "pv": ["D4"],
            }],
        }
        print(json.dumps(result, separators=(",", ":")), flush=True)
"""#

    @MainActor
    static func main() async {
        let logURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("qidao-live-ai-\(UUID().uuidString).jsonl")
        let manager = AIManager()
        do {
            try Data().write(to: logURL)
            try await run(manager: manager, logURL: logURL)
            manager.stop()
            try? await Task.sleep(nanoseconds: 500_000_000)
            try? FileManager.default.removeItem(at: logURL)
            print("Live AI priority OK: full scan terminated before query, first result under five seconds")
        } catch {
            manager.stop()
            try? await Task.sleep(nanoseconds: 500_000_000)
            try? FileManager.default.removeItem(at: logURL)
            FileHandle.standardError.write(Data("SMOKE FAILED: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }

    @MainActor
    private static func run(manager: AIManager, logURL: URL) async throws {
        var config = AIConfig.default
        config.analysis.maxVisits = 50
        config.analysis.reportDuringSearchEvery = 0.05
        config.display.showWinRateGraph = true
        manager.start(
            executable: "/usr/bin/python3",
            args: ["-u", "-c", fakeEngine, logURL.path],
            config: config
        )

        let readyDeadline = Date().addingTimeInterval(3)
        while !manager.isEngineReady, Date() < readyDeadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard manager.isEngineReady else { throw SmokeError.engineNotReady }

        let metadata = GameMetadata(
            blackName: "", blackRank: "", whiteName: "", whiteRank: "",
            komi: 7.5, handicap: 0, result: "", date: "", event: "",
            gameName: "", place: "", size: 19
        )
        let fullScanID = "fullscan-\(manager.analysisSessionId)"
        manager.startFullGameAnalysis(
            mainLineMoves: [["B", "D4"]],
            initialStones: [],
            metadata: metadata,
            config: config,
            initialPlayer: "B"
        )

        let scanDeadline = Date().addingTimeInterval(3)
        while !messages(at: logURL).contains(where: {
            $0["id"] as? String == fullScanID && $0["action"] == nil
        }), Date() < scanDeadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard messages(at: logURL).contains(where: {
            $0["id"] as? String == fullScanID && $0["action"] == nil
        }) else {
            throw SmokeError.fullScanNotSubmitted
        }

        manager.stopFullGameAnalysis()
        let liveStartedAt = Date()
        manager.updateAnalysis(
            currentNodeId: "live-priority",
            initialStones: [],
            moves: [["B", "D4"]],
            nextPlayer: "W",
            initialPlayer: "B",
            turnNumber: 1,
            metadata: metadata,
            config: config,
            fastResponse: true
        )

        let resultDeadline = Date().addingTimeInterval(5)
        while !(manager.analysisResult?.id.hasSuffix("-live-priority") == true
                && manager.analysisResult?.moveInfos.isEmpty == false),
              Date() < resultDeadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard manager.analysisResult?.moveInfos.first?.moveStr == "D4",
              Date().timeIntervalSince(liveStartedAt) < 5 else {
            throw SmokeError.missingLiveResult
        }

        let protocolMessages = messages(at: logURL)
        guard let fullScanIndex = protocolMessages.lastIndex(where: {
            $0["id"] as? String == fullScanID && $0["action"] == nil
        }),
        let liveQueryIndex = protocolMessages.indices.first(where: { index in
            index > fullScanIndex
                && (protocolMessages[index]["id"] as? String)?.hasPrefix("qidao-") == true
                && protocolMessages[index]["action"] == nil
        }) else {
            throw SmokeError.missingQueryOrder
        }
        let terminatedBeforeLiveQuery = protocolMessages.indices.contains { index in
            index > fullScanIndex
                && index < liveQueryIndex
                && protocolMessages[index]["action"] as? String == "terminate"
                && protocolMessages[index]["terminateId"] as? String == fullScanID
        }
        guard terminatedBeforeLiveQuery else { throw SmokeError.fullScanNotTerminated }

        let ordinaryStartIndex = protocolMessages.count
        manager.analysisResult = nil
        manager.startFullGameAnalysis(
            mainLineMoves: [["B", "D4"], ["W", "Q16"]],
            initialStones: [],
            metadata: metadata,
            config: config,
            initialPlayer: "B"
        )

        let ordinaryScanDeadline = Date().addingTimeInterval(3)
        while !messages(at: logURL).dropFirst(ordinaryStartIndex).contains(where: { message in
            (message["id"] as? String) == fullScanID
                && message["action"] == nil
        }), Date() < ordinaryScanDeadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }

        let ordinaryScanMessages = messages(at: logURL)
        guard ordinaryScanMessages.indices.contains(where: { index in
            index >= ordinaryStartIndex
                && (ordinaryScanMessages[index]["id"] as? String) == fullScanID
                && ordinaryScanMessages[index]["action"] == nil
        }) else {
            throw SmokeError.ordinaryFullScanNotSubmitted
        }

        let ordinaryStartedAt = Date()
        manager.updateAnalysis(
            currentNodeId: "ordinary-priority",
            initialStones: [],
            moves: [["B", "D4"], ["W", "Q16"]],
            nextPlayer: "B",
            initialPlayer: "B",
            turnNumber: 2,
            metadata: metadata,
            config: config,
            fastResponse: false
        )

        let ordinaryQueryDeadline = Date().addingTimeInterval(3)
        while !messages(at: logURL).contains(where: {
            ($0["id"] as? String)?.hasSuffix("-ordinary-priority") == true
                && $0["action"] == nil
        }), Date() < ordinaryQueryDeadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }

        let ordinaryMessages = messages(at: logURL)
        guard let ordinaryFullScanIndex = ordinaryMessages.indices.last(where: { index in
            index >= ordinaryStartIndex
                && (ordinaryMessages[index]["id"] as? String) == fullScanID
                && ordinaryMessages[index]["action"] == nil
        }),
        let ordinaryQueryIndex = ordinaryMessages.indices.first(where: { index in
            index > ordinaryFullScanIndex
                && (ordinaryMessages[index]["id"] as? String)?.hasSuffix("-ordinary-priority") == true
                && ordinaryMessages[index]["action"] == nil
        }) else {
            throw SmokeError.missingOrdinaryQuery
        }

        let ordinaryTerminatedBeforeQuery = ordinaryMessages.indices.contains { index in
            index > ordinaryFullScanIndex
                && index < ordinaryQueryIndex
                && ordinaryMessages[index]["action"] as? String == "terminate"
                && ordinaryMessages[index]["terminateId"] as? String == fullScanID
        }
        guard ordinaryTerminatedBeforeQuery else {
            throw SmokeError.ordinaryFullScanNotTerminated
        }
        guard ordinaryMessages[ordinaryQueryIndex]["priority"] as? Int == 30 else {
            throw SmokeError.wrongOrdinaryPriority
        }
        guard let reportInterval = ordinaryMessages[ordinaryQueryIndex]["reportDuringSearchEvery"] as? Double,
              reportInterval <= 0.25 else {
            throw SmokeError.slowOrdinaryReportInterval
        }

        let ordinaryResultDeadline = Date().addingTimeInterval(5)
        while !(manager.analysisResult?.id.hasSuffix("-ordinary-priority") == true
                && manager.analysisResult?.moveInfos.isEmpty == false),
              Date() < ordinaryResultDeadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard manager.analysisResult?.moveInfos.first?.moveStr == "D4",
              Date().timeIntervalSince(ordinaryStartedAt) < 5 else {
            throw SmokeError.missingOrdinaryResult
        }
    }

    private static func messages(at url: URL) -> [[String: Any]] {
        guard let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { line in
            guard let data = String(line).data(using: .utf8),
                  let value = try? JSONSerialization.jsonObject(with: data) else { return nil }
            return value as? [String: Any]
        }
    }
}
