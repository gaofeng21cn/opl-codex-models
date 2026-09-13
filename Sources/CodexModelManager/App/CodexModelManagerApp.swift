import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
}

@main
struct CodexModelManagerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = CatalogStore()

    var body: some Scene {
        WindowGroup("Codex Models") {
            ContentView(store: store)
                .frame(minWidth: 1_280, minHeight: 680)
        }
        .defaultSize(width: 1_320, height: 760)
        .commands {
            CommandMenu("模型目录") {
                Button("刷新") {
                    Task { await store.refresh() }
                }
                .keyboardShortcut("r")

                Button("立即同步") {
                    Task { await store.syncNow() }
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                .disabled(store.isSyncing || !store.isConfigured)

                Divider()

                Button("新增自定义模型") {
                    store.isPresentingAddModel = true
                }
                .keyboardShortcut("n")
                .disabled(!store.isConfigured)
            }
        }

        Settings {
            SettingsView(store: store)
        }
    }
}
