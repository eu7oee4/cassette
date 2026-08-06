import SwiftUI

/// 抽屉能去的页面。层级：聊天 → 抽屉 → 页面 → 详情，返回逐层退（PLAN_cassette_v2 §0）。
/// 占位页随 PR2-5/PR8 逐个换成真页面。
enum DrawerPage: String, Hashable, CaseIterable {
    case memory, mind, history, plugins, settings

    var title: String {
        switch self {
        case .memory:   return "记忆 · Ombre-Brain"
        case .mind:     return "心流日志"
        case .history:  return "聊天记录"
        case .plugins:  return "插件商店"
        case .settings: return "设置"
        }
    }

    var icon: String {
        switch self {
        case .memory:   return "brain"
        case .mind:     return "waveform"
        case .history:  return "clock.arrow.circlepath"
        case .plugins:  return "puzzlepiece.extension"
        case .settings: return "gearshape"
        }
    }
}

/// 抽屉面板：从左侧滑入的菜单。只负责画和报点击，开关状态在 ContentView。
struct DrawerPanel: View {
    let agentName: String
    let onSelect: (DrawerPage) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // 顶部放 TA 的名字——这是 TA 的空间，不是功能列表的标题。
            // 下沉一段（眠眠真机反馈：贴着状态栏太高），大约落在原第二行菜单的位置。
            Text(agentName)
                .font(.title3.bold())
                .padding(.horizontal, 20)
                .padding(.top, 120)
                .padding(.bottom, 14)
            Divider()
            ForEach(DrawerPage.allCases, id: \.self) { page in
                Button { onSelect(page) } label: {
                    HStack(spacing: 14) {
                        Image(systemName: page.icon)
                            .font(.system(size: 17))
                            .foregroundStyle(Color.theme)
                            .frame(width: 24)
                        Text(page.title)
                            .foregroundStyle(.primary)
                        Spacer()
                    }
                    .padding(.horizontal, 20)
                    .padding(.vertical, 14)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            Spacer()
        }
        .frame(width: 290)
        .frame(maxHeight: .infinity)
        .background(Color(.systemBackground))
        .ignoresSafeArea()
    }
}

/// 还没盖好的页面占位。
struct DrawerPlaceholderPage: View {
    let page: DrawerPage

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: page.icon)
                .font(.system(size: 44))
                .foregroundStyle(Color.theme.opacity(0.5))
            Text("这里以后是\(page.title)")
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemGroupedBackground))
        .navigationTitle(page.title)
        .navigationBarTitleDisplayMode(.inline)
    }
}
