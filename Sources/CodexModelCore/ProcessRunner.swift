import Foundation

public struct ProcessResult: Sendable {
    public let exitCode: Int32
    public let standardOutput: String
    public let standardError: String
}

public enum ProcessRunner {
    public static func run(
        _ executable: URL,
        arguments: [String],
        environment: [String: String]? = nil,
        excludingEnvironmentKeys: [String] = []
    ) throws -> ProcessResult {
        let process = Process()
        let outputPipe = Pipe()
        let errorPipe = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = outputPipe
        process.standardError = errorPipe
        if environment != nil || !excludingEnvironmentKeys.isEmpty {
            var childEnvironment = ProcessInfo.processInfo.environment.merging(environment ?? [:]) { _, new in new }
            for key in excludingEnvironmentKeys { childEnvironment.removeValue(forKey: key) }
            process.environment = childEnvironment
        }
        let output = DataBox()
        let errors = DataBox()
        let readers = DispatchGroup()
        readers.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            output.data = outputPipe.fileHandleForReading.readDataToEndOfFile()
            readers.leave()
        }
        readers.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            errors.data = errorPipe.fileHandleForReading.readDataToEndOfFile()
            readers.leave()
        }
        do {
            try process.run()
        } catch {
            try? outputPipe.fileHandleForWriting.close()
            try? errorPipe.fileHandleForWriting.close()
            readers.wait()
            throw error
        }
        process.waitUntilExit()
        readers.wait()
        return ProcessResult(
            exitCode: process.terminationStatus,
            standardOutput: String(decoding: output.data, as: UTF8.self),
            standardError: String(decoding: errors.data, as: UTF8.self)
        )
    }
}

private final class DataBox: @unchecked Sendable {
    var data = Data()
}
