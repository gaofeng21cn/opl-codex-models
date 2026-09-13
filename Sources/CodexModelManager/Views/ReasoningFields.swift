import SwiftUI

struct ReasoningFields: View {
    @Binding var settings: ReasoningSettings

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("可选档位")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 105), alignment: .leading)], alignment: .leading) {
                ForEach(settings.availableEfforts, id: \.self) { effort in
                    Toggle(effort, isOn: Binding(
                        get: { settings.supportedEfforts.contains(effort) },
                        set: { settings.setEnabled($0, for: effort) }
                    ))
                    .toggleStyle(.checkbox)
                    .accessibilityLabel("推理档位 \(effort)")
                }
            }
            Picker("默认档位", selection: $settings.defaultEffort) {
                if !settings.supportedEfforts.contains(settings.defaultEffort) {
                    Text("请选择").tag(settings.defaultEffort)
                }
                ForEach(settings.supportedEfforts, id: \.self) { effort in
                    Text(effort).tag(effort)
                }
            }
            .disabled(settings.supportedEfforts.isEmpty)
            Text("请按供应商实际支持的能力选择。取消默认档位时，会改用最后一个已选档位，可在上方调整。")
                .font(.caption)
                .foregroundStyle(.secondary)
            if !settings.isValid {
                Text("至少选择一个推理档位，并指定默认档位。")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 4)
    }
}
