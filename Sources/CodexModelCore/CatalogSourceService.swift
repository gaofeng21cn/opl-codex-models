import CryptoKit
import Darwin
import Foundation

public enum CatalogCommandError: LocalizedError {
    case invalidArguments(String)
    case sourceConflict(expected: String, actual: String)

    public var code: String {
        switch self {
        case .invalidArguments: "invalid_arguments"
        case .sourceConflict: "source_conflict"
        }
    }

    public var errorDescription: String? {
        switch self {
        case .invalidArguments(let detail): detail
        case .sourceConflict: "模型源已变化；请重新读取并审阅当前版本。"
        }
    }
}

/// The GUI and machine entry point edit the same source, under the same lock.
/// Saving the source does not imply that the installed Codex catalog was synced.
public struct CatalogSourceService {
    private let customSource: URL
    private let mergedCatalog: URL
    private let backupDirectory: URL

    public init(paths: CatalogPaths) {
        customSource = paths.customSource
        mergedCatalog = paths.mergedCatalog
        backupDirectory = paths.backupDirectory
    }

    public init(configuration: AppConfiguration,
                homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser) throws {
        customSource = try AppConfiguration.resolve(configuration.customSourcePath, field: "customSourcePath", homeDirectory: homeDirectory)
        mergedCatalog = try AppConfiguration.resolve(configuration.mergedCatalogPath, field: "mergedCatalogPath", homeDirectory: homeDirectory)
        backupDirectory = try AppConfiguration.resolve(configuration.backupDirectoryPath, field: "backupDirectoryPath", homeDirectory: homeDirectory)
    }

    public func read() throws -> [String: Any] {
        let source = try Data(contentsOf: customSource)
        let catalog = try catalogData()
        return try snapshot(source: source, catalog: catalog)
    }

    public func preview(
        expectedDigest: String? = nil,
        transform: (Data, Data?) throws -> Data
    ) throws -> [String: Any] {
        let source = try Data(contentsOf: customSource)
        try checkDigest(source, expected: expectedDigest)
        let catalog = try catalogData()
        let updated = try transform(source, catalog)
        var result = try snapshot(source: source, catalog: catalog)
        result["status"] = "preview"
        result["proposed_source_digest"] = Self.digest(updated)
        result["proposed_models"] = try Self.models(updated)
        result["sync_required"] = false
        return result
    }

    public func edit(
        expectedDigest: String? = nil,
        transform: (Data, Data?) throws -> Data
    ) throws -> [String: Any] {
        let lockPath = customSource.path + ".lock"
        let fd = open(lockPath, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { flock(fd, LOCK_UN) }
        let source = try Data(contentsOf: customSource)
        try checkDigest(source, expected: expectedDigest)
        let catalog = try catalogData()
        let updated = try transform(source, catalog)
        _ = try snapshot(source: updated, catalog: catalog)
        // Also detect edits from external editors which do not participate in our lock.
        try checkDigest(Data(contentsOf: customSource), expected: Self.digest(source))
        try FileManager.default.createDirectory(at: backupDirectory, withIntermediateDirectories: true)
        let backup = backupDirectory.appendingPathComponent("custom-models.\(UUID().uuidString).json")
        try source.write(to: backup, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: backup.path)
        try updated.write(to: customSource, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: customSource.path)
        var result = try snapshot(source: Data(contentsOf: customSource), catalog: catalog)
        result["status"] = "source_saved"
        result["previous_source_digest"] = Self.digest(source)
        result["backup_path"] = backup.path
        result["sync_required"] = true
        return result
    }

    public static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    public static func slugs(_ source: Data, _ catalog: Data?) throws -> Set<String> {
        let all = try models(source) + (catalog.map { try models($0) } ?? [])
        return Set(all.compactMap { $0["slug"] as? String })
    }

    private func checkDigest(_ data: Data, expected: String?) throws {
        if let expected, Self.digest(data) != expected {
            throw CatalogCommandError.sourceConflict(expected: expected, actual: Self.digest(data))
        }
    }

    private func catalogData() throws -> Data? {
        guard FileManager.default.fileExists(atPath: mergedCatalog.path) else { return nil }
        return try Data(contentsOf: mergedCatalog)
    }

    private func snapshot(source: Data, catalog: Data?) throws -> [String: Any] {
        var result: [String: Any] = [
            "schema": "codex_models.cli.v1", "status": "read",
            "source_path": customSource.path, "source_digest": Self.digest(source),
            "models": try Self.models(source), "custom_models": try Self.models(source), "catalog_path": mergedCatalog.path
        ]
        if let catalog {
            result["catalog_digest"] = Self.digest(catalog)
            result["catalog_models"] = try Self.models(catalog)
        }
        return result
    }

    private static func models(_ data: Data) throws -> [[String: Any]] {
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let models = root["models"] as? [[String: Any]] else {
            throw CoreError.invalidCatalog("models 必须是数组")
        }
        return models
    }
}
