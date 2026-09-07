import AppKit
import CodexModelCore
import SwiftUI

struct SettingsView: View {
    @ObservedObject var store: CatalogStore
    @State private var draft = ConfigurationDraft()
    @State private var loaded = false
    @State private var runtimes: [CodexRuntime] = []

    var body: some View {
        TabView {
            catalogSettings
                .tabItem { Label("模型目录", systemImage: "square.stack.3d.up") }
            ContextManagementView(store: store)
                .tabItem { Label("实验功能", systemImage: "flask") }
        }
        .frame(width: 680, height: 580)
        .task {
            runtimes = await Task.detached { CodexRuntimeLocator.discover() }.value
        }
    }

    private var catalogSettings: some View {
        Form {
            Section("Codex") {
                LabeledContent("运行时") {
                    HStack(spacing: 8) {
                        Picker("运行时", selection: $draft.codexRuntimePath) {
                            Text("自动选择最新版本").tag("")
                            ForEach(runtimes) { runtime in
                                Text("\(runtime.version) · \(runtime.url.path)").tag(runtime.url.path)
                            }
                            if !draft.codexRuntimePath.isEmpty,
                               !runtimes.contains(where: { $0.url.path == draft.codexRuntimePath }) {
                                Text(draft.codexRuntimePath).tag(draft.codexRuntimePath)
                            }
                        }
                        .labelsHidden()
                        Button {
                            chooseRuntime()
                        } label: {
                            Image(systemName: "folder")
                        }
                        .help("选择 Codex 运行时")
                    }
                }

                LabeledContent("合并模型目录") {
                    TextField("模型目录", text: $draft.mergedCatalogPath)
                        .multilineTextAlignment(.trailing)
                }

                LabeledContent("自定义模型源") {
                    TextField("自定义模型源", text: $draft.customSourcePath)
                        .multilineTextAlignment(.trailing)
                }
            }

            Section("自动同步") {
                LabeledContent("执行周期", value: "每天")
                LabeledContent("任务标识") {
                    TextField("任务标识", text: $draft.launchAgentLabel)
                        .multilineTextAlignment(.trailing)
                }
                LabeledContent("任务配置") {
                    TextField("LaunchAgent", text: $draft.launchAgentPlistPath)
                        .multilineTextAlignment(.trailing)
                }
            }

            DisclosureGroup("日志与备份") {
                LabeledContent("同步日志") {
                    TextField("同步日志", text: $draft.syncLogPath)
                        .multilineTextAlignment(.trailing)
                }
                LabeledContent("错误日志") {
                    TextField("错误日志", text: $draft.errorLogPath)
                        .multilineTextAlignment(.trailing)
                }
                LabeledContent("备份目录") {
                    TextField("备份目录", text: $draft.backupDirectoryPath)
                        .multilineTextAlignment(.trailing)
                }
            }

            HStack {
                Button("恢复推荐值") {
                    loadRecommended()
                }
                Spacer()
                Button {
                    Task { _ = await store.applyConfiguration(draft.configuration) }
                } label: {
                    if store.isApplyingConfiguration {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Text("保存并应用")
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!draft.isComplete || store.isBusy)
            }
        }
        .formStyle(.grouped)
        .onAppear {
            guard !loaded else { return }
            loaded = true
            if let configuration = store.configuration {
                draft = ConfigurationDraft(configuration: configuration)
            } else {
                loadRecommended()
            }
        }
        .onChange(of: store.configuration) { _, configuration in
            if let configuration {
                draft = ConfigurationDraft(configuration: configuration)
            }
        }
    }

    private func loadRecommended() {
        do {
            draft = ConfigurationDraft(configuration: try store.recommendedConfiguration())
        } catch {
            store.errorMessage = error.localizedDescription
        }
    }

    private func chooseRuntime() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.prompt = "选择"
        panel.message = "选择 Codex 可执行文件"
        if panel.runModal() == .OK, let url = panel.url {
            draft.codexRuntimePath = url.path
        }
    }
}
