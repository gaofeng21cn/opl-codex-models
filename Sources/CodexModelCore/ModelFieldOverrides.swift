import Foundation

/// Only these fields may override the freshly loaded official model.
public struct ModelFieldOverrides: Codable, Equatable, Sendable {
    public var contextWindow: Int?
    public var maxContextWindow: Int?

    public init(contextWindow: Int? = nil, maxContextWindow: Int? = nil) {
        self.contextWindow = contextWindow
        self.maxContextWindow = maxContextWindow
    }

    enum CodingKeys: String, CodingKey {
        case contextWindow = "context_window"
        case maxContextWindow = "max_context_window"
    }

    public var isEmpty: Bool { contextWindow == nil && maxContextWindow == nil }

    public var fields: [String: Int] {
        var result: [String: Int] = [:]
        if let contextWindow { result["context_window"] = contextWindow }
        if let maxContextWindow { result["max_context_window"] = maxContextWindow }
        return result
    }

    public func validate() throws {
        guard fields.values.allSatisfy({ $0 > 0 }) else {
            throw CoreError.invalidConfiguration("本机覆盖的上下文必须是正整数。")
        }
        if let contextWindow, let maxContextWindow, contextWindow > maxContextWindow {
            throw CoreError.invalidConfiguration("当前上下文不能大于最大上下文。")
        }
    }

    func applying(to model: [String: Any]) throws -> [String: Any] {
        try validate()
        var result = model
        for (field, value) in fields { result[field] = value }
        if !isEmpty,
           let current = result["context_window"] as? Int,
           let maximum = result["max_context_window"] as? Int,
           current > maximum {
            throw CoreError.invalidConfiguration("本机覆盖与官方目录合并后，当前上下文大于最大上下文。请调整该模型的覆盖值。")
        }
        return result
    }
}
