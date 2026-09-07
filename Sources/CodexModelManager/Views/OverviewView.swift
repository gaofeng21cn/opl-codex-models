import SwiftUI

struct OverviewView: View {
    @ObservedObject var store: CatalogStore

    private let metricColumns = [
        GridItem(.flexible(minimum: 150), spacing: 12),
        GridItem(.flexible(minimum: 150), spacing: 12),
        GridItem(.flexible(minimum: 150), spacing: 12),
        GridItem(.flexible(minimum: 150), spacing: 12)
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header

                LazyVGrid(columns: metricColumns, spacing: 12) {
                    MetricTile(
                        title: "OpenAI 官方",
                        value: String(store.snapshot.officialModels.count),
                        icon: "building.columns"
                    )
                    MetricTile(
                        title: "自定义",
                        value: String(store.snapshot.customModels.count),
                        icon: "slider.horizontal.3"
                    )
                    MetricTile(
                        title: "当前总数",
                        value: String(store.snapshot.models.count),
                        icon: "square.stack.3d.up"
                    )
                    MetricTile(
                        title: "累计执行",
                        value: store.snapshot.scheduler.runs.map(String.init) ?? "未知",
                        icon: "clock.arrow.trianglehead.counterclockwise.rotate.90"
                    )
                }

                schedulerSection
                recentRunsSection
                filesSection
            }
            .padding(24)
            .frame(maxWidth: 1_080, alignment: .leading)
        }
        .navigationTitle("概览")
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Codex 模型目录")
                    .font(.title2.weight(.semibold))
                Text(store.snapshot.latestRecord?.appVersion ?? "等待首次同步")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            StatusBadge(
                title: store.snapshot.scheduler.isHealthy ? "同步正常" : "需要检查",
                color: store.snapshot.scheduler.isHealthy ? .green : .orange,
                systemImage: store.snapshot.scheduler.isHealthy
                    ? "checkmark.circle.fill"
                    : "exclamationmark.triangle.fill"
            )
        }
    }

    private var schedulerSection: some View {
        GroupBox {
            Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 12) {
                GridRow {
                    InfoLabel(title: "最后执行", value: lastRunText)
                    InfoLabel(
                        title: "最近结果",
                        value: statusTitle(store.snapshot.latestRecord?.status)
                    )
                    InfoLabel(
                        title: "退出码",
                        value: store.snapshot.scheduler.lastExitCode.map(String.init) ?? "未知"
                    )
                }
                GridRow {
                    InfoLabel(
                        title: "Codex 版本",
                        value: store.snapshot.latestRecord?.appVersion ?? "未知"
                    )
                    InfoLabel(
                        title: "执行周期",
                        value: intervalText
                    )
                    InfoLabel(
                        title: "错误日志",
                        value: store.snapshot.scheduler.errorLogBytes == 0
                            ? "0 bytes"
                            : "\(store.snapshot.scheduler.errorLogBytes) bytes"
                    )
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 4)
        } label: {
            Label("同步任务", systemImage: "arrow.trianglehead.2.clockwise.rotate.90")
        }
    }

    private var recentRunsSection: some View {
        GroupBox {
            if store.snapshot.records.isEmpty {
                ContentUnavailableView(
                    "尚无同步记录",
                    systemImage: "list.bullet.rectangle"
                )
                .frame(height: 150)
            } else {
                VStack(spacing: 0) {
                    ForEach(store.snapshot.records.prefix(5)) { record in
                        SyncRecordRow(record: record)
                        if record.id != store.snapshot.records.prefix(5).last?.id {
                            Divider()
                        }
                    }
                }
            }
        } label: {
            Label("最近执行", systemImage: "clock")
        }
    }

    private var filesSection: some View {
        GroupBox {
            if let paths = store.paths {
                HStack {
                    FileActionButton(
                        title: "自定义模型源",
                        icon: "doc.text",
                        action: { store.reveal(paths.customSource) }
                    )
                    FileActionButton(
                        title: "合并目录",
                        icon: "square.stack.3d.up",
                        action: { store.reveal(paths.mergedCatalog) }
                    )
                    FileActionButton(
                        title: "同步日志",
                        icon: "list.bullet.rectangle",
                        action: { store.reveal(paths.syncLog) }
                    )
                    FileActionButton(
                        title: "本机配置",
                        icon: "gearshape",
                        action: { store.reveal(store.configurationURL) }
                    )
                    Spacer()
                }
            } else {
                VStack(alignment: .leading, spacing: 5) {
                    Label("尚未加载本机配置", systemImage: "gearshape.2")
                    Text(store.configurationURL.path)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } label: {
            Label("文件位置", systemImage: "folder")
        }
    }

    private var lastRunText: String {
        guard let date = store.snapshot.lastRunAt else { return "未知" }
        let prefix = store.snapshot.lastRunIsExact ? "" : "约 "
        return prefix + AppFormatters.timestamp.string(from: date)
    }

    private var intervalText: String {
        guard let seconds = store.snapshot.scheduler.intervalSeconds else { return "未知" }
        return seconds == 86_400 ? "每天" : "\(seconds) 秒"
    }

    private func statusTitle(_ status: String?) -> String {
        switch status {
        case "updated": "有改动"
        case "no_change": "无改动"
        case "busy": "任务正忙"
        case .none: "未知"
        default: status ?? "未知"
        }
    }
}

private struct MetricTile: View {
    let title: String
    let value: String
    let icon: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.title3)
                .foregroundStyle(.tint)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(value)
                    .font(.title3.weight(.semibold))
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(minHeight: 72)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 7))
        .overlay {
            RoundedRectangle(cornerRadius: 7)
                .stroke(.separator.opacity(0.7))
        }
    }
}

private struct InfoLabel: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .lineLimit(1)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct FileActionButton: View {
    let title: String
    let icon: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Label(title, systemImage: icon)
        }
    }
}

struct SyncRecordRow: View {
    let record: SyncRecord

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: record.changed ? "arrow.down.circle.fill" : "checkmark.circle.fill")
                .foregroundStyle(record.changed ? Color.blue : Color.green)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(record.changed ? "模型目录已更新" : "目录无改动")
                    .fontWeight(.medium)
                Text(record.appVersion)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(record.recordedAt.map(AppFormatters.timestamp.string) ?? "历史记录")
                Text("\(record.bundledCount) 官方 + \(record.customCount) 自定义")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 9)
    }
}
