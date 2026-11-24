import AppKit
import Foundation
import Sparkle

enum AppUpdateConfiguration {
    static let feedURLKey = "SUFeedURL"
    static let publicEDKey = "SUPublicEDKey"
    static let automaticChecksKey = "SUEnableAutomaticChecks"
    static let scheduledCheckIntervalKey = "SUScheduledCheckInterval"
    static let canonicalFeedURL =
        "https://github.com/gaofeng21cn/opl-codex-models/releases/latest/download/appcast.xml"
    static let canonicalPublicEDKey = "SR+6mkq8nrhuOn5fydmkWDw/DYPXHXOZkCeSqAYNw0o="
    static let scheduledCheckIntervalSeconds = 6 * 60 * 60

    static func canStartSparkle(
        bundle: Bundle = .main,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> Bool {
        guard environment["XCTestConfigurationFilePath"] == nil else {
            return false
        }

        guard bundle.bundleURL.pathExtension == "app" else {
            return false
        }

        guard
            let feedURLString = bundle.object(forInfoDictionaryKey: feedURLKey) as? String,
            let feedURL = URL(string: feedURLString),
            feedURL.scheme == "https",
            let host = feedURL.host,
            !host.isEmpty
        else {
            return false
        }

        guard
            let publicKey = bundle.object(forInfoDictionaryKey: publicEDKey) as? String,
            !publicKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return false
        }

        return true
    }

    static func monotonicBuildNumber(forVersion version: String) -> String? {
        let parts = version.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3,
              let major = Int(parts[0]),
              let minor = Int(parts[1]),
              let patch = Int(parts[2]),
              major >= 0, minor >= 0, patch >= 0,
              minor < 100, patch < 100
        else {
            return nil
        }
        return String(major * 10_000 + minor * 100 + patch)
    }
}

@MainActor
final class AppUpdater: NSObject, ObservableObject {
    @Published private(set) var canCheckForUpdates = false
    @Published private(set) var isConfigured = false
    @Published var automaticallyChecksForUpdates: Bool {
        didSet {
            guard isConfigured,
                  let updater = updaterController?.updater,
                  updater.automaticallyChecksForUpdates != automaticallyChecksForUpdates
            else {
                return
            }
            updater.automaticallyChecksForUpdates = automaticallyChecksForUpdates
        }
    }

    private let bundle: Bundle
    private var updaterController: SPUStandardUpdaterController?
    private var canCheckObservation: NSKeyValueObservation?
    private var checkAction: (() -> Void)?

    init(
        bundle: Bundle = .main,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        startImmediately: Bool = true
    ) {
        self.bundle = bundle
        let configured = AppUpdateConfiguration.canStartSparkle(
            bundle: bundle,
            environment: environment
        )
        self.isConfigured = configured
        self.automaticallyChecksForUpdates = true
        super.init()

        guard configured else {
            self.automaticallyChecksForUpdates = false
            return
        }

        let controller = SPUStandardUpdaterController(
            startingUpdater: startImmediately,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
        self.updaterController = controller
        self.canCheckForUpdates = controller.updater.canCheckForUpdates
        self.automaticallyChecksForUpdates = controller.updater.automaticallyChecksForUpdates
        self.canCheckObservation = controller.updater.observe(
            \.canCheckForUpdates,
            options: [.initial, .new]
        ) { [weak self] _, change in
            guard let canCheck = change.newValue else { return }
            Task { @MainActor [weak self] in
                self?.canCheckForUpdates = canCheck
            }
        }
    }

    /// Test-only initializer for verifying UI and state transitions without launching Sparkle UI.
    init(
        isConfigured: Bool,
        canCheckForUpdates: Bool,
        automaticallyChecksForUpdates: Bool = true,
        bundle: Bundle = .main,
        checkAction: (() -> Void)? = nil
    ) {
        self.bundle = bundle
        self.isConfigured = isConfigured
        self.canCheckForUpdates = canCheckForUpdates
        self.automaticallyChecksForUpdates = automaticallyChecksForUpdates
        self.checkAction = checkAction
        super.init()
    }

    var currentVersionLabel: String {
        guard let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
              !version.isEmpty
        else {
            return "dev"
        }
        return "v\(version)"
    }

    func checkForUpdates() {
        if let checkAction {
            checkAction()
            return
        }
        guard let updaterController else { return }
        NSApp.activate(ignoringOtherApps: true)
        updaterController.checkForUpdates(nil)
    }
}
