import CodexModelCore
import SwiftUI

struct ContextManagementView: View {
    @ObservedObject var store: CatalogStore
    @State private var settings: ContextManagementSettings?
    @State private var enabled = false
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        Form {
            Section("上下文管理") {
                Toggle("实验性上下文管理", isOn: Binding(
                    get: { enabled },
                    set: { value in Task { await save(value) } }
                ))
                .disabled(settings == nil || saving || store.isBusy || store.paths == nil)
                LabeledContent("配置状态", value: saving ? "正在保存" : (enabled ? "已开启" : "已关闭"))
                LabeledContent("默认供应商", value: settings?.provider ?? "未知")
                LabeledContent("线路支持", value: supportStatus)
                LabeledContent("会话生效状态", value: "未验证")
                if settings != nil, enabled {
                    Label("待 Codex 会话重新加载配置", systemImage: "arrow.clockwise")
                        .foregroundStyle(.secondary)
                }
            }
            Section {
                Link("官方支持条件", destination: URL(string: "https://learn.chatgpt.com/docs/config-file/config-reference")!)
                Button {
                    store.reveal(ContextManagementSettings.configurationURL)
                } label: {
                    Label("打开 Codex 配置", systemImage: "doc.text")
                }
            }
            if let error {
                Text(error).foregroundStyle(.red).textSelection(.enabled)
            }
        }
        .formStyle(.grouped)
        .task { await reload() }
    }

    private var supportStatus: String {
        guard let settings else { return "未知" }
        return settings.provider == "openai"
            ? "需 ChatGPT 登录及符合条件的订阅"
            : "自定义供应商，官方暂未支持"
    }

    private func reload() async {
        do {
            let current = try await Task.detached { try ContextManagementSettings.read() }.value
            settings = current
            enabled = current.enabled
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func save(_ value: Bool) async {
        guard let backups = store.paths?.backupDirectory, !saving else { return }
        saving = true
        defer { saving = false }
        do {
            try await Task.detached {
                try ContextManagementSettings.setEnabled(value, backupDirectory: backups)
            }.value
            await reload()
        } catch { self.error = error.localizedDescription }
    }
}
