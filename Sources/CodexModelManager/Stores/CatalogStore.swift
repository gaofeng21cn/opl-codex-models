import AppKit
import CodexModelCore
import Foundation

@MainActor
final class CatalogStore: ObservableObject {
    @Published private(set) var snapshot = CatalogSnapshot.empty
    @Published private(set) var isRefreshing = false
    @Published private(set) var isSyncing = false
    @Published private(set) var isAddingModel = false
    @Published private(set) var isApplyingConfiguration = false
    @Published var errorMessage: String?
    @Published var isPresentingAddModel = false

    @Published private(set) var paths: CatalogPaths?
    @Published private(set) var configuration: AppConfiguration?
    let configurationURL: URL
    private var service: CatalogDataService?
    private var configurationErrorMessage: String?

    var isConfigured: Bool { service != nil }
    var isBusy: Bool { isSyncing || isAddingModel || isApplyingConfiguration }

    init(configurationURL: URL = AppConfiguration.defaultURL) {
        self.configurationURL = configurationURL
        do {
            let configuration = try AppConfiguration.read(from: configurationURL)
            let paths = try configuration.resolved()
            self.configuration = configuration
            self.paths = paths
            self.service = CatalogDataService(paths: paths)
            self.configurationErrorMessage = nil
        } catch {
            self.configuration = nil
            self.paths = nil
            self.service = nil
            self.configurationErrorMessage = error.localizedDescription
        }
    }

    init(service: CatalogDataService, configurationURL: URL = AppConfiguration.defaultURL) {
        self.configurationURL = configurationURL
        self.configuration = nil
        self.paths = service.paths
        self.service = service
        self.configurationErrorMessage = nil
    }

    func recommendedConfiguration() throws -> AppConfiguration {
        try AppConfiguration.recommended()
    }

    func applyConfiguration(_ configuration: AppConfiguration) async -> Bool {
        guard !isBusy else { return false }
        isApplyingConfiguration = true
        defer { isApplyingConfiguration = false }
        do {
            let configurationURL = configurationURL
            let helperURL = Self.packagedHelperURL
            let result = try await Task.detached(priority: .userInitiated) {
                try SetupService().apply(
                    configuration: configuration,
                    configurationURL: configurationURL,
                    helperURL: helperURL
                )
            }.value
            self.configuration = configuration
            self.paths = result.paths
            self.service = CatalogDataService(paths: result.paths)
            self.configurationErrorMessage = nil
            await refresh()
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }

    func refresh() async {
        guard !isRefreshing else { return }
        guard let service else {
            errorMessage = configurationErrorMessage
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let service = service
            snapshot = try await Task.detached(priority: .userInitiated) {
                try service.loadSnapshot()
            }.value
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func syncNow() async {
        guard !isBusy else { return }
        guard let service else {
            errorMessage = configurationErrorMessage
            return
        }
        isSyncing = true
        defer { isSyncing = false }
        do {
            let service = service
            try await Task.detached(priority: .userInitiated) {
                try service.runSync()
            }.value
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
            await refresh()
        }
    }

    func addModel(_ draft: NewModelDraft) async -> Bool {
        guard !isBusy else { return false }
        guard let service else {
            errorMessage = configurationErrorMessage
            return false
        }
        isAddingModel = true
        defer { isAddingModel = false }
        do {
            let service = service
            try await Task.detached(priority: .userInitiated) {
                try service.addCustomModel(draft)
                try service.runSync()
            }.value
            await refresh()
            return true
        } catch {
            errorMessage = error.localizedDescription
            await refresh()
            return false
        }
    }

    func reveal(_ url: URL) {
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func setVisibility(_ visibility: ModelVisibility?, for slug: String) async {
        do {
            var updated = try AppConfiguration.read(from: configurationURL)
            var overrides = updated.visibilityOverrides ?? [:]
            overrides[slug] = visibility
            updated.visibilityOverrides = overrides
            _ = await applyConfiguration(updated)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private static var packagedHelperURL: URL {
        Bundle.main.bundleURL
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("Helpers", isDirectory: true)
            .appendingPathComponent("CodexModelSync")
    }
}
