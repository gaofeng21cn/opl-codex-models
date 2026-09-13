import Foundation

struct NewModelDraft {
    var slug = ""
    var displayName = ""
    var description = ""
    var templateSlug = ""
    var contextWindow = 262_144
    var supportsImage = false
    var supportsOriginalImageDetail = false
    var reasoning: ReasoningSettings?

    var normalizedSlug: String {
        slug.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    var normalizedDisplayName: String {
        displayName.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var normalizedDescription: String {
        description.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
