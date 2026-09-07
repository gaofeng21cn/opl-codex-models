import CodexModelCore
import SwiftUI

struct ModelListView: View {
    @ObservedObject var store: CatalogStore
    let title: String
    let subtitle: String
    let models: [CatalogModel]
    var addAction: (() -> Void)?

    @State private var searchText = ""
    @State private var selection: CatalogModel.ID?

    private var filteredModels: [CatalogModel] {
        guard !searchText.isEmpty else { return models }
        return models.filter {
            $0.slug.localizedCaseInsensitiveContains(searchText)
                || $0.displayName.localizedCaseInsensitiveContains(searchText)
                || $0.description.localizedCaseInsensitiveContains(searchText)
        }
    }

    private var selectedModel: CatalogModel? {
        guard let selection else { return filteredModels.first }
        return filteredModels.first { $0.id == selection }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.title2.weight(.semibold))
                    Text(subtitle)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Spacer()
                if let addAction {
                    Button(action: addAction) {
                        Label("新增模型", systemImage: "plus")
                    }
                }
            }
            .padding(20)

            Divider()

            HSplitView {
                Table(filteredModels, selection: $selection) {
                    TableColumn("模型") { model in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(model.displayName)
                                .fontWeight(.medium)
                                .lineLimit(1)
                            Text(model.slug)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                        .padding(.vertical, 3)
                    }
                    .width(min: 210, ideal: 260)

                    TableColumn("来源") { model in
                        Text(model.source.title)
                    }
                    .width(min: 90, ideal: 110, max: 130)

                    TableColumn("输入") { model in
                        Text(model.modalitiesLabel)
                    }
                    .width(min: 80, ideal: 100, max: 130)

                    TableColumn("当前上下文") { model in
                        Text(AppFormatters.tokenCount(model.contextWindow))
                            .monospacedDigit()
                    }
                    .width(min: 70, ideal: 90, max: 110)

                    TableColumn("优先级") { model in
                        Text(model.priority.map(String.init) ?? "—")
                            .monospacedDigit()
                    }
                    .width(min: 60, ideal: 70, max: 85)
                }
                .frame(minWidth: 620)
                .clipped()

                ModelDetailView(store: store, model: selectedModel)
                    .frame(minWidth: 300, idealWidth: 320, maxWidth: 380)
                    .clipped()
            }
            .frame(minWidth: 920)
        }
        .searchable(text: $searchText, placement: .toolbar, prompt: "搜索模型")
        .navigationTitle(title)
    }
}

private struct ModelDetailView: View {
    @ObservedObject var store: CatalogStore
    let model: CatalogModel?

    var body: some View {
        if let model {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(model.displayName)
                            .font(.title3.weight(.semibold))
                            .lineLimit(2)
                        Text(model.slug)
                            .font(.callout.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                    }

                    StatusBadge(
                        title: model.source.title,
                        color: model.source == .official ? .blue : .orange,
                        systemImage: model.source == .official ? "building.columns" : "slider.horizontal.3"
                    )

                    Text(model.description.isEmpty ? "没有说明。" : model.description)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)

                    Divider()

                    DetailRow(title: "输入能力", value: model.modalitiesLabel)
                    DetailRow(title: "当前上下文", value: AppFormatters.tokenCount(model.contextWindow))
                    DetailRow(title: "最大上下文", value: AppFormatters.tokenCount(model.maxContextWindow))
                    DetailRow(title: "上下文来源", value: model.contextSourceTitle)
                    DetailRow(title: "优先级", value: model.priority.map(String.init) ?? "未知")
                    DetailRow(title: "目录可见性", value: model.visibility ?? "未知")
                    Picker("模型选择器", selection: Binding(
                        get: { store.configuration?.visibilityOverrides?[model.slug]?.rawValue ?? "source" },
                        set: { value in
                            Task { await store.setVisibility(ModelVisibility(rawValue: value), for: model.slug) }
                        }
                    )) {
                        Text("跟随来源").tag("source")
                        Text("显示").tag("list")
                        Text("隐藏").tag("hide")
                    }
                    .disabled(store.isBusy)
                    DetailRow(title: "API 支持", value: model.supportedInAPI ? "是" : "否")
                    DetailRow(
                        title: "原始图像 detail",
                        value: model.supportsOriginalImageDetail ? "支持" : "不支持"
                    )
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            ContentUnavailableView("没有模型", systemImage: "square.stack.3d.up.slash")
        }
    }
}

struct DetailRow: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .lineLimit(2)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
