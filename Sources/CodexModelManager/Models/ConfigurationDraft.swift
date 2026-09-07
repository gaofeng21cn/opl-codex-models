import CodexModelCore
import Foundation

struct ConfigurationDraft: Equatable {
    var codexRuntimePath = ""
    var customSourcePath = ""
    var mergedCatalogPath = ""
    var syncLogPath = ""
    var errorLogPath = ""
    var launchAgentPlistPath = ""
    var launchAgentLabel = ""
    var backupDirectoryPath = ""
    var visibilityOverrides: [String: ModelVisibility] = [:]

    init() {}

    init(configuration: AppConfiguration) {
        codexRuntimePath = configuration.codexRuntimePath ?? ""
        customSourcePath = configuration.customSourcePath
        mergedCatalogPath = configuration.mergedCatalogPath
        syncLogPath = configuration.syncLogPath
        errorLogPath = configuration.errorLogPath
        launchAgentPlistPath = configuration.launchAgentPlistPath
        launchAgentLabel = configuration.launchAgentLabel
        backupDirectoryPath = configuration.backupDirectoryPath
        visibilityOverrides = configuration.visibilityOverrides ?? [:]
    }

    var configuration: AppConfiguration {
        AppConfiguration(
            codexRuntimePath: codexRuntimePath.isEmpty ? nil : codexRuntimePath,
            customSourcePath: customSourcePath,
            mergedCatalogPath: mergedCatalogPath,
            syncLogPath: syncLogPath,
            errorLogPath: errorLogPath,
            launchAgentPlistPath: launchAgentPlistPath,
            launchAgentLabel: launchAgentLabel,
            backupDirectoryPath: backupDirectoryPath,
            visibilityOverrides: visibilityOverrides
        )
    }

    var isComplete: Bool {
        [
            customSourcePath,
            mergedCatalogPath,
            syncLogPath,
            errorLogPath,
            launchAgentPlistPath,
            launchAgentLabel,
            backupDirectoryPath
        ].allSatisfy { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
}
