import Foundation

struct CatalogModel: Identifiable, Hashable {
    enum Source: String, Hashable {
        case official
        case custom

        var title: String {
            switch self {
            case .official: "OpenAI 官方"
            case .custom: "自定义"
            }
        }
    }

    let slug: String
    let displayName: String
    let description: String
    let inputModalities: [String]
    let supportsOriginalImageDetail: Bool
    let contextWindow: Int?
    let maxContextWindow: Int?
    let priority: Int?
    let visibility: String?
    let supportedInAPI: Bool
    let reasoning: ReasoningSettings
    let source: Source

    var id: String { slug }
    var modalitiesLabel: String { inputModalities.joined(separator: " + ") }
    var contextSourceTitle: String {
        switch source {
        case .official: "Codex 内置目录"
        case .custom: "本地自定义源"
        }
    }
}
