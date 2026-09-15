import CodexModelCore
import SwiftUI

struct EditContextOverrideView: View {
    @ObservedObject var store: CatalogStore
    let model: CatalogModel
    @Environment(\.dismiss) private var dismiss
    @State private var overridesCurrent: Bool
    @State private var overridesMaximum: Bool
    @State private var currentText: String
    @State private var maximumText: String
    @State private var saveError: String?

    init(store: CatalogStore, model: CatalogModel) {
        self.store = store
        self.model = model
        let override = store.configuration?.modelOverrides?[model.slug]
        _overridesCurrent = State(initialValue: override?.contextWindow != nil)
        _overridesMaximum = State(initialValue: override?.maxContextWindow != nil)
        _currentText = State(initialValue: (override?.contextWindow ?? model.contextWindow).map(String.init) ?? "")
        _maximumText = State(initialValue: (override?.maxContextWindow ?? model.maxContextWindow).map(String.init) ?? "")
    }

    private var override: ModelFieldOverrides {
        ModelFieldOverrides(
            contextWindow: overridesCurrent ? Int(currentText) : nil,
            maxContextWindow: overridesMaximum ? Int(maximumText) : nil
        )
    }

    private var isValid: Bool {
        (!overridesCurrent || (Int(currentText) ?? 0) > 0)
            && (!overridesMaximum || (Int(maximumText) ?? 0) > 0)
            && (try? override.validate()) != nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                Text("编辑上下文覆盖").font(.title3.weight(.semibold))
                Text(model.displayName).foregroundStyle(.secondary)
                Text("只保留勾选字段的本机值，其他配置继续跟随官方目录。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .padding(20)
            Divider()
            Form {
                Section {
                    Toggle("覆盖当前上下文", isOn: $overridesCurrent)
                    contextField("当前上下文", text: $currentText, enabled: overridesCurrent)
                    Toggle("覆盖最大上下文", isOn: $overridesMaximum)
                    contextField("最大上下文", text: $maximumText, enabled: overridesMaximum)
                } footer: {
                    Text("单位为 token；384K = 393216。未勾选的字段在下次同步时使用官方值。")
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .disabled(store.isBusy)
            if let saveError {
                Text(saveError).foregroundStyle(.red).font(.callout).padding(.horizontal, 20)
            }
            Divider()
            HStack {
                Button("全部跟随官方") {
                    overridesCurrent = false
                    overridesMaximum = false
                }
                Spacer()
                if store.isApplyingConfiguration { ProgressView().controlSize(.small) }
                Button("取消", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Button("保存并同步") {
                    Task {
                        if await store.setContextOverride(override, for: model.slug) {
                            dismiss()
                        } else {
                            saveError = store.errorMessage ?? "保存失败，请重试。"
                        }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!isValid)
            }
            .disabled(store.isBusy)
            .padding(16)
        }
        .frame(width: 560, height: 440)
        .interactiveDismissDisabled(store.isBusy)
    }

    private func contextField(_ title: String, text: Binding<String>, enabled: Bool) -> some View {
        HStack {
            Text(title)
            Spacer()
            TextField(title, text: text)
                .labelsHidden()
                .multilineTextAlignment(.trailing)
                .monospacedDigit()
                .frame(width: 170)
                .disabled(!enabled)
                .accessibilityLabel(title)
        }
    }
}
