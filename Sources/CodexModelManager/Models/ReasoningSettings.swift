import Foundation

struct ReasoningSettings: Equatable, Hashable {
    static let knownEfforts = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

    var supportedEfforts: [String]
    var defaultEffort: String

    init(supportedEfforts: [String] = [], defaultEffort: String = "") {
        self.supportedEfforts = supportedEfforts
        self.defaultEffort = defaultEffort
    }

    init(model: [String: Any]) {
        supportedEfforts = (model["supported_reasoning_levels"] as? [[String: Any]])?
            .compactMap { $0["effort"] as? String } ?? []
        defaultEffort = model["default_reasoning_level"] as? String ?? ""
    }

    var availableEfforts: [String] {
        Self.knownEfforts + supportedEfforts.filter { !Self.knownEfforts.contains($0) }
    }

    var isValid: Bool {
        !supportedEfforts.isEmpty
            && supportedEfforts.allSatisfy { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            && Set(supportedEfforts).count == supportedEfforts.count
            && supportedEfforts.contains(defaultEffort)
    }

    mutating func setEnabled(_ enabled: Bool, for effort: String) {
        if enabled, !supportedEfforts.contains(effort) {
            supportedEfforts.append(effort)
            let order = availableEfforts
            supportedEfforts.sort { order.firstIndex(of: $0)! < order.firstIndex(of: $1)! }
        } else if !enabled {
            supportedEfforts.removeAll { $0 == effort }
        }
        if !supportedEfforts.contains(defaultEffort) {
            defaultEffort = supportedEfforts.last ?? ""
        }
    }
}
