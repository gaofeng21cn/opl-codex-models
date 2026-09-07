import Foundation
import TOMLKit

public struct ContextManagementSettings: Sendable {
    public let enabled: Bool
    public let provider: String

    public static var configurationURL: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex/config.toml")
    }

    public static func read(from url: URL = configurationURL) throws -> Self {
        let text = FileManager.default.fileExists(atPath: url.path)
            ? try String(contentsOf: url, encoding: .utf8) : ""
        let root = try TOMLTable(string: text)
        let features = root["features"]?.tomlValue.table
        let context = features?["context_management"]?.tomlValue.table
        return Self(
            enabled: context?["experimental_mode"]?.tomlValue.bool ?? false,
            provider: root["model_provider"]?.tomlValue.string ?? "openai"
        )
    }

    public static func setEnabled(
        _ enabled: Bool,
        in url: URL = configurationURL,
        backupDirectory: URL
    ) throws {
        let original = FileManager.default.fileExists(atPath: url.path)
            ? try String(contentsOf: url, encoding: .utf8) : ""
        let updated = try updating(original, enabled: enabled)
        guard updated != original else { return }
        try FileManager.default.createDirectory(at: backupDirectory, withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.copyItem(
                at: url, to: backupDirectory.appendingPathComponent("config.toml.\(UUID().uuidString).backup")
            )
        }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(updated.utf8).write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    static func updating(_ original: String, enabled: Bool) throws -> String {
        let expected = try TOMLTable(string: original)
        if let value = expected["features"], value.tomlValue.table == nil {
            throw CoreError.invalidConfiguration("features 必须是 TOML 表")
        }
        let features = expected["features"]?.tomlValue.table ?? TOMLTable()
        if let value = features["context_management"], value.tomlValue.table == nil {
            throw CoreError.invalidConfiguration("context_management 使用了旧格式，请先改为配置表")
        }
        let context = features["context_management"]?.tomlValue.table ?? TOMLTable()
        if context["experimental_mode"]?.tomlValue.bool == enabled { return original }
        context["experimental_mode"] = enabled
        features["context_management"] = context
        expected["features"] = features

        // Preserve comments and layout; accept a text edit only when the parser
        // proves that its entire semantic change matches the intended setting.
        let value = enabled ? "true" : "false"
        let assignment = try NSRegularExpression(pattern: #"experimental_mode["']?\s*=\s*(true|false)\b"#)
        for match in assignment.matches(in: original, range: NSRange(original.startIndex..., in: original)) {
            guard let range = Range(match.range(at: 1), in: original) else { continue }
            let candidate = original.replacingCharacters(in: range, with: value)
            if let parsed = try? TOMLTable(string: candidate), parsed == expected { return candidate }
        }
        let suffix = "\n[features.context_management]\nexperimental_mode = \(value)\n"
        let appended = original + suffix
        if let parsed = try? TOMLTable(string: appended), parsed == expected { return appended }
        let header = try NSRegularExpression(pattern: #"(?m)^\s*\[\s*features\s*\.\s*context_management\s*\][^\n]*\n"#)
        for match in header.matches(in: original, range: NSRange(original.startIndex..., in: original)) {
            guard let range = Range(match.range, in: original) else { continue }
            var candidate = original
            candidate.insert(contentsOf: "experimental_mode = \(value)\n", at: range.upperBound)
            if let parsed = try? TOMLTable(string: candidate), parsed == expected { return candidate }
        }
        throw CoreError.invalidConfiguration("无法保留当前 TOML 布局。请在配置文件中使用 [features.context_management] 配置表后重试。")
    }
}
