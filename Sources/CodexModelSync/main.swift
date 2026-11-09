import CodexModelCore
import Darwin
import Foundation

do {
    let result = try CatalogCommand.run(Array(CommandLine.arguments.dropFirst()))
    FileHandle.standardOutput.write(result + Data([0x0A]))
} catch {
    if CommandLine.arguments.contains("--json") {
        FileHandle.standardOutput.write(CatalogCommand.errorData(error) + Data([0x0A]))
    } else {
        FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
    }
    exit(EXIT_FAILURE)
}
