@testable import CodexModelCore
@testable import CodexModelManager
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

        let sourceBeforeEdit = try Data(contentsOf: custom)
        let settings = ReasoningSettings(supportedEfforts: ["low", "high", "max"], defaultEffort: "high")
        try CatalogDataService(paths: paths).updateReasoning(settings, for: "vendor-model")
        let backups = try FileManager.default.contentsOfDirectory(at: paths.backupDirectory, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix("custom-models.") }
        XCTAssertEqual(backups.count, 1)
        XCTAssertEqual(try Data(contentsOf: XCTUnwrap(backups.first)), sourceBeforeEdit)
        XCTAssertEqual(try service.syncAndAppendLog().status, "updated")
        let parsed = try CatalogParser.parseModels(customData: Data(contentsOf: custom), catalogData: Data(contentsOf: paths.mergedCatalog))
        XCTAssertEqual(parsed.last?.reasoning, settings)
        XCTAssertEqual(try service.syncAndAppendLog().status, "no_change")

        let savedSource = try Data(contentsOf: custom)
        XCTAssertThrowsError(try CatalogDataService(paths: paths).updateReasoning(
            ReasoningSettings(supportedEfforts: ["low"], defaultEffort: "max"), for: "vendor-model"
        ))
        XCTAssertEqual(try Data(contentsOf: custom), savedSource)
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

    func testOfficialFieldOverridesFollowFreshUpstreamAndCanBeRemoved() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runtime = directory.appendingPathComponent("codex")
        let upstream = directory.appendingPathComponent("upstream.json")
        let custom = directory.appendingPathComponent("custom.json")
        let script = """
        #!/bin/sh
        if [ "$1" = "--version" ]; then
          echo 'codex-test 1.0'
        else
          cat '\(upstream.path)'
        fi
        """
        try Data(script.utf8).write(to: runtime)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: runtime.path)
        var official: [String: Any] = [
            "slug": "gpt-official", "priority": 1, "visibility": "hide",
            "context_window": 200_000, "max_context_window": 800_000,
            "input_modalities": ["text"], "future_metadata": ["version": 1],
            "supports_reasoning_summaries": false, "supports_parallel_tool_calls": false
        ]
        let customData = Data(#"{"models":[{"slug":"vendor-model","context_window":128000,"max_context_window":128000}]}"#.utf8)
        try customData.write(to: custom)
        func writeUpstream() throws {
            try JSONSerialization.data(withJSONObject: ["models": [official]]).write(to: upstream)
        }
        func readModels(_ paths: CatalogPaths) throws -> [[String: Any]] {
            let root = try JSONSerialization.jsonObject(with: Data(contentsOf: paths.mergedCatalog)) as! [String: Any]
            return root["models"] as! [[String: Any]]
        }
        func paths(_ overrides: [String: ModelFieldOverrides]) -> CatalogPaths {
            makePaths(directory: directory, runtime: runtime, custom: custom, modelOverrides: overrides)
        }
        try writeUpstream()
        let currentOnly = paths([
            "gpt-official": ModelFieldOverrides(contextWindow: 393_216),
            "vendor-model": ModelFieldOverrides(contextWindow: 999),
            "future-model": ModelFieldOverrides(contextWindow: 123)
        ])
        let service = CatalogSyncService(paths: currentOnly)
        let result = try service.sync()
        var expected = official
        expected["context_window"] = 393_216
        expected["visibility"] = "list"
        XCTAssertEqual(try readModels(currentOnly)[0] as NSDictionary, expected as NSDictionary)
        XCTAssertEqual(try readModels(currentOnly)[1]["context_window"] as? Int, 128_000)
        XCTAssertEqual(try Data(contentsOf: custom), customData)
        let record = try JSONSerialization.jsonObject(with: result.recordData) as! [String: Any]
        XCTAssertEqual(record["inactive_model_overrides"] as? [String], ["future-model", "vendor-model"])
        XCTAssertEqual(try service.sync().status, "no_change")

        official["context_window"] = 300_000
        official["max_context_window"] = 1_000_000
        official["input_modalities"] = ["text", "image"]
        official["future_metadata"] = ["version": 2]
        official["default_reasoning_level"] = "max"
        try writeUpstream()
        XCTAssertEqual(try service.sync().status, "updated")
        expected = official
        expected["context_window"] = 393_216
        expected["visibility"] = "list"
        XCTAssertEqual(try readModels(currentOnly)[0] as NSDictionary, expected as NSDictionary)

        let maximumOnly = paths(["gpt-official": ModelFieldOverrides(maxContextWindow: 500_000)])
        _ = try CatalogSyncService(paths: maximumOnly).sync()
        XCTAssertEqual(try readModels(maximumOnly)[0]["context_window"] as? Int, 300_000)
        XCTAssertEqual(try readModels(maximumOnly)[0]["max_context_window"] as? Int, 500_000)

        let both = paths(["gpt-official": ModelFieldOverrides(contextWindow: 393_216, maxContextWindow: 393_216)])
        _ = try CatalogSyncService(paths: both).sync()
        XCTAssertEqual(try readModels(both)[0]["context_window"] as? Int, 393_216)
        XCTAssertEqual(try readModels(both)[0]["max_context_window"] as? Int, 393_216)
        let beforeInvalid = try Data(contentsOf: both.mergedCatalog)
        for override in [ModelFieldOverrides(contextWindow: 0), ModelFieldOverrides(maxContextWindow: 1)] {
            XCTAssertThrowsError(try CatalogSyncService(paths: paths(["gpt-official": override])).sync())
            XCTAssertEqual(try Data(contentsOf: both.mergedCatalog), beforeInvalid)
        }

        let cleared = paths([:])
        _ = try CatalogSyncService(paths: cleared).sync()
        expected = official
        expected["visibility"] = "list"
        XCTAssertEqual(try readModels(cleared)[0] as NSDictionary, expected as NSDictionary)
    }

    private func makePaths(
        directory: URL, runtime: URL, custom: URL,
        modelOverrides: [String: ModelFieldOverrides] = [:]
    ) -> CatalogPaths {
        CatalogPaths(
            codexRuntime: runtime,
            customSource: custom,
            mergedCatalog: directory.appendingPathComponent("models.json"),
            syncLog: directory.appendingPathComponent("sync.jsonl"),
            errorLog: directory.appendingPathComponent("sync.error.log"),
            launchAgentPlist: directory.appendingPathComponent("sync.plist"),
            launchAgentLabel: "com.example.test-sync",
            backupDirectory: directory.appendingPathComponent("Backups", isDirectory: true),
            visibilityOverrides: ["gpt-official": .list, "vendor-model": .hide, "future-model": .list],
            modelOverrides: modelOverrides
        )
    }
}
