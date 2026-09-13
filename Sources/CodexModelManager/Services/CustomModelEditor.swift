import Foundation

enum CustomModelEditor {
    static func adding(
        draft: NewModelDraft,
        to sourceData: Data,
        catalogData: Data? = nil,
        existingSlugs: Set<String>
    ) throws -> Data {
        guard isValidSlug(draft.normalizedSlug) else {
            throw AppError.invalidSlug
        }
        guard !existingSlugs.contains(draft.normalizedSlug) else {
            throw AppError.duplicateSlug(draft.normalizedSlug)
        }
        guard
            !draft.templateSlug.isEmpty,
            draft.contextWindow > 0,
            !draft.normalizedDisplayName.isEmpty,
            !draft.normalizedDescription.isEmpty
        else {
            throw AppError.missingTemplate
        }

        guard
            var root = try JSONSerialization.jsonObject(with: sourceData) as? [String: Any],
            var models = root["models"] as? [[String: Any]]
        else {
            throw AppError.missingTemplate
        }
        let templateData = catalogData ?? sourceData
        guard
            let templateRoot = try JSONSerialization.jsonObject(with: templateData) as? [String: Any],
            let templates = templateRoot["models"] as? [[String: Any]],
            var newModel = templates.first(where: { $0["slug"] as? String == draft.templateSlug })
        else {
            throw AppError.missingTemplate
        }

        let nextPriority = models.compactMap { CatalogParser.integer($0["priority"]) }.max().map { $0 + 1 } ?? 1000
        newModel["slug"] = draft.normalizedSlug
        newModel["visibility"] = "list"
        newModel["display_name"] = draft.normalizedDisplayName
        newModel["description"] = draft.normalizedDescription
        newModel["context_window"] = draft.contextWindow
        newModel["max_context_window"] = draft.contextWindow
        newModel["input_modalities"] = draft.supportsImage ? ["text", "image"] : ["text"]
        newModel["supports_image_detail_original"] =
            draft.supportsImage && draft.supportsOriginalImageDetail
        if newModel["supports_reasoning_summaries"] == nil {
            newModel["supports_reasoning_summaries"] = true
        }
        newModel["priority"] = nextPriority
        if let reasoning = draft.reasoning {
            try applyReasoning(reasoning, to: &newModel)
        }

        models.append(newModel)
        root["models"] = models
        guard JSONSerialization.isValidJSONObject(root) else {
            throw AppError.invalidData("新增模型无法序列化")
        }
        return try JSONSerialization.data(
            withJSONObject: root,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        ) + Data([0x0A])
    }

    static func isValidSlug(_ slug: String) -> Bool {
        slug.range(of: #"^[a-z0-9][a-z0-9._-]*$"#, options: .regularExpression) != nil
    }

    static func updatingReasoning(
        _ settings: ReasoningSettings,
        for slug: String,
        in sourceData: Data
    ) throws -> Data {
        guard
            var root = try JSONSerialization.jsonObject(with: sourceData) as? [String: Any],
            var models = root["models"] as? [[String: Any]],
            let index = models.firstIndex(where: { $0["slug"] as? String == slug })
        else {
            throw AppError.invalidData("只能编辑自定义模型源中已有模型的推理配置。")
        }
        try applyReasoning(settings, to: &models[index])
        root["models"] = models
        return try JSONSerialization.data(
            withJSONObject: root,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        ) + Data([0x0A])
    }

    private static func applyReasoning(_ settings: ReasoningSettings, to model: inout [String: Any]) throws {
        guard settings.isValid else {
            throw AppError.invalidData("请至少选择一个推理档位，并将默认档位设为其中之一。")
        }
        let existing = model["supported_reasoning_levels"] as? [[String: Any]] ?? []
        model["supported_reasoning_levels"] = settings.supportedEfforts.map { effort in
            existing.first { $0["effort"] as? String == effort }
                ?? ["effort": effort, "description": "Reasoning effort: \(effort)"]
        }
        model["default_reasoning_level"] = settings.defaultEffort
    }
}
