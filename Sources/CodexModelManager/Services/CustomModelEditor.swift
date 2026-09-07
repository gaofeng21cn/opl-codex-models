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
}
