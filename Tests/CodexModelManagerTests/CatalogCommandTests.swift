import CodexModelCore
import Foundation
import XCTest
@testable import CodexModelManager

final class CatalogCommandTests: XCTestCase {
    func testReadPreviewSaveAndStaleRevisionUseTheSameSourceAsGUI() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        func command(_ args: [String]) throws -> [String: Any] {
            let data = try CatalogCommand.run(["--config", fixture.config.path, "--json"] + args)
            return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        }
        let before = try command(["read"])
        let digest = try XCTUnwrap(before["source_digest"] as? String)
        let original = try Data(contentsOf: fixture.paths.customSource)
        let options = ["--slug", "vendor", "--template", "official", "--name", "Vendor", "--description", "Hosted model"]
        let preview = try command(["preview", "add"] + options)
        XCTAssertEqual(preview["source_digest"] as? String, digest)
        XCTAssertEqual(try Data(contentsOf: fixture.paths.customSource), original)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.paths.backupDirectory.path))
        let saved = try command(["add"] + options + ["--expected-source-digest", digest])
        XCTAssertEqual(saved["status"] as? String, "source_saved")
        XCTAssertEqual(saved["sync_required"] as? Bool, true)
        XCTAssertEqual(saved["source_digest"] as? String, preview["proposed_source_digest"] as? String)
        XCTAssertEqual(try Data(contentsOf: URL(fileURLWithPath: XCTUnwrap(saved["backup_path"] as? String))), original)
        // GUI sees and edits the exact CLI-created source through the shared core.
        try CatalogDataService(paths: fixture.paths).updateReasoning(
            ReasoningSettings(supportedEfforts: ["low", "high"], defaultEffort: "high"), for: "vendor")
        let current = try command(["read"])
        XCTAssertNotEqual(current["source_digest"] as? String, saved["source_digest"] as? String)
        XCTAssertThrowsError(try command(["remove", "--slug", "vendor", "--expected-source-digest", digest])) { error in
            let result = try? JSONSerialization.jsonObject(with: CatalogCommand.errorData(error)) as? [String: Any]
            XCTAssertEqual((result?["error"] as? [String: Any])?["code"] as? String, "source_conflict")
        }
        XCTAssertEqual(try command(["read"])["source_digest"] as? String, current["source_digest"] as? String)
    }

    func testMutationRequiresDigestAndInvalidOptionsNeverWrite() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let original = try Data(contentsOf: fixture.paths.customSource)
        for args in [
            ["remove", "--slug", "vendor"],
            ["remove", "--slug", "vendor", "--force"],
            ["preview", "add", "--slug", "vendor", "--template", "official", "--name", "Vendor", "--description", "Desc", "--context-window", "bad"]
        ] {
            XCTAssertThrowsError(try CatalogCommand.run(["--config", fixture.config.path] + args))
        }
        XCTAssertEqual(try Data(contentsOf: fixture.paths.customSource), original)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.paths.backupDirectory.path))
    }

    func testLocalReadDoesNotRequireRuntimeOrSchedulerConfiguration() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        var configuration = try AppConfiguration.read(from: fixture.config)
        configuration.codexRuntimePath = nil
        configuration.launchAgentLabel = ""
        try configuration.save(to: fixture.config)
        let data = try CatalogCommand.run(["read", "--config", fixture.config.path, "--json"])
        let result = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(result["status"] as? String, "read")
        XCTAssertThrowsError(try CatalogCommand.run(["sync", "--config", fixture.config.path, "--json"]))
    }

    func testCompetingWritersCannotBothUseSameRevision() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let source = try Data(contentsOf: fixture.paths.customSource)
        let digest = CatalogSourceService.digest(source)
        let group = DispatchGroup()
        let results = NSLock()
        var saved = 0
        var conflicts = 0
        for index in 0..<2 {
            group.enter()
            DispatchQueue.global().async {
                defer { group.leave() }
                do {
                    _ = try CatalogSourceService(paths: fixture.paths).edit(expectedDigest: digest) { _, _ in
                        Data("{\"models\":[{\"slug\":\"writer-\(index)\"}]}".utf8)
                    }
                    results.lock(); saved += 1; results.unlock()
                } catch CatalogCommandError.sourceConflict {
                    results.lock(); conflicts += 1; results.unlock()
                } catch { XCTFail("Unexpected error: \(error)") }
            }
        }
        XCTAssertEqual(group.wait(timeout: .now() + 5), .success)
        XCTAssertEqual(saved, 1)
        XCTAssertEqual(conflicts, 1)
    }

    private func makeFixture() throws -> (directory: URL, config: URL, paths: CatalogPaths) {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let config = directory.appendingPathComponent("config.json")
        var configuration = AppConfiguration(codexRuntimePath: "/usr/bin/false",
            customSourcePath: directory.appendingPathComponent("custom.json").path,
            mergedCatalogPath: directory.appendingPathComponent("catalog.json").path,
            syncLogPath: directory.appendingPathComponent("sync.log").path,
            errorLogPath: directory.appendingPathComponent("error.log").path,
            launchAgentPlistPath: directory.appendingPathComponent("sync.plist").path,
            launchAgentLabel: "test.catalog", backupDirectoryPath: directory.appendingPathComponent("backups").path)
        configuration.codexHomePath = directory.appendingPathComponent("codex-home").path
        try configuration.save(to: config)
        let paths = try configuration.resolved()
        try Data(#"{"schema":"future.v2","models":[]}"#.utf8).write(to: paths.customSource)
        try Data(#"{"models":[{"slug":"official","display_name":"Official","description":"Official template","priority":1,"wire_api":"responses"}]}"#.utf8).write(to: paths.mergedCatalog)
        return (directory, config, paths)
    }
}
