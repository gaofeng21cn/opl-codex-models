import SwiftUI

struct ContentView: View {
    @ObservedObject var store: CatalogStore
    @SceneStorage("selectedDestination") private var selectedRawValue =
        SidebarDestination.overview.rawValue

    private var selection: Binding<SidebarDestination?> {
        Binding(
            get: { SidebarDestination(rawValue: selectedRawValue) },
            set: { selectedRawValue = ($0 ?? .overview).rawValue }
        )
    }

    var body: some View {
        Group {
            if store.isConfigured {
                configuredContent
            } else {
                SetupView(store: store)
            }
        }
        .alert(
            "操作未完成",
            isPresented: Binding(
                get: { store.errorMessage != nil },
                set: { if !$0 { store.errorMessage = nil } }
            )
        ) {
            Button("好", role: .cancel) {
                store.errorMessage = nil
            }
        } message: {
            Text(store.errorMessage ?? "")
        }
    }

    private var configuredContent: some View {
        NavigationSplitView {
            SidebarView(
                selection: selection,
                scheduler: store.snapshot.scheduler,
                lastRunAt: store.snapshot.lastRunAt
            )
            .frame(minWidth: 200)
            .navigationSplitViewColumnWidth(min: 200, ideal: 220, max: 280)
            .clipped()
        } detail: {
            detailView
                .toolbar {
                    ToolbarItemGroup {
                        Button {
                            Task { await store.refresh() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .help("刷新状态")
                        .disabled(store.isRefreshing)

                        Button {
                            Task { await store.syncNow() }
                        } label: {
                            if store.isSyncing {
                                ProgressView()
                                    .controlSize(.small)
                            } else {
                                Image(systemName: "arrow.trianglehead.2.clockwise.rotate.90")
                            }
                        }
                        .help("立即运行同步任务")
                        .disabled(store.isSyncing || !store.isConfigured)

                        Button {
                            store.isPresentingAddModel = true
                        } label: {
                            Image(systemName: "plus")
                        }
                        .help("新增自定义模型")
                        .disabled(!store.isConfigured)

                        SettingsLink {
                            Image(systemName: "gearshape")
                        }
                        .help("设置")
                    }
                }
        }
        .navigationSplitViewStyle(.balanced)
        .task {
            await store.refresh()
        }
        .sheet(isPresented: $store.isPresentingAddModel) {
            AddModelView(
                store: store,
                templates: store.snapshot.models
            )
        }
    }

    @ViewBuilder
    private var detailView: some View {
        switch SidebarDestination(rawValue: selectedRawValue) ?? .overview {
        case .overview:
            OverviewView(store: store)
        case .allModels:
            ModelListView(
                store: store,
                title: "全部模型",
                subtitle: "OpenAI 官方目录与本机自定义模型的当前合并结果",
                models: store.snapshot.models
            )
        case .customModels:
            ModelListView(
                store: store,
                title: "自定义模型",
                subtitle: "由权威自定义源维护，优先级位于官方模型之后",
                models: store.snapshot.customModels,
                addAction: { store.isPresentingAddModel = true }
            )
        case .logs:
            SyncLogView(records: store.snapshot.records)
        }
    }
}
