@testable import CodexModelCore
import Foundation
import XCTest

final class ContextManagementSettingsTests: XCTestCase {
    func testTogglePreservesCommentsProfilesAndStringContents() throws {
        let original = """
        # My settings
        model_provider = "gflab"
        notes = '''experimental_mode = false'''
        [profiles.other.features.context_management]
        experimental_mode = false
        [features.context_management]
        experimental_mode = false # keep comment
        """
        let updated = try ContextManagementSettings.updating(original, enabled: true)
        XCTAssertEqual(updated, original.replacingOccurrences(
            of: "experimental_mode = false # keep comment", with: "experimental_mode = true # keep comment"
        ))
        XCTAssertEqual(try ContextManagementSettings.updating(updated, enabled: false), original)
        XCTAssertEqual(try ContextManagementSettings.updating(updated, enabled: true), updated)
    }

    func testSupportsDottedAndInlineSettings() throws {
        for original in [
            "features.context_management.experimental_mode = false\n",
            "[features]\ncontext_management.experimental_mode = false\n",
            "features = { context_management = { experimental_mode = false } }\n"
        ] {
            XCTAssertEqual(try ContextManagementSettings.updating(original, enabled: true),
                           original.replacingOccurrences(of: "false", with: "true"))
        }
    }

    func testAddsMissingSettingAndReadsBackWithBackup() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let config = directory.appendingPathComponent("config.toml")
        let original = "model_provider = 'gflab'\n[features]\nmemories = true\n"
        try Data(original.utf8).write(to: config)
        let backups = directory.appendingPathComponent("Backups")
        try ContextManagementSettings.setEnabled(true, in: config, backupDirectory: backups)
        let current = try ContextManagementSettings.read(from: config)
        XCTAssertTrue(current.enabled)
        XCTAssertEqual(current.provider, "gflab")
        let files = try FileManager.default.contentsOfDirectory(at: backups, includingPropertiesForKeys: nil)
        XCTAssertEqual(files.count, 1)
        XCTAssertEqual(try String(contentsOf: files[0]), original)
        try ContextManagementSettings.setEnabled(true, in: config, backupDirectory: backups)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: backups.path).count, 1)
    }

    func testInsertsIntoExistingEmptySection() throws {
        let original = "[features.context_management]\n# retained\n[other]\nvalue = true\n"
        XCTAssertEqual(try ContextManagementSettings.updating(original, enabled: true),
                       "[features.context_management]\nexperimental_mode = true\n# retained\n[other]\nvalue = true\n")
    }

    func testRejectsInvalidOrAmbiguousConfiguration() {
        XCTAssertThrowsError(try ContextManagementSettings.updating("invalid = [", enabled: true))
        XCTAssertThrowsError(try ContextManagementSettings.updating("[features]\ncontext_management = true", enabled: true))
    }
}
