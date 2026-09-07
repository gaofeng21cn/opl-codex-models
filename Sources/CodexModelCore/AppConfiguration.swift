import Foundation

public struct CatalogPaths: Equatable, Sendable {
    public let codexRuntime: URL
    public let customSource: URL
    public let mergedCatalog: URL
    public let syncLog: URL
    public let errorLog: URL
    public let launchAgentPlist: URL
    public let launchAgentLabel: String
    public let backupDirectory: URL
    public let visibilityOverrides: [String: ModelVisibility]

    public init(
        codexRuntime: URL,
        customSource: URL,
        mergedCatalog: URL,
        syncLog: URL,
        errorLog: URL,
        launchAgentPlist: URL,
        launchAgentLabel: String,
        backupDirectory: URL,
        visibilityOverrides: [String: ModelVisibility] = [:]
    ) {
        self.codexRuntime = codexRuntime
        self.customSource = customSource
        self.mergedCatalog = mergedCatalog
        self.syncLog = syncLog
        self.errorLog = errorLog
        self.launchAgentPlist = launchAgentPlist
        self.launchAgentLabel = launchAgentLabel
        self.backupDirectory = backupDirectory
        self.visibilityOverrides = visibilityOverrides
    }
}

public struct AppConfiguration: Codable, Equatable, Sendable {
    public var codexRuntimePath: String?
    public var customSourcePath: String
    public var mergedCatalogPath: String
    public var syncLogPath: String
    public var errorLogPath: String
    public var launchAgentPlistPath: String
    public var launchAgentLabel: String
    public var backupDirectoryPath: String
    public var visibilityOverrides: [String: ModelVisibility]?

    public init(
        codexRuntimePath: String?,
        customSourcePath: String,
        mergedCatalogPath: String,
        syncLogPath: String,
        errorLogPath: String,
        launchAgentPlistPath: String,
        launchAgentLabel: String,
        backupDirectoryPath: String,
        visibilityOverrides: [String: ModelVisibility]? = nil
    ) {
        self.codexRuntimePath = codexRuntimePath
        self.customSourcePath = customSourcePath
        self.mergedCatalogPath = mergedCatalogPath
        self.syncLogPath = syncLogPath
        self.errorLogPath = errorLogPath
        self.launchAgentPlistPath = launchAgentPlistPath
        self.launchAgentLabel = launchAgentLabel
        self.backupDirectoryPath = backupDirectoryPath
        self.visibilityOverrides = visibilityOverrides
    }

    public static var defaultURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support", isDirectory: true)
            .appendingPathComponent("CodexModelManager", isDirectory: true)
            .appendingPathComponent("config.json")
    }

    public static func recommended(
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) throws -> AppConfiguration {
        guard CodexRuntimeLocator.find() != nil else {
            throw CoreError.runtimeNotFound
        }
        let codexDirectory = homeDirectory.appendingPathComponent(".codex", isDirectory: true)
        let supportDirectory = homeDirectory
            .appendingPathComponent("Library/Application Support", isDirectory: true)
            .appendingPathComponent("CodexModelManager", isDirectory: true)
        let logDirectory = homeDirectory
            .appendingPathComponent("Library/Logs", isDirectory: true)
            .appendingPathComponent("CodexModelManager", isDirectory: true)
        let label = "com.onepersonlab.codex-model-manager.sync"
        return AppConfiguration(
            codexRuntimePath: nil,
            customSourcePath: codexDirectory.appendingPathComponent("custom-models.json").path,
            mergedCatalogPath: codexDirectory.appendingPathComponent("models.json").path,
            syncLogPath: logDirectory.appendingPathComponent("sync.jsonl").path,
            errorLogPath: logDirectory.appendingPathComponent("sync.error.log").path,
            launchAgentPlistPath: homeDirectory
                .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
                .appendingPathComponent("\(label).plist").path,
            launchAgentLabel: label,
            backupDirectoryPath: supportDirectory.appendingPathComponent("Backups", isDirectory: true).path
        )
    }

    public static func read(from url: URL = defaultURL) throws -> AppConfiguration {
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw CoreError.configurationNotFound(url.path)
        }
        do {
            return try JSONDecoder().decode(AppConfiguration.self, from: Data(contentsOf: url))
        } catch let error as DecodingError {
            throw CoreError.invalidConfiguration(decodingDetail(error))
        } catch {
            throw CoreError.invalidConfiguration(error.localizedDescription)
        }
    }

    public static func load(
        from url: URL = defaultURL,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) throws -> CatalogPaths {
        try read(from: url).resolved(homeDirectory: homeDirectory)
    }

    public func resolved(
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) throws -> CatalogPaths {
        let label = launchAgentLabel.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !label.isEmpty else {
            throw CoreError.invalidConfiguration("launchAgentLabel 不能为空")
        }
        let runtime: URL
        if let codexRuntimePath, !codexRuntimePath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            runtime = try Self.resolve(codexRuntimePath, field: "codexRuntimePath", homeDirectory: homeDirectory)
        } else if let discovered = CodexRuntimeLocator.find() {
            runtime = discovered
        } else {
            throw CoreError.runtimeNotFound
        }

        return CatalogPaths(
            codexRuntime: runtime,
            customSource: try Self.resolve(customSourcePath, field: "customSourcePath", homeDirectory: homeDirectory),
            mergedCatalog: try Self.resolve(mergedCatalogPath, field: "mergedCatalogPath", homeDirectory: homeDirectory),
            syncLog: try Self.resolve(syncLogPath, field: "syncLogPath", homeDirectory: homeDirectory),
            errorLog: try Self.resolve(errorLogPath, field: "errorLogPath", homeDirectory: homeDirectory),
            launchAgentPlist: try Self.resolve(
                launchAgentPlistPath,
                field: "launchAgentPlistPath",
                homeDirectory: homeDirectory
            ),
            launchAgentLabel: label,
            backupDirectory: try Self.resolve(
                backupDirectoryPath,
                field: "backupDirectoryPath",
                homeDirectory: homeDirectory
            ),
            visibilityOverrides: visibilityOverrides ?? [:]
        )
    }

    public func save(to url: URL = defaultURL) throws {
        let directory = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let data = try JSONEncoder.pretty.encode(self)
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private static func resolve(_ rawValue: String, field: String, homeDirectory: URL) throws -> URL {
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            throw CoreError.invalidConfiguration("\(field) 不能为空")
        }
        let url: URL
        if value == "~" {
            url = homeDirectory
        } else if value.hasPrefix("~/") {
            url = homeDirectory.appendingPathComponent(String(value.dropFirst(2)))
        } else if NSString(string: value).isAbsolutePath {
            url = URL(fileURLWithPath: value)
        } else {
            throw CoreError.invalidConfiguration("\(field) 必须使用绝对路径或 ~/ 路径")
        }
        let standardized = url.standardizedFileURL
        guard standardized.path != "/" else {
            throw CoreError.invalidConfiguration("\(field) 不能指向文件系统根目录")
        }
        return standardized
    }

    private static func decodingDetail(_ error: DecodingError) -> String {
        switch error {
        case .keyNotFound(let key, _):
            return "缺少必填字段 \(key.stringValue)"
        case .typeMismatch(_, let context), .valueNotFound(_, let context), .dataCorrupted(let context):
            return context.debugDescription
        @unknown default:
            return error.localizedDescription
        }
    }
}

public enum CodexRuntimeLocator {
    public static func find(fileManager: FileManager = .default) -> URL? {
        discover(fileManager: fileManager).first?.url
    }

    public static func discover(fileManager: FileManager = .default) -> [CodexRuntime] {
        let candidates = [
            fileManager.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/codex").path,
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex"
        ]
        return candidates
            .map(URL.init(fileURLWithPath:))
            .filter { fileManager.isExecutableFile(atPath: $0.path) }
            .compactMap { url in
                guard let result = try? ProcessRunner.run(url, arguments: ["--version"]),
                      result.exitCode == 0 else { return nil }
                return CodexRuntime(url: url, version: result.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines))
            }
            .sorted { $0.version.compare($1.version, options: .numeric) == .orderedDescending }
    }
}

public struct CodexRuntime: Identifiable, Sendable {
    public let url: URL
    public let version: String
    public var id: String { url.path }
}

public enum ModelVisibility: String, Codable, Sendable, CaseIterable {
    case list
    case hide
}

public enum CoreError: LocalizedError {
    case configurationNotFound(String)
    case invalidConfiguration(String)
    case runtimeNotFound
    case invalidCatalog(String)
    case processFailed(String)
    case setupFailed(String)

    public var errorDescription: String? {
        switch self {
        case .configurationNotFound(let path):
            "尚未找到本机配置：\(path)"
        case .invalidConfiguration(let detail):
            "本机配置无效：\(detail)"
        case .runtimeNotFound:
            "没有找到可用的 Codex 运行时。请先安装 ChatGPT 或在设置中选择 codex 可执行文件。"
        case .invalidCatalog(let detail):
            "模型目录格式无效：\(detail)"
        case .processFailed(let detail), .setupFailed(let detail):
            detail
        }
    }
}

extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return encoder
    }
}
