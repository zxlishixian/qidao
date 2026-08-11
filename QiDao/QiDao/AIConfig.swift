import Foundation
import Combine

struct EngineProfile: Identifiable, Codable, Equatable {
    var id: UUID
    var name: String
    var path: String
    var model: String
    var config: String
    var extraArgs: String

    init(id: UUID = UUID(), name: String, path: String, model: String = "", config: String = "", extraArgs: String = "") {
        self.id = id
        self.name = name
        self.path = path
        self.model = model
        self.config = config
        self.extraArgs = extraArgs
    }

    static var `default`: EngineProfile {
        let fileManager = FileManager.default
        let sourceRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let resourceRoot = Bundle.main.resourceURL

        let pathEntries = (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":")
            .map { URL(fileURLWithPath: String($0)).appendingPathComponent("katago").path }
        let executableCandidates = pathEntries + [
            "/opt/homebrew/bin/katago",
            "/usr/local/bin/katago",
            NSHomeDirectory() + "/.homebrew/bin/katago",
        ]
        let executable = executableCandidates.first(where: fileManager.isExecutableFile(atPath:))
            ?? "/opt/homebrew/bin/katago"

        var modelCandidates = [
            sourceRoot.appendingPathComponent("katago/default_model.bin.gz").path,
            NSHomeDirectory() + "/Library/Application Support/QiDao/default_model.bin.gz",
        ]
        var configCandidates = [
            sourceRoot.appendingPathComponent("katago/analysis.cfg").path,
        ]
        if let resourceRoot {
            modelCandidates.insert(resourceRoot.appendingPathComponent("katago/default_model.bin.gz").path, at: 0)
            configCandidates.insert(resourceRoot.appendingPathComponent("katago/analysis.cfg").path, at: 0)
        }

        return EngineProfile(
            name: "KataGo (QiDao)",
            path: executable,
            model: modelCandidates.first(where: fileManager.fileExists(atPath:)) ?? modelCandidates[0],
            config: configCandidates.first(where: fileManager.fileExists(atPath:)) ?? configCandidates[0],
            extraArgs: ""
        )
    }
}

struct AnalysisSettings: Codable, Equatable {
    var maxVisits: Int? = 1000
    var fullScanMaxVisits: Int? = 40
    var maxTime: Double? = nil
    var iterativeDeepening: Bool = true
    var reportDuringSearchEvery: Double? = 1.0
    var includePolicy: Bool = true
    var advancedParams: [String: String] = [:]

    var effectiveFullScanMaxVisits: Int {
        fullScanMaxVisits ?? 40
    }
}

enum WinRatePerspective: String, Codable, CaseIterable {
    case black = "Black"
    case current = "Current Player"

    var localized: String { self.rawValue.localized }
}

struct DisplaySettings: Codable, Equatable {
    var maxCandidates: Int = 20
    var showOwnership: Bool = true
    var showWinRateGraph: Bool = true
    var blunderThreshold: Double = 0.15
    var overlayWinRatePerspective: WinRatePerspective = .current
}

struct AIConfig: Codable {
    var currentProfileId: UUID?
    var profiles: [EngineProfile]
    var analysis: AnalysisSettings
    var display: DisplaySettings

    static var `default`: AIConfig {
        let defaultProfile = EngineProfile.default
        return AIConfig(
            currentProfileId: defaultProfile.id,
            profiles: [defaultProfile],
            analysis: AnalysisSettings(),
            display: DisplaySettings()
        )
    }
}

class ConfigManager: ObservableObject {
    static let shared = ConfigManager()

    @Published var config: AIConfig

    private let configKey = "QiDaoAppConfig"

    private init() {
        if let data = UserDefaults.standard.data(forKey: configKey),
           var decoded = try? JSONDecoder().decode(AIConfig.self, from: data) {
            let fallback = EngineProfile.default
            if let profileID = decoded.currentProfileId,
               let index = decoded.profiles.firstIndex(where: { $0.id == profileID }) {
                let fileManager = FileManager.default
                if !fileManager.isExecutableFile(atPath: decoded.profiles[index].path) {
                    decoded.profiles[index].path = fallback.path
                }
                if !fileManager.fileExists(atPath: decoded.profiles[index].model) {
                    decoded.profiles[index].model = fallback.model
                }
                if !fileManager.fileExists(atPath: decoded.profiles[index].config) {
                    decoded.profiles[index].config = fallback.config
                }
            }
            self.config = decoded
        } else {
            self.config = AIConfig.default
        }
    }

    func save() {
        if let encoded = try? JSONEncoder().encode(config) {
            UserDefaults.standard.set(encoded, forKey: configKey)
        }
    }

    var currentProfile: EngineProfile {
        config.profiles.first { $0.id == config.currentProfileId } ?? EngineProfile.default
    }
}
