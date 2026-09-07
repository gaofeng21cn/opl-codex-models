@testable import CodexModelCore
import Foundation
import XCTest
@testable import CodexModelManager

final class AppConfigurationTests: XCTestCase {
    func testLoadsTildePathsAndLaunchAgentLabel() throws {
        let home = URL(fileURLWithPath: "/tmp/codex-model-manager-home", isDirectory: true)
        let configurationURL = try makeConfiguration(
            customSourcePath: "~/.codex/custom-models.json"
        )
        defer { try? FileManager.default.removeItem(at: configurationURL.deletingLastPathComponent()) }

        let paths = try AppConfiguration.load(
            from: configurationURL,
            homeDirectory: home
        )

        XCTAssertEqual(
            paths.customSource.path,
            "/tmp/codex-model-manager-home/.codex/custom-models.json"
        )
        XCTAssertEqual(paths.mergedCatalog.path, "/var/tmp/models.json")
        XCTAssertEqual(paths.launchAgentLabel, "com.example.model-catalog-sync")
        XCTAssertTrue(paths.visibilityOverrides.isEmpty)
        XCTAssertEqual(
            paths.backupDirectory.path,
            "/tmp/codex-model-manager-home/.codex/backups/model-catalog"
        )
    }

    func testRejectsRelativePaths() throws {
        let configurationURL = try makeConfiguration(customSourcePath: "custom-models.json")
        defer { try? FileManager.default.removeItem(at: configurationURL.deletingLastPathComponent()) }

        XCTAssertThrowsError(try AppConfiguration.load(from: configurationURL)) { error in
            XCTAssertEqual(
                error.localizedDescription,
                "本机配置无效：customSourcePath 必须使用绝对路径或 ~/ 路径"
            )
        }
    }

    func testReportsMissingConfiguration() {
        let configurationURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathComponent("config.json")

        XCTAssertThrowsError(try AppConfiguration.load(from: configurationURL)) { error in
            XCTAssertTrue(error.localizedDescription.contains("尚未找到本机配置"))
        }
    }

    private func makeConfiguration(customSourcePath: String) throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent("config.json")
        let object: [String: Any] = [
            "backupDirectoryPath": "~/.codex/backups/model-catalog",
            "codexRuntimePath": "/usr/bin/true",
            "customSourcePath": customSourcePath,
            "errorLogPath": "/var/tmp/model-catalog-sync.error.log",
            "launchAgentLabel": "com.example.model-catalog-sync",
            "launchAgentPlistPath": "/var/tmp/com.example.model-catalog-sync.plist",
            "mergedCatalogPath": "/var/tmp/models.json",
            "syncLogPath": "/var/tmp/model-catalog-sync.log"
        ]
        let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted])
        try data.write(to: url, options: .atomic)
        return url
    }
}
