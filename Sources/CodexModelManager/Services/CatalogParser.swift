import Foundation

enum CatalogParser {
    static func parseModels(customData: Data, catalogData: Data) throws -> [CatalogModel] {
        let customRoot = try object(from: customData)
        let catalogRoot = try object(from: catalogData)
        guard
            let customObjects = customRoot["models"] as? [[String: Any]],
            let catalogObjects = catalogRoot["models"] as? [[String: Any]]
        else {
            throw AppError.invalidData("缺少 models 数组")
        }

        let customSlugs = Set(customObjects.compactMap { $0["slug"] as? String })
        return try catalogObjects.map { raw in
            guard let slug = raw["slug"] as? String, !slug.isEmpty else {
                throw AppError.invalidData("模型缺少 slug")
            }
            return CatalogModel(
                slug: slug,
                displayName: raw["display_name"] as? String ?? slug,
                description: raw["description"] as? String ?? "",
                inputModalities: raw["input_modalities"] as? [String] ?? ["text"],
                supportsOriginalImageDetail: raw["supports_image_detail_original"] as? Bool ?? false,
                contextWindow: integer(raw["context_window"]),
                maxContextWindow: integer(raw["max_context_window"]),
                priority: integer(raw["priority"]),
                visibility: raw["visibility"] as? String,
                supportedInAPI: raw["supported_in_api"] as? Bool ?? false,
                reasoning: ReasoningSettings(model: raw),
                source: customSlugs.contains(slug) ? .custom : .official
            )
        }
    }

    static func parseRecords(_ text: String) -> [SyncRecord] {
        let formatter = ISO8601DateFormatter()
        let lines = text.split(whereSeparator: \.isNewline)
        return lines.enumerated().compactMap { index, line in
            guard
                let data = String(line).data(using: .utf8),
                let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let status = raw["status"] as? String
            else {
                return nil
            }

            let timestamp = (raw["recorded_at"] as? String).flatMap(formatter.date(from:))
            return SyncRecord(
                id: index,
                recordedAt: timestamp,
                status: status,
                appVersion: raw["app_version"] as? String ?? "未知版本",
                bundledCount: integer(raw["bundled_count"]) ?? 0,
                customCount: integer(raw["custom_count"]) ?? 0,
                currentHash: raw["current_hash"] as? String,
                desiredHash: raw["desired_hash"] as? String,
                backupPath: raw["backup_path"] as? String
            )
        }
        .reversed()
    }

    static func parseScheduler(
        launchctlOutput: String,
        plistData: Data,
        errorLogBytes: Int64
    ) throws -> SchedulerSnapshot {
        var snapshot = SchedulerSnapshot()
        snapshot.isLoaded = !launchctlOutput.isEmpty
        snapshot.errorLogBytes = errorLogBytes

        for rawLine in launchctlOutput.split(whereSeparator: \.isNewline) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("state = ") && snapshot.state == "unknown" {
                snapshot.state = String(line.dropFirst("state = ".count))
            } else if line.hasPrefix("runs = ") {
                snapshot.runs = Int(line.dropFirst("runs = ".count))
            } else if line.hasPrefix("last exit code = ") {
                snapshot.lastExitCode = Int(line.dropFirst("last exit code = ".count))
            } else if line.hasPrefix("run interval = ") {
                let value = line.dropFirst("run interval = ".count).split(separator: " ").first
                snapshot.intervalSeconds = value.flatMap { Int($0) }
            }
        }

        if
            let plist = try PropertyListSerialization.propertyList(from: plistData, format: nil)
                as? [String: Any]
        {
            snapshot.intervalSeconds = plist["StartInterval"] as? Int ?? snapshot.intervalSeconds
            snapshot.watchPaths = plist["WatchPaths"] as? [String] ?? []
        }
        return snapshot
    }

    private static func object(from data: Data) throws -> [String: Any] {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw AppError.invalidData("根对象不是 JSON object")
        }
        return object
    }

    static func integer(_ value: Any?) -> Int? {
        if let value = value as? Int { return value }
        if let value = value as? NSNumber { return value.intValue }
        return nil
    }
}
