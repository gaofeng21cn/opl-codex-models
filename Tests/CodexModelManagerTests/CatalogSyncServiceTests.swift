@testable import CodexModelCore
import Foundation
import XCTest

final class CatalogSyncServiceTests: XCTestCase {
    func testSyncMergesCustomModelsAndBecomesNoChange() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let runtime = directory.appendingPathComponent("codex")
        let bundled = #"{"models":[{"slug":"gpt-official","display_name":"Official","description":"Official","visibility":"hide","priority":4,"context_window":200000,"max_context_window":800000,"input_modalities":["text"]}]}"#
        let script = """
        #!/bin/sh
        if [ "$1" = "--version" ]; then
          echo "codex-test 1.0.0"
        else
          if [ -z "$CODEX_HOME" ] || [ -e "$CODEX_HOME/config.toml" ]; then
            echo "bundled catalog read inherited user configuration" >&2
            exit 20
          fi
          echo '\(bundled)'
        fi
        """
        try Data(script.utf8).write(to: runtime)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: runtime.path)

        let custom = directory.appendingPathComponent("custom.json")
        let customObject: [String: Any] = [
            "models": [[
                "slug": "vendor-model",
                "display_name": "Vendor",
                "description": "Vendor",
                "priority": 1000,
                "context_window": 128_000,
                "max_context_window": 128_000,
                "input_modalities": ["text"],
                "supports_reasoning_summaries": true
            ]]
        ]
        try JSONSerialization.data(withJSONObject: customObject).write(to: custom)
        let paths = makePaths(directory: directory, runtime: runtime, custom: custom)
        let service = CatalogSyncService(paths: paths)

        XCTAssertEqual(try service.syncAndAppendLog().status, "updated")
        XCTAssertEqual(try service.syncAndAppendLog().status, "no_change")
        let root = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: paths.mergedCatalog)) as? [String: Any]
        )
        let models = try XCTUnwrap(root["models"] as? [[String: Any]])
        XCTAssertEqual(models.map { $0["slug"] as? String }, ["gpt-official", "vendor-model"])
        XCTAssertEqual(models[1]["priority"] as? Int, 5)
        XCTAssertEqual(models[0]["supports_parallel_tool_calls"] as? Bool, true)
        XCTAssertEqual(models[1]["supports_parallel_tool_calls"] as? Bool, true)
        XCTAssertEqual(models[0]["visibility"] as? String, "list")
        XCTAssertEqual(models[0]["max_context_window"] as? Int, 800_000)
        XCTAssertEqual(models[1]["visibility"] as? String, "hide")
        let unchangedSource = try JSONSerialization.jsonObject(with: Data(contentsOf: custom)) as? NSDictionary
        XCTAssertEqual(unchangedSource, customObject as NSDictionary)
        XCTAssertEqual(
            try String(contentsOf: paths.syncLog, encoding: .utf8)
                .split(separator: "\n").count,
            2
        )
    }

    func testCodexConfigUpdatePreservesOtherTopLevelAndProfileValues() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let configuration = directory.appendingPathComponent("config.toml")
        let original = """
        model = "gpt-5"
        model_catalog_json = "/old/models.json"

        [profiles.work]
        model_catalog_json = "/profile/models.json"
        """
        try Data(original.utf8).write(to: configuration)
        let catalog = directory.appendingPathComponent("new models.json")
        let backups = directory.appendingPathComponent("Backups", isDirectory: true)

        try CodexConfigurationEditor.setModelCatalog(
            catalog,
            in: configuration,
            backupDirectory: backups
        )
        let updated = try String(contentsOf: configuration, encoding: .utf8)
        XCTAssertTrue(updated.contains("model = \"gpt-5\""))
        XCTAssertTrue(updated.contains("model_catalog_json = \"\(catalog.path)\""))
        XCTAssertTrue(updated.contains("[profiles.work]\nmodel_catalog_json = \"/profile/models.json\""))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: backups.path).count, 1)
    }

    private func makePaths(directory: URL, runtime: URL, custom: URL) -> CatalogPaths {
        CatalogPaths(
            codexRuntime: runtime,
            customSource: custom,
            mergedCatalog: directory.appendingPathComponent("models.json"),
            syncLog: directory.appendingPathComponent("sync.jsonl"),
            errorLog: directory.appendingPathComponent("sync.error.log"),
            launchAgentPlist: directory.appendingPathComponent("sync.plist"),
            launchAgentLabel: "com.example.test-sync",
            backupDirectory: directory.appendingPathComponent("Backups", isDirectory: true),
            visibilityOverrides: ["gpt-official": .list, "vendor-model": .hide, "future-model": .list]
        )
    }
}
