import CodexModelCore
import Foundation

struct CatalogDataService {
    let paths: CatalogPaths

    init(paths: CatalogPaths) {
        self.paths = paths
    }

    func loadSnapshot() throws -> CatalogSnapshot {
        let customData = try Data(contentsOf: paths.customSource)
        let catalogData = try Data(contentsOf: paths.mergedCatalog)
        let models = try CatalogParser.parseModels(customData: customData, catalogData: catalogData)
        let logText = (try? String(contentsOf: paths.syncLog, encoding: .utf8)) ?? ""
        let records = CatalogParser.parseRecords(logText)
        let errorBytes = fileSize(paths.errorLog)
        let scheduler = try schedulerSnapshot(errorLogBytes: errorBytes)
        let exactDate = records.first?.recordedAt
        let fallbackDate = modificationDate(paths.syncLog)

        return CatalogSnapshot(
            models: models,
            records: records,
            scheduler: scheduler,
            lastRunAt: exactDate ?? fallbackDate,
            lastRunIsExact: exactDate != nil
        )
    }

    func addCustomModel(_ draft: NewModelDraft) throws {
        let sourceData = try Data(contentsOf: paths.customSource)
        let mergedData = try Data(contentsOf: paths.mergedCatalog)
        let models = try CatalogParser.parseModels(customData: sourceData, catalogData: mergedData)
        let updatedData = try CustomModelEditor.adding(
            draft: draft,
            to: sourceData,
            catalogData: mergedData,
            existingSlugs: Set(models.map(\.slug))
        )

        try writeCustomSource(updatedData, replacing: sourceData)
    }

    func updateReasoning(_ settings: ReasoningSettings, for slug: String) throws {
        let sourceData = try Data(contentsOf: paths.customSource)
        let updatedData = try CustomModelEditor.updatingReasoning(settings, for: slug, in: sourceData)
        try writeCustomSource(updatedData, replacing: sourceData)
    }

    private func writeCustomSource(_ updatedData: Data, replacing sourceData: Data) throws {
        try FileManager.default.createDirectory(
            at: paths.backupDirectory,
            withIntermediateDirectories: true
        )
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        let backup = paths.backupDirectory.appendingPathComponent(
            "custom-models.\(formatter.string(from: Date())).\(UUID().uuidString).json"
        )
        try sourceData.write(to: backup, options: .atomic)
        try updatedData.write(to: paths.customSource, options: .atomic)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: paths.customSource.path
        )
    }

    func runSync() throws {
        let userID = getuid()
        let target = "gui/\(userID)/\(paths.launchAgentLabel)"
        let before = try? schedulerSnapshot(errorLogBytes: fileSize(paths.errorLog)).runs
        let result = try ProcessRunner.run(
            URL(fileURLWithPath: "/bin/launchctl"),
            arguments: ["kickstart", "-k", target]
        )
        guard result.exitCode == 0 else {
            let detail = result.standardError.trimmingCharacters(in: .whitespacesAndNewlines)
            throw AppError.processFailed(
                detail.isEmpty ? "无法启动模型同步任务。" : detail
            )
        }

        for _ in 0..<60 {
            Thread.sleep(forTimeInterval: 0.25)
            let current = try schedulerSnapshot(errorLogBytes: fileSize(paths.errorLog))
            let runAdvanced = before == nil || current.runs != before
            if current.state == "not running", runAdvanced {
                guard current.lastExitCode == 0 else {
                    throw AppError.processFailed("同步任务退出码为 \(current.lastExitCode ?? -1)。")
                }
                return
            }
        }
        throw AppError.syncTimedOut
    }

    private func schedulerSnapshot(errorLogBytes: Int64) throws -> SchedulerSnapshot {
        let target = "gui/\(getuid())/\(paths.launchAgentLabel)"
        let result = try ProcessRunner.run(
            URL(fileURLWithPath: "/bin/launchctl"),
            arguments: ["print", target]
        )
        guard result.exitCode == 0 else {
            return SchedulerSnapshot(errorLogBytes: errorLogBytes)
        }
        let plistData = try Data(contentsOf: paths.launchAgentPlist)
        return try CatalogParser.parseScheduler(
            launchctlOutput: result.standardOutput,
            plistData: plistData,
            errorLogBytes: errorLogBytes
        )
    }

    private func fileSize(_ url: URL) -> Int64 {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        return (attributes?[.size] as? NSNumber)?.int64Value ?? 0
    }

    private func modificationDate(_ url: URL) -> Date? {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        return attributes?[.modificationDate] as? Date
    }
}
