// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "CodexModelManager",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "CodexModelManager", targets: ["CodexModelManager"]),
        .executable(name: "CodexModelSync", targets: ["CodexModelSync"])
    ],
    dependencies: [
        .package(url: "https://github.com/LebJe/TOMLKit.git", from: "0.6.0"),
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.10.1")
    ],
    targets: [
        .target(
            name: "CodexModelCore",
            dependencies: [.product(name: "TOMLKit", package: "TOMLKit")],
            path: "Sources/CodexModelCore"
        ),
        .executableTarget(
            name: "CodexModelManager",
            dependencies: [
                "CodexModelCore",
                .product(name: "Sparkle", package: "Sparkle")
            ],
            path: "Sources/CodexModelManager",
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"
                ])
            ]
        ),
        .executableTarget(
            name: "CodexModelSync",
            dependencies: ["CodexModelCore"],
            path: "Sources/CodexModelSync"
        ),
        .testTarget(
            name: "CodexModelManagerTests",
            dependencies: ["CodexModelManager", "CodexModelCore"],
            path: "Tests/CodexModelManagerTests"
        )
    ],
    swiftLanguageVersions: [.v5]
)
