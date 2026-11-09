@testable import CodexModelCore
@testable import CodexModelManager
import Foundation
import XCTest

final class SelectedCodexHomeTests: XCTestCase {
    func testSettingsSetupAndBothCommandFormsShareSelectedHome() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.writeSelectedCredentials()
        let defaultConfigBefore = try Data(contentsOf: fixture.defaultConfiguration)
        let defaultAuthBefore = try Data(contentsOf: fixture.defaultCredentials)
        let selectedAuthBefore = try Data(contentsOf: fixture.selectedCredentials)

        // Exercise the settings model that the native GUI saves, not a second
        // CLI-only target selection.
        var draft = ConfigurationDraft(configuration: fixture.configuration)
        draft.codexHomePath = fixture.selectedHome.path
        try draft.configuration.save(to: fixture.appConfiguration)
        let saved = try AppConfiguration.read(from: fixture.appConfiguration)
        XCTAssertEqual(saved.codexHomePath, fixture.selectedHome.path)
        let paths = try saved.resolved(homeDirectory: fixture.userHome)
        XCTAssertEqual(paths.codexHome.path, fixture.selectedHome.path)
        XCTAssertEqual(paths.codexConfiguration, fixture.selectedConfiguration)

        let setup = try SetupService().apply(
            configuration: saved,
            configurationURL: fixture.appConfiguration,
            helperURL: fixture.runtime,
            installDailySync: false,
            homeDirectory: fixture.userHome
        )
        XCTAssertEqual(setup.paths.codexHome.path, fixture.selectedHome.path)
        let selectedConfig = try String(contentsOf: fixture.selectedConfiguration, encoding: .utf8)
        XCTAssertTrue(selectedConfig.contains("model_catalog_json = \"\(paths.mergedCatalog.path)\""))
        XCTAssertTrue(selectedConfig.contains("cli_auth_credentials_store = \"file\""))

        let explicit = try CatalogCommand.run([
            "sync", "--config", fixture.appConfiguration.path, "--json"
        ])
        // LaunchAgent uses this same helper form without a subcommand.
        let scheduled = try CatalogCommand.run(["--config", fixture.appConfiguration.path])
        for output in [explicit, scheduled] {
            let record = try fixture.record(output)
            XCTAssertEqual(record["official_source"] as? String, "refresh")
            XCTAssertNil(record["official_fallback_reason"])
            fixture.assertNoCredentialContent(output)
        }
        XCTAssertEqual(try fixture.slugs(), ["selected-account-model"])
        XCTAssertEqual(try Data(contentsOf: fixture.defaultConfiguration), defaultConfigBefore)
        XCTAssertEqual(try Data(contentsOf: fixture.defaultCredentials), defaultAuthBefore)
        XCTAssertEqual(try Data(contentsOf: fixture.selectedCredentials), selectedAuthBefore)
        XCTAssertEqual(try fixture.queryKinds(), ["refresh", "refresh", "refresh"])
        try fixture.assertTemporaryHomesRemoved()
    }

    func testMissingSelectedCredentialsNeverUseDefaultAccount() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        let paths = try fixture.selectedConfigurationPaths()
        let result = try CatalogSyncService(paths: paths).sync()
        try fixture.assertBundled(result, reason: "file_credentials_unavailable")
        XCTAssertEqual(try fixture.queryKinds(), ["bundled"])
        try fixture.assertTemporaryHomesRemoved()
    }

    func testBuiltHelperIgnoresLaunchingShellHomeAndCredentials() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.writeSelectedCredentials()
        var configuration = fixture.configuration
        configuration.codexHomePath = fixture.selectedHome.path
        try configuration.save(to: fixture.appConfiguration)
        let helper = Bundle(for: Self.self).bundleURL.deletingLastPathComponent()
            .appendingPathComponent("CodexModelSync")
        let result = try ProcessRunner.run(helper,
            arguments: ["--config", fixture.appConfiguration.path, "--json"],
            environment: [
                "CODEX_HOME": fixture.defaultHome.path,
                "OPENAI_API_KEY": "other-account-private-marker",
                "CODEX_API_KEY": "other-account-private-marker",
                "CODEX_ACCESS_TOKEN": "other-account-private-marker",
                "OPENAI_FEDERATION_RULE_ID": "other-account-private-marker",
                "OPENAI_IDENTITY_TOKEN_FILE": "/nonexistent-synthetic-token",
                "OPENAI_WORKLOAD_IDENTITY_CONTEXT": "other-account-private-marker"
            ])
        XCTAssertEqual(result.exitCode, 0)
        let output = Data(result.standardOutput.utf8)
        XCTAssertEqual(try fixture.record(output)["official_source"] as? String, "refresh")
        XCTAssertEqual(try fixture.slugs(), ["selected-account-model"])
        fixture.assertNoCredentialContent(output)
        XCTAssertFalse(result.standardError.contains("private-marker"))
        try fixture.assertTemporaryHomesRemoved()
    }

    func testMissingSelectedHomeNeverUsesDefaultAccount() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try FileManager.default.removeItem(at: fixture.selectedHome)
        let paths = try fixture.selectedConfigurationPaths()
        let result = try CatalogSyncService(paths: paths).sync()
        try fixture.assertBundled(result, reason: "file_credentials_unavailable")
        XCTAssertEqual(paths.codexHome.path, fixture.selectedHome.path)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.selectedHome.path))
        XCTAssertEqual(try fixture.queryKinds(), ["bundled"])
        try fixture.assertTemporaryHomesRemoved()
    }

    func testNativeCredentialModesDoNotUseAnOldAuthFile() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.writeSelectedCredentials()
        for mode in ["keyring", "auto", "ephemeral"] {
            try Data("cli_auth_credentials_store = \"\(mode)\"\n".utf8)
                .write(to: fixture.selectedConfiguration)
            let result = try CatalogSyncService(paths: fixture.selectedConfigurationPaths()).sync()
            try fixture.assertBundled(result, reason: "credential_store_requires_native_access")
        }
        XCTAssertEqual(try fixture.queryKinds(), ["bundled", "bundled", "bundled"])
        try fixture.assertTemporaryHomesRemoved()
    }

    func testRefreshFailureReportsBoundedFallbackAndCleansTemporaryCredentials() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.writeSelectedCredentials()
        try Data().write(to: fixture.failRefresh)
        let result = try CatalogSyncService(paths: fixture.selectedConfigurationPaths()).sync()
        try fixture.assertBundled(result, reason: "refresh_unavailable")
        XCTAssertEqual(try fixture.queryKinds(), ["refresh", "bundled"])
        try fixture.assertTemporaryHomesRemoved()
    }

    func testMalformedSelectedConfigurationDoesNotTryAnotherCredentialSource() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.writeSelectedCredentials()
        try Data("cli_auth_credentials_store = [\n".utf8).write(to: fixture.selectedConfiguration)
        let result = try CatalogSyncService(paths: fixture.selectedConfigurationPaths()).sync()
        try fixture.assertBundled(result, reason: "credential_configuration_unavailable")
        XCTAssertEqual(try fixture.queryKinds(), ["bundled"])
        try fixture.assertTemporaryHomesRemoved()
    }

    private struct Fixture {
        let root: URL
        let userHome: URL
        let selectedHome: URL
        let runtime: URL
        let appConfiguration: URL
        let queryLog: URL
        let failRefresh: URL
        let configuration: AppConfiguration

        var defaultHome: URL { userHome.appendingPathComponent(".codex", isDirectory: true) }
        var defaultConfiguration: URL { defaultHome.appendingPathComponent("config.toml") }
        var defaultCredentials: URL { defaultHome.appendingPathComponent("auth.json") }
        var selectedConfiguration: URL { selectedHome.appendingPathComponent("config.toml") }
        var selectedCredentials: URL { selectedHome.appendingPathComponent("auth.json") }

        init() throws {
            root = FileManager.default.temporaryDirectory
                .appendingPathComponent("selected-codex-home-\(UUID().uuidString)", isDirectory: true)
            userHome = root.appendingPathComponent("user", isDirectory: true)
            selectedHome = root.appendingPathComponent("selected profile", isDirectory: true)
            runtime = root.appendingPathComponent("codex-fixture")
            appConfiguration = root.appendingPathComponent("app.json")
            queryLog = root.appendingPathComponent("queries.log")
            failRefresh = root.appendingPathComponent("fail-refresh")
            configuration = AppConfiguration(
                codexRuntimePath: runtime.path,
                customSourcePath: root.appendingPathComponent("custom.json").path,
                mergedCatalogPath: root.appendingPathComponent("models.json").path,
                syncLogPath: root.appendingPathComponent("sync.jsonl").path,
                errorLogPath: root.appendingPathComponent("sync.error.log").path,
                launchAgentPlistPath: root.appendingPathComponent("sync.plist").path,
                launchAgentLabel: "com.example.selected-codex-home",
                backupDirectoryPath: root.appendingPathComponent("Backups").path
            )
            try FileManager.default.createDirectory(at: defaultHome, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: selectedHome, withIntermediateDirectories: true)
            try Data("model_catalog_json = \"default-account-must-not-change\"\n".utf8)
                .write(to: defaultConfiguration)
            try Data(#"{"fixture_account":"other-account-private-marker"}"#.utf8)
                .write(to: defaultCredentials)
            try Data("cli_auth_credentials_store = \"file\"\nmodel_catalog_json = \"must-not-be-inherited\"\n".utf8)
                .write(to: selectedConfiguration)
            let script = """
            #!/bin/sh
            if [ "$1" = "--version" ]; then
              echo 'codex-fixture 1.0'
              exit 0
            fi
            mode=refresh
            file_store=0
            while [ "$#" -gt 0 ]; do
              case "$1" in
                --bundled) mode=bundled ;;
                -c|--config)
                  shift
                  [ "$1" = 'cli_auth_credentials_store="file"' ] && file_store=1
                  ;;
              esac
              shift
            done
            [ -n "$CODEX_HOME" ] || exit 31
            [ "$CODEX_HOME" != \(Self.quote(selectedHome.path)) ] || exit 32
            [ "$CODEX_HOME" != \(Self.quote(defaultHome.path)) ] || exit 33
            [ ! -e "$CODEX_HOME/config.toml" ] || exit 34
            [ -z "$OPENAI_API_KEY$CODEX_API_KEY$CODEX_ACCESS_TOKEN$OPENAI_FEDERATION_RULE_ID$OPENAI_IDENTITY_TOKEN_FILE$OPENAI_WORKLOAD_IDENTITY_CONTEXT" ] || exit 40
            printf '%s\\t%s\\n' "$mode" "$CODEX_HOME" >> \(Self.quote(queryLog.path))
            if [ "$mode" = bundled ]; then
              [ ! -e "$CODEX_HOME/auth.json" ] || exit 35
              echo '{"models":[{"slug":"bundled-model","priority":1,"context_window":1000,"max_context_window":1000}]}'
              exit 0
            fi
            [ "$file_store" = 1 ] || exit 36
            [ -f "$CODEX_HOME/auth.json" ] || exit 37
            [ "$(cat "$CODEX_HOME/auth.json")" = '{"fixture_account":"selected-private-marker"}' ] || exit 38
            if [ -e \(Self.quote(failRefresh.path)) ]; then
              echo 'selected-private-marker' >&2
              exit 39
            fi
            echo '{"models":[{"slug":"selected-account-model","priority":1,"context_window":1000,"max_context_window":1000}]}'
            """
            try Data(script.utf8).write(to: runtime)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: runtime.path)
        }

        func remove() { try? FileManager.default.removeItem(at: root) }

        func writeSelectedCredentials() throws {
            try Data(#"{"fixture_account":"selected-private-marker"}"#.utf8).write(to: selectedCredentials)
        }

        func selectedConfigurationPaths() throws -> CatalogPaths {
            var selected = configuration
            selected.codexHomePath = selectedHome.path
            return try selected.resolved(homeDirectory: userHome)
        }

        func record(_ data: Data) throws -> [String: Any] {
            try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        }

        func slugs() throws -> [String] {
            let catalog = try record(Data(contentsOf: URL(fileURLWithPath: configuration.mergedCatalogPath)))
            return try XCTUnwrap(catalog["models"] as? [[String: Any]]).compactMap { $0["slug"] as? String }
        }

        func queryKinds() throws -> [String] {
            try queries().compactMap { $0.first }
        }

        func queries() throws -> [[String]] {
            try String(contentsOf: queryLog, encoding: .utf8).split(separator: "\n")
                .map { $0.split(separator: "\t").map(String.init) }
        }

        func assertBundled(_ result: CatalogSyncResult, reason: String, file: StaticString = #filePath, line: UInt = #line) throws {
            let output = try record(result.recordData)
            XCTAssertEqual(output["official_source"] as? String, "bundled", file: file, line: line)
            XCTAssertEqual(output["official_fallback_reason"] as? String, reason, file: file, line: line)
            XCTAssertEqual(try slugs(), ["bundled-model"], file: file, line: line)
            assertNoCredentialContent(result.recordData, file: file, line: line)
        }

        func assertNoCredentialContent(_ output: Data, file: StaticString = #filePath, line: UInt = #line) {
            let text = String(decoding: output, as: UTF8.self)
            XCTAssertFalse(text.contains("private-marker"), file: file, line: line)
            XCTAssertFalse(text.contains("fixture_account"), file: file, line: line)
        }

        func assertTemporaryHomesRemoved(file: StaticString = #filePath, line: UInt = #line) throws {
            for query in try queries() {
                XCTAssertEqual(query.count, 2, file: file, line: line)
                if query.count == 2 {
                    XCTAssertFalse(FileManager.default.fileExists(atPath: query[1]), file: file, line: line)
                }
            }
        }

        private static func quote(_ value: String) -> String {
            "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
        }
    }
}
