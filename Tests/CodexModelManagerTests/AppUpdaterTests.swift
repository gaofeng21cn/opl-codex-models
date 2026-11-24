import Foundation
import XCTest

@testable import CodexModelManager

@MainActor
final class AppUpdaterTests: XCTestCase {
    func testCanStartSparkleValidatesAppBundleAndInfoPlistContract() throws {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-models-updater-tests-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let validBundle = try makeTestAppBundle(
            at: tempRoot.appendingPathComponent("Valid.app", isDirectory: true),
            feedURL: AppUpdateConfiguration.canonicalFeedURL,
            publicEDKey: AppUpdateConfiguration.canonicalPublicEDKey
        )
        XCTAssertTrue(
            AppUpdateConfiguration.canStartSparkle(
                bundle: validBundle,
                environment: [:]
            )
        )

        XCTAssertFalse(
            AppUpdateConfiguration.canStartSparkle(
                bundle: validBundle,
                environment: ["XCTestConfigurationFilePath": "/tmp/test.xctestconfiguration"]
            )
        )

        let nonAppBundle = try makeTestAppBundle(
            at: tempRoot.appendingPathComponent("BareDir", isDirectory: true),
            feedURL: AppUpdateConfiguration.canonicalFeedURL,
            publicEDKey: AppUpdateConfiguration.canonicalPublicEDKey
        )
        XCTAssertFalse(
            AppUpdateConfiguration.canStartSparkle(
                bundle: nonAppBundle,
                environment: [:]
            )
        )

        let httpFeedBundle = try makeTestAppBundle(
            at: tempRoot.appendingPathComponent("Insecure.app", isDirectory: true),
            feedURL: "http://example.com/appcast.xml",
            publicEDKey: AppUpdateConfiguration.canonicalPublicEDKey
        )
        XCTAssertFalse(
            AppUpdateConfiguration.canStartSparkle(
                bundle: httpFeedBundle,
                environment: [:]
            )
        )

        let missingKeyBundle = try makeTestAppBundle(
            at: tempRoot.appendingPathComponent("MissingKey.app", isDirectory: true),
            feedURL: AppUpdateConfiguration.canonicalFeedURL,
            publicEDKey: nil
        )
        XCTAssertFalse(
            AppUpdateConfiguration.canStartSparkle(
                bundle: missingKeyBundle,
                environment: [:]
            )
        )
    }

    func testMonotonicBuildNumberNeverRegressesOnDoubleDigitMinorOrPatch() {
        XCTAssertEqual(AppUpdateConfiguration.monotonicBuildNumber(forVersion: "0.4.6"), "406")
        XCTAssertEqual(AppUpdateConfiguration.monotonicBuildNumber(forVersion: "0.4.10"), "410")
        XCTAssertEqual(AppUpdateConfiguration.monotonicBuildNumber(forVersion: "0.5.0"), "500")
        XCTAssertLessThan(
            Int(AppUpdateConfiguration.monotonicBuildNumber(forVersion: "0.4.10")!)!,
            Int(AppUpdateConfiguration.monotonicBuildNumber(forVersion: "0.5.0")!)!
        )
        XCTAssertNil(AppUpdateConfiguration.monotonicBuildNumber(forVersion: "invalid"))
    }

    func testAppUpdaterFormatsVersionAndInvokesCheckAction() throws {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("codex-models-version-tests-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: tempRoot) }

        let bundle = try makeTestAppBundle(
            at: tempRoot.appendingPathComponent("Sample.app", isDirectory: true),
            feedURL: AppUpdateConfiguration.canonicalFeedURL,
            publicEDKey: AppUpdateConfiguration.canonicalPublicEDKey
        )

        var triggered = 0
        let updater = AppUpdater(
            isConfigured: true,
            canCheckForUpdates: true,
            automaticallyChecksForUpdates: true,
            bundle: bundle
        ) {
            triggered += 1
        }

        XCTAssertTrue(updater.isConfigured)
        XCTAssertTrue(updater.canCheckForUpdates)
        XCTAssertTrue(updater.automaticallyChecksForUpdates)
        XCTAssertEqual(updater.currentVersionLabel, "v0.4.6")

        updater.checkForUpdates()
        XCTAssertEqual(triggered, 1)

        let decodedKey = try XCTUnwrap(Data(base64Encoded: AppUpdateConfiguration.canonicalPublicEDKey))
        XCTAssertEqual(decodedKey.count, 32)
    }

    private func makeTestAppBundle(
        at bundleURL: URL,
        feedURL: String?,
        publicEDKey: String?
    ) throws -> Bundle {
        let contentsURL = bundleURL.appendingPathComponent("Contents", isDirectory: true)
        try FileManager.default.createDirectory(at: contentsURL, withIntermediateDirectories: true)

        var info: [String: Any] = [
            "CFBundleIdentifier": "com.onepersonlab.codex-model-manager.tests",
            "CFBundleName": "CodexModelManager",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "0.4.6",
            "CFBundleVersion": "406",
        ]
        if let feedURL {
            info[AppUpdateConfiguration.feedURLKey] = feedURL
        }
        if let publicEDKey {
            info[AppUpdateConfiguration.publicEDKey] = publicEDKey
        }

        let data = try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
        try data.write(to: contentsURL.appendingPathComponent("Info.plist"))
        return try XCTUnwrap(Bundle(url: bundleURL))
    }
}
