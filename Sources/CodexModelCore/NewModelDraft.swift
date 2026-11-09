import Foundation

public struct NewModelDraft {
    public init() {}
    public var slug = ""
    public var displayName = ""
    public var description = ""
    public var templateSlug = ""
    public var contextWindow = 262_144
    public var supportsImage = false
    public var supportsOriginalImageDetail = false
    public var reasoning: ReasoningSettings?

    public var normalizedSlug: String {
        slug.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    public var normalizedDisplayName: String {
        displayName.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var normalizedDescription: String {
        description.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
