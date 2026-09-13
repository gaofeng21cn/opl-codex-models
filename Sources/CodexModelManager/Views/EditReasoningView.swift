import SwiftUI

struct EditReasoningView: View {
    @ObservedObject var store: CatalogStore
    let model: CatalogModel
    @Environment(\.dismiss) private var dismiss
    @State private var settings: ReasoningSettings

    init(store: CatalogStore, model: CatalogModel) {
        self.store = store
        self.model = model
        _settings = State(initialValue: model.reasoning)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                Text("编辑推理档位")
                    .font(.title3.weight(.semibold))
                Text(model.displayName)
                    .foregroundStyle(.secondary)
            }
            .padding(20)
            Divider()
            Form {
                ReasoningFields(settings: $settings)
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .disabled(store.isBusy)
            Divider()
            HStack {
                Text("保存前自动备份，随后同步到 Codex。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(store.isBusy)
                Button("保存并同步") {
                    Task {
                        if await store.updateReasoning(settings, for: model.slug) {
                            dismiss()
                        }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!settings.isValid || store.isBusy)
                if store.isSavingReasoning {
                    ProgressView().controlSize(.small)
                }
            }
            .padding(16)
        }
        .frame(width: 560, height: 390)
        .interactiveDismissDisabled(store.isBusy)
    }
}
