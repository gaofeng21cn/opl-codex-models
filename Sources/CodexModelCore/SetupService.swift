import Foundation

public struct SetupResult: Sendable {
    public let paths: CatalogPaths
    public let syncStatus: String
}

public struct SetupService {
    public init() {}

    public func apply(
        configuration: AppConfiguration,
        configurationURL: URL = AppConfiguration.defaultURL,
        helperURL: URL,
        updateCodexConfiguration: Bool = true,
        installDailySync: Bool = true,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) throws -> SetupResult {
        let paths = try configuration.resolved(homeDirectory: homeDirectory)
        guard FileManager.default.isExecutableFile(atPath: helperURL.path) else {
            throw CoreError.setupFailed("应用内的同步组件不可执行：\(helperURL.path)")
        }
        guard !helperURL.standardizedFileURL.path.hasPrefix("/Volumes/") else {
            throw CoreError.setupFailed("请先将应用移入“应用程序”文件夹，再完成首次设置。")
        }
        let syncService = CatalogSyncService(paths: paths)
        try syncService.ensureInitialFiles()
        try syncService.clearErrorLog()
        let syncResult = try syncService.syncAndAppendLog()
        try configuration.save(to: configurationURL)

        if updateCodexConfiguration {
            try CodexConfigurationEditor.setModelCatalog(
                paths.mergedCatalog,
                in: paths.codexConfiguration,
                backupDirectory: paths.backupDirectory
            )
        }
        if installDailySync {
            try LaunchAgentInstaller.install(
                paths: paths,
                helperURL: helperURL,
                configurationURL: configurationURL,
                homeDirectory: homeDirectory
            )
        }
        return SetupResult(paths: paths, syncStatus: syncResult.status)
    }
}

public enum CodexConfigurationEditor {
    public static func setModelCatalog(
        _ catalogURL: URL,
        in configurationURL: URL,
        backupDirectory: URL
    ) throws {
        let fileManager = FileManager.default
        try fileManager.createDirectory(
            at: configurationURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let original = (try? String(contentsOf: configurationURL, encoding: .utf8)) ?? ""
        let assignment = "model_catalog_json = \"\(tomlEscaped(catalogURL.path))\""
        let lines = original.components(separatedBy: "\n")
        var output: [String] = []
        var insideTopLevel = true
        var replaced = false
        let keyPattern = try NSRegularExpression(pattern: #"^\s*model_catalog_json\s*="#)
        let tablePattern = try NSRegularExpression(pattern: #"^\s*\["#)

        for line in lines {
            let range = NSRange(line.startIndex..<line.endIndex, in: line)
            if tablePattern.firstMatch(in: line, range: range) != nil {
                insideTopLevel = false
            }
            if insideTopLevel, keyPattern.firstMatch(in: line, range: range) != nil {
                if !replaced {
                    output.append(assignment)
                    replaced = true
                }
                continue
            }
            output.append(line)
        }
        if !replaced {
            if output == [""] {
                output = [assignment, ""]
            } else {
                output.insert(contentsOf: [assignment, ""], at: 0)
            }
        }
        let updated = output.joined(separator: "\n")
        guard updated != original else { return }

        if fileManager.fileExists(atPath: configurationURL.path) {
            try fileManager.createDirectory(at: backupDirectory, withIntermediateDirectories: true)
            let backup = backupDirectory.appendingPathComponent(
                "config.toml.\(timestamp()).\(UUID().uuidString).backup"
            )
            try fileManager.copyItem(at: configurationURL, to: backup)
        }
        try Data(updated.utf8).write(to: configurationURL, options: .atomic)
        try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: configurationURL.path)
    }

    private static func tomlEscaped(_ value: String) -> String {
        value.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    private static func timestamp() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        return formatter.string(from: Date())
    }
}

public enum LaunchAgentInstaller {
    public static func install(
        paths: CatalogPaths,
        helperURL: URL,
        configurationURL: URL,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) throws {
        let fileManager = FileManager.default
        for directory in [
            paths.launchAgentPlist.deletingLastPathComponent(),
            paths.syncLog.deletingLastPathComponent(),
            paths.errorLog.deletingLastPathComponent()
        ] {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        for log in [paths.syncLog, paths.errorLog] where !fileManager.fileExists(atPath: log.path) {
            try Data().write(to: log, options: .atomic)
        }

        let plist = propertyList(
            paths: paths,
            helperURL: helperURL,
            configurationURL: configurationURL,
            homeDirectory: homeDirectory
        )
        let data = try PropertyListSerialization.data(
            fromPropertyList: plist,
            format: .xml,
            options: 0
        )
        let target = "gui/\(getuid())"
        _ = try? ProcessRunner.run(
            URL(fileURLWithPath: "/bin/launchctl"),
            arguments: ["bootout", target, paths.launchAgentPlist.path]
        )
        try data.write(to: paths.launchAgentPlist, options: .atomic)
        let result = try ProcessRunner.run(
            URL(fileURLWithPath: "/bin/launchctl"),
            arguments: ["bootstrap", target, paths.launchAgentPlist.path]
        )
        guard result.exitCode == 0 else {
            let detail = result.standardError.trimmingCharacters(in: .whitespacesAndNewlines)
            throw CoreError.setupFailed(
                detail.isEmpty ? "无法安装每日同步任务，退出码 \(result.exitCode)。" : "无法安装每日同步任务：\(detail)"
            )
        }
    }

    static func propertyList(
        paths: CatalogPaths,
        helperURL: URL,
        configurationURL: URL,
        homeDirectory: URL
    ) -> [String: Any] {
        let launchPath = [
            homeDirectory.appendingPathComponent(".local/bin", isDirectory: true).path,
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin"
        ].joined(separator: ":")
        return [
            "Label": paths.launchAgentLabel,
            "ProcessType": "Background",
            "ProgramArguments": [helperURL.path, "--config", configurationURL.path],
            "EnvironmentVariables": [
                // launchd starts agents with only the system PATH. Node-based
                // Codex entry points cannot resolve `node` without the user's
                // standard package-manager locations.
                "PATH": launchPath
            ],
            "RunAtLoad": false,
            "StandardErrorPath": paths.errorLog.path,
            "StandardOutPath": paths.syncLog.path,
            "StartInterval": 86_400,
            "ThrottleInterval": 60,
            "WatchPaths": [paths.codexRuntime.path]
        ]
    }
}
