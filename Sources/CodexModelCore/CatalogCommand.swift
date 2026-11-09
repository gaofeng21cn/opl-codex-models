import Foundation

/// Machine commands are a thin adapter over the same source editor used by the GUI.
public enum CatalogCommand {
    public static func run(_ arguments: [String]) throws -> Data {
        var args = arguments
        let jsonIndex = args.firstIndex(of: "--json")
        if let jsonIndex { args.remove(at: jsonIndex) }
        let config: URL
        if let index = args.firstIndex(of: "--config") {
            guard args.indices.contains(index + 1) else {
                throw CatalogCommandError.invalidArguments("--config requires a path")
            }
            config = URL(fileURLWithPath: args[index + 1])
            args.removeSubrange(index...(index + 1))
        } else { config = AppConfiguration.defaultURL }
        let command = args.isEmpty ? "sync" : args.removeFirst()
        if command == "help" || command == "--help" {
            return try json([
                "schema": "codex_models.cli.v1", "status": "help",
                "commands": ["read", "list", "preview add|remove|reasoning", "add", "remove", "reasoning", "update", "sync"],
                "options": ["--config PATH", "--json", "--expected-source-digest SHA256", "--slug SLUG", "--template SLUG", "--name NAME", "--description TEXT", "--context-window INTEGER", "--image", "--original-image-detail", "--efforts low,high", "--default-effort high"],
                "mutation_scope": "Custom source only; run sync separately to update the Codex catalog."
            ])
        }
        if command == "sync" {
            guard args.isEmpty else { throw CatalogCommandError.invalidArguments("sync accepts only --config and --json") }
            let service = CatalogSyncService(paths: try AppConfiguration.load(from: config))
            try service.clearErrorLog()
            return try service.sync().recordData
        }
        let service = try CatalogSourceService(configuration: AppConfiguration.read(from: config))
        if command == "read" || command == "list" {
            guard args.isEmpty else { throw CatalogCommandError.invalidArguments("read accepts only --config and --json") }
            var result = try service.read()
            result["operation"] = command
            return try json(result)
        }
        let preview = command == "preview"
        let action = preview && !args.isEmpty ? args.removeFirst() : command
        guard ["add", "remove", "reasoning", "update"].contains(action) else {
            throw CatalogCommandError.invalidArguments("Unknown command; use --help")
        }
        var options: [String: String] = [:]
        let allowed = Set(["--expected-source-digest", "--slug"] + (action == "add"
            ? ["--template", "--name", "--description", "--context-window", "--image", "--original-image-detail", "--efforts", "--default-effort"]
            : action == "remove" ? [] : ["--efforts", "--default-effort"]))
        while !args.isEmpty {
            let key = args.removeFirst()
            guard allowed.contains(key), options[key] == nil else {
                throw CatalogCommandError.invalidArguments("Unknown or duplicate option: \(key)")
            }
            if key == "--image" || key == "--original-image-detail" { options[key] = "true" }
            else {
                guard !args.isEmpty, !args[0].hasPrefix("--") else {
                    throw CatalogCommandError.invalidArguments("\(key) requires a value")
                }
                options[key] = args.removeFirst()
            }
        }
        func required(_ key: String) throws -> String {
            guard let value = options[key], !value.isEmpty else {
                throw CatalogCommandError.invalidArguments("\(key) is required")
            }
            return value
        }
        let expected = options["--expected-source-digest"]
        if !preview { _ = try required("--expected-source-digest") }
        let slug = try required("--slug")
        let transform: (Data, Data?) throws -> Data
        switch action {
        case "add":
            var draft = NewModelDraft()
            draft.slug = slug
            draft.templateSlug = try required("--template")
            draft.displayName = try required("--name")
            draft.description = try required("--description")
            if let value = options["--context-window"] {
                guard let count = Int(value), count > 0 else { throw CatalogCommandError.invalidArguments("Invalid context window") }
                draft.contextWindow = count
            }
            draft.supportsImage = options["--image"] != nil
            draft.supportsOriginalImageDetail = options["--original-image-detail"] != nil
            if options["--efforts"] != nil || options["--default-effort"] != nil {
                draft.reasoning = ReasoningSettings(
                    supportedEfforts: try required("--efforts").components(separatedBy: ","),
                    defaultEffort: try required("--default-effort"))
            }
            transform = { source, catalog in
                try CustomModelEditor.adding(draft: draft, to: source, catalogData: catalog,
                    existingSlugs: CatalogSourceService.slugs(source, catalog))
            }
        case "remove":
            transform = { source, _ in try CustomModelEditor.removing(slug: slug, from: source) }
        default:
            let settings = ReasoningSettings(
                supportedEfforts: try required("--efforts").components(separatedBy: ","),
                defaultEffort: try required("--default-effort"))
            transform = { source, _ in try CustomModelEditor.updatingReasoning(settings, for: slug, in: source) }
        }
        var result = try preview
            ? service.preview(expectedDigest: expected, transform: transform)
            : service.edit(expectedDigest: expected, transform: transform)
        result["operation"] = preview ? "preview.\(action)" : action
        return try json(result)
    }

    public static func errorData(_ error: Error) -> Data {
        var detail: [String: Any] = ["code": "operation_failed", "message": error.localizedDescription]
        if let commandError = error as? CatalogCommandError {
            detail["code"] = commandError.code
            if case .sourceConflict(let expected, let actual) = commandError {
                detail["expected_source_digest"] = expected
                detail["source_digest"] = actual
            }
        } else if case CoreError.invalidCatalog = error { detail["code"] = "invalid_catalog" }
        return (try? json(["schema": "codex_models.cli.v1", "status": "error", "error": detail,
                           "next_action": "read_current_source_before_retry"])) ?? Data()
    }

    private static func json(_ value: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes])
    }
}
