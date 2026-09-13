import SwiftUI

struct AddModelView: View {
    @ObservedObject var store: CatalogStore
    let templates: [CatalogModel]

    @Environment(\.dismiss) private var dismiss
    @State private var draft = NewModelDraft()

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("新增自定义模型")
                        .font(.title3.weight(.semibold))
                    Text("选择模板，再按实际能力调整上下文、图像和推理配置。")
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(20)

            Divider()

            Form {
                Section("标识") {
                    TextField("slug", text: $draft.slug, prompt: Text("vendor-model-name"))
                        .textContentType(.none)
                    TextField("显示名称", text: $draft.displayName)
                    TextField("说明", text: $draft.description, axis: .vertical)
                        .lineLimit(2...4)
                }

                Section("能力模板") {
                    Picker("继承自", selection: $draft.templateSlug) {
                        Text("请选择").tag("")
                        ForEach(templates) { model in
                            Text(model.displayName).tag(model.slug)
                        }
                    }

                    LabeledContent("上下文窗口") {
                        HStack(spacing: 8) {
                            Image(systemName: "info.circle")
                                .foregroundStyle(.secondary)
                                .help("默认复制所选本地模板；标准模型列表接口不提供上下文窗口。保存时当前值与最大值相同。")
                            TextField("", value: $draft.contextWindow, format: .number)
                                .labelsHidden()
                                .multilineTextAlignment(.trailing)
                                .monospacedDigit()
                                .frame(width: 120)
                            Stepper(
                                "",
                                value: $draft.contextWindow,
                                in: 1...2_000_000,
                                step: 1_024
                            )
                            .labelsHidden()
                        }
                        .frame(maxWidth: .infinity, alignment: .trailing)
                    }

                    Toggle("支持图像输入", isOn: $draft.supportsImage)
                    Toggle(
                        "支持 detail: original",
                        isOn: $draft.supportsOriginalImageDetail
                    )
                    .disabled(!draft.supportsImage)
                }

                Section("推理配置") {
                    Toggle("单独设置推理档位", isOn: Binding(
                        get: { draft.reasoning != nil },
                        set: { enabled in
                            draft.reasoning = enabled
                                ? templates.first(where: { $0.slug == draft.templateSlug })?.reasoning ?? ReasoningSettings()
                                : nil
                        }
                    ))
                    if draft.reasoning != nil {
                        ReasoningFields(settings: Binding(
                            get: { draft.reasoning ?? ReasoningSettings() },
                            set: { draft.reasoning = $0 }
                        ))
                    } else {
                        Text("沿用模板中的推理档位和默认值。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .disabled(store.isBusy)

            Divider()

            HStack {
                Text("保存前会备份自定义模型源文件。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("取消", role: .cancel) {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                .disabled(store.isBusy)

                Button {
                    Task {
                        if await store.addModel(draft) {
                            dismiss()
                        }
                    }
                } label: {
                    if store.isAddingModel {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Label("新增并同步", systemImage: "plus")
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSave || store.isBusy)
            }
            .padding(16)
        }
        .frame(width: 590, height: 640)
        .interactiveDismissDisabled(store.isBusy)
        .onAppear {
            if let template = templates.last {
                draft.templateSlug = template.slug
                applyTemplate(template)
            }
        }
        .onChange(of: draft.templateSlug) { _, slug in
            if let template = templates.first(where: { $0.slug == slug }) {
                applyTemplate(template)
            }
        }
        .onChange(of: draft.supportsImage) { _, supportsImage in
            if !supportsImage {
                draft.supportsOriginalImageDetail = false
            }
        }
    }

    private var canSave: Bool {
        CustomModelEditor.isValidSlug(draft.normalizedSlug)
            && !draft.normalizedDisplayName.isEmpty
            && !draft.normalizedDescription.isEmpty
            && !draft.templateSlug.isEmpty
            && draft.contextWindow > 0
            && (draft.reasoning?.isValid ?? true)
    }

    private func applyTemplate(_ template: CatalogModel) {
        draft.contextWindow = template.contextWindow ?? draft.contextWindow
        draft.supportsImage = template.inputModalities.contains("image")
        draft.supportsOriginalImageDetail = template.supportsOriginalImageDetail
        draft.reasoning = template.reasoning.supportedEfforts.isEmpty ? nil : template.reasoning
    }
}
