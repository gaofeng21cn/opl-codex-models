import CryptoKit
import Foundation

public struct CatalogSyncResult: Sendable {
    public let status: String
    public let recordData: Data
}

public struct CatalogSyncService: Sendable {
    public let paths: CatalogPaths

    public init(paths: CatalogPaths) {
        self.paths = paths
    }

    public func ensureInitialFiles() throws {
        let fileManager = FileManager.default
        for directory in [
            paths.customSource.deletingLastPathComponent(),
            paths.mergedCatalog.deletingLastPathComponent(),
            paths.syncLog.deletingLastPathComponent(),
            paths.errorLog.deletingLastPathComponent(),
            paths.backupDirectory
        ] {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        if !fileManager.fileExists(atPath: paths.customSource.path) {
            let initial: [String: Any] = [
                "schema": "codex_model_manager_custom_models.v1",
                "models": []
            ]
            try Self.prettyJSON(initial).write(to: paths.customSource, options: .atomic)
            try Self.setPrivatePermissions(paths.customSource)
        }
        for log in [paths.syncLog, paths.errorLog] where !fileManager.fileExists(atPath: log.path) {
            try Data().write(to: log, options: .atomic)
            try Self.setPrivatePermissions(log)
        }
    }

    public func sync() throws -> CatalogSyncResult {
        guard FileManager.default.isExecutableFile(atPath: paths.codexRuntime.path) else {
            throw CoreError.processFailed("Codex 运行时不可执行：\(paths.codexRuntime.path)")
        }
        try ensureInitialFiles()

        let isolatedCodexHome = FileManager.default.temporaryDirectory.appendingPathComponent(
            "codex-model-manager-\(UUID().uuidString)",
            isDirectory: true
        )
        try FileManager.default.createDirectory(at: isolatedCodexHome, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: isolatedCodexHome) }

        let bundledResult = try ProcessRunner.run(
            paths.codexRuntime,
            arguments: ["debug", "models", "--bundled"],
            environment: ["CODEX_HOME": isolatedCodexHome.path]
        )
        guard bundledResult.exitCode == 0 else {
            throw CoreError.processFailed(Self.processMessage("读取 Codex 官方模型失败", bundledResult))
        }
        let versionResult = try ProcessRunner.run(paths.codexRuntime, arguments: ["--version"])
        guard versionResult.exitCode == 0 else {
            throw CoreError.processFailed(Self.processMessage("读取 Codex 版本失败", versionResult))
        }

        let customData = try Data(contentsOf: paths.customSource)
        let bundledData = Data(bundledResult.standardOutput.utf8)
        let customRoot = try Self.rootObject(customData, name: "自定义模型源")
        var bundledRoot = try Self.rootObject(bundledData, name: "Codex 官方模型")
        guard
            let customModels = customRoot["models"] as? [[String: Any]],
            var bundledModels = bundledRoot["models"] as? [[String: Any]],
            !bundledModels.isEmpty
        else {
            throw CoreError.invalidCatalog("models 必须是数组，且官方目录不能为空")
        }

        let bundledSlugs = try Self.validatedSlugs(bundledModels, name: "官方模型")
        let customSlugs = try Self.validatedSlugs(customModels, name: "自定义模型")
        let overlap = Set(bundledSlugs).intersection(customSlugs)
        guard overlap.isEmpty else {
            throw CoreError.invalidCatalog("自定义模型与官方模型重名：\(overlap.sorted().joined(separator: ", "))")
        }
        let priorityCeiling = bundledModels.compactMap { Self.integer($0["priority"]) }.max()
        guard let priorityCeiling, bundledModels.allSatisfy({ Self.integer($0["priority"]) != nil }) else {
            throw CoreError.invalidCatalog("官方模型缺少数字 priority")
        }

        bundledModels = bundledModels.map { model in
            var copy = model
            if copy["supports_reasoning_summaries"] == nil {
                copy["supports_reasoning_summaries"] = true
            }
            if copy["supports_parallel_tool_calls"] == nil {
                copy["supports_parallel_tool_calls"] = true
            }
            if let slug = copy["slug"] as? String, let visibility = paths.visibilityOverrides[slug] {
                copy["visibility"] = visibility.rawValue
            }
            return copy
        }
        let prioritizedCustom = customModels.enumerated().map { index, model in
            var copy = model
            copy["priority"] = priorityCeiling + index + 1
            if copy["supports_reasoning_summaries"] == nil {
                copy["supports_reasoning_summaries"] = true
            }
            if copy["supports_parallel_tool_calls"] == nil {
                copy["supports_parallel_tool_calls"] = true
            }
            if let slug = copy["slug"] as? String, let visibility = paths.visibilityOverrides[slug] {
                copy["visibility"] = visibility.rawValue
            }
            return copy
        }
        bundledRoot["models"] = bundledModels + prioritizedCustom
        let desiredData = try Self.prettyJSON(bundledRoot)
        let desiredCanonical = try Self.canonicalJSON(bundledRoot)
        let desiredHash = Self.sha256(desiredCanonical)
        let customHash = Self.sha256(try Self.canonicalJSON(customRoot))

        var currentHash: String?
        if FileManager.default.fileExists(atPath: paths.mergedCatalog.path),
           let currentObject = try? Self.rootObject(Data(contentsOf: paths.mergedCatalog), name: "当前合并目录") {
            currentHash = Self.sha256(try Self.canonicalJSON(currentObject))
        }

        let status: String
        var backupPath: String?
        if currentHash == desiredHash {
            status = "no_change"
        } else {
            status = "updated"
            if FileManager.default.fileExists(atPath: paths.mergedCatalog.path) {
                try FileManager.default.createDirectory(at: paths.backupDirectory, withIntermediateDirectories: true)
                let backup = paths.backupDirectory.appendingPathComponent(
                    "models.\(Self.fileTimestamp()).\(UUID().uuidString).json"
                )
                try FileManager.default.copyItem(at: paths.mergedCatalog, to: backup)
                backupPath = backup.path
            }
            try desiredData.write(to: paths.mergedCatalog, options: .atomic)
            try Self.setPrivatePermissions(paths.mergedCatalog)
        }

        let record: [String: Any?] = [
            "recorded_at": Self.isoTimestamp(),
            "status": status,
            "app_codex": paths.codexRuntime.path,
            "app_version": versionResult.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines),
            "custom_models": paths.customSource.path,
            "custom_hash": customHash,
            "catalog": paths.mergedCatalog.path,
            "bundled_count": bundledModels.count,
            "custom_count": prioritizedCustom.count,
            "current_hash": currentHash,
            "desired_hash": desiredHash,
            "backup_path": backupPath
        ]
        let compactRecord = record.compactMapValues { $0 }
        let recordData = try JSONSerialization.data(withJSONObject: compactRecord, options: [.sortedKeys])
        return CatalogSyncResult(status: status, recordData: recordData)
    }

    public func clearErrorLog() throws {
        try ensureInitialFiles()
        let handle = try FileHandle(forWritingTo: paths.errorLog)
        defer { try? handle.close() }
        try handle.truncate(atOffset: 0)
    }

    public func syncAndAppendLog() throws -> CatalogSyncResult {
        let result = try sync()
        let handle = try FileHandle(forWritingTo: paths.syncLog)
        defer { try? handle.close() }
        try handle.seekToEnd()
        try handle.write(contentsOf: result.recordData + Data([0x0A]))
        try Self.setPrivatePermissions(paths.syncLog)
        return result
    }

    private static func rootObject(_ data: Data, name: String) throws -> [String: Any] {
        do {
            guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw CoreError.invalidCatalog("\(name) 顶层必须是 JSON 对象")
            }
            return root
        } catch let error as CoreError {
            throw error
        } catch {
            throw CoreError.invalidCatalog("\(name) 无法解析：\(error.localizedDescription)")
        }
    }

    private static func validatedSlugs(_ models: [[String: Any]], name: String) throws -> [String] {
        let slugs = models.compactMap { $0["slug"] as? String }
        guard slugs.count == models.count, slugs.allSatisfy({ !$0.isEmpty }) else {
            throw CoreError.invalidCatalog("\(name)均须包含非空 slug")
        }
        guard Set(slugs).count == slugs.count else {
            throw CoreError.invalidCatalog("\(name)包含重复 slug")
        }
        return slugs
    }

    private static func integer(_ value: Any?) -> Int? {
        if let int = value as? Int { return int }
        if let number = value as? NSNumber { return number.intValue }
        return nil
    }

    private static func prettyJSON(_ object: Any) throws -> Data {
        try JSONSerialization.data(
            withJSONObject: object,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        ) + Data([0x0A])
    }

    private static func canonicalJSON(_ object: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
    }

    private static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private static func setPrivatePermissions(_ url: URL) throws {
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private static func processMessage(_ prefix: String, _ result: ProcessResult) -> String {
        let detail = result.standardError.trimmingCharacters(in: .whitespacesAndNewlines)
        return detail.isEmpty ? "\(prefix)，退出码 \(result.exitCode)。" : "\(prefix)：\(detail)"
    }

    private static func isoTimestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }

    private static func fileTimestamp() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        return formatter.string(from: Date())
    }
}
