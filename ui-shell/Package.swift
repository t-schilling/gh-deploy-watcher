// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "GhDeployWatcher",
    platforms: [.macOS(.v12)],
    targets: [
        .executableTarget(name: "GhDeployWatcherUI", path: "Sources/GhDeployWatcherUI")
    ]
)
