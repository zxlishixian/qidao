import Foundation
import Darwin
import qidao_coreFFI

@main
struct LiveAIStartupSmoke {
    private enum SmokeError: LocalizedError {
        case missingValue(String)
        case missingRequiredValue(option: String, environment: String)
        case engineNotReady
        case missingAnalysisResult

        var errorDescription: String? {
            switch self {
            case let .missingValue(option):
                return "Missing value for \(option)"
            case let .missingRequiredValue(option, environment):
                return "Provide \(option) <path> or set \(environment)"
            case .engineNotReady:
                return "KataGo transport did not become ready"
            case .missingAnalysisResult:
                return "KataGo produced no live analysis result"
            }
        }
    }

    private static func requiredValue(option: String, environment: String) throws -> String {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if let optionIndex = arguments.firstIndex(of: option) {
            let valueIndex = arguments.index(after: optionIndex)
            guard valueIndex < arguments.endIndex else {
                throw SmokeError.missingValue(option)
            }
            return arguments[valueIndex]
        }
        if let value = ProcessInfo.processInfo.environment[environment], !value.isEmpty {
            return value
        }
        throw SmokeError.missingRequiredValue(option: option, environment: environment)
    }

    @MainActor
    static func main() async {
        do {
            try await run()
        } catch {
            FileHandle.standardError.write(
                Data("SMOKE FAILED: \(error.localizedDescription)\n".utf8)
            )
            exit(1)
        }
    }

    @MainActor
    private static func run() async throws {
        let executable = try requiredValue(option: "--katago", environment: "KATAGO_EXECUTABLE")
        let configPath = try requiredValue(option: "--config", environment: "KATAGO_CONFIG")
        let modelPath = try requiredValue(option: "--model", environment: "KATAGO_MODEL")
        var config = AIConfig.default
        config.analysis.maxVisits = 50
        config.analysis.reportDuringSearchEvery = 0.05

        let manager = AIManager()
        let startedAt = Date()
        manager.start(
            executable: executable,
            args: ["analysis", "-config", configPath, "-model", modelPath],
            config: config
        )

        while !manager.isEngineReady && Date().timeIntervalSince(startedAt) < 5 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        do {
            guard manager.isEngineReady else {
                throw SmokeError.engineNotReady
            }
            let transportLatency = Date().timeIntervalSince(startedAt)
            let metadata = GameMetadata(
                blackName: "", blackRank: "", whiteName: "", whiteRank: "",
                komi: 7.5, handicap: 0, result: "", date: "", event: "",
                gameName: "", place: "", size: 19
            )
            manager.updateAnalysis(
                currentNodeId: "live-smoke",
                initialStones: [],
                moves: [],
                nextPlayer: "B",
                initialPlayer: "B",
                turnNumber: 0,
                metadata: metadata,
                config: config,
                fastResponse: true
            )

            while manager.analysisResult == nil && Date().timeIntervalSince(startedAt) < 20 {
                try? await Task.sleep(nanoseconds: 20_000_000)
            }
            guard let result = manager.analysisResult,
                  let best = result.moveInfos.first?.moveStr else {
                throw SmokeError.missingAnalysisResult
            }
            let firstResultLatency = Date().timeIntervalSince(startedAt)
            await stopAndDrain(manager)
            print(
                String(
                    format: "Live AI startup OK: transport %.3fs, first result %.3fs, best %@, visits %u",
                    transportLatency,
                    firstResultLatency,
                    best,
                    result.rootInfo.visits
                )
            )
        } catch {
            await stopAndDrain(manager)
            throw error
        }
    }

    @MainActor
    private static func stopAndDrain(_ manager: AIManager) async {
        manager.stop()
        // Core shutdown has bounded graceful and forced-reap stages of up to two seconds each.
        try? await Task.sleep(nanoseconds: 5_000_000_000)
    }
}
