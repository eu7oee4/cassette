import SwiftUI

/// 抽屉能去的页面。层级：聊天 → 抽屉 → 页面 → 详情，返回逐层退（PLAN_cassette_v2 §0）。
/// 占位页随 PR2-5/PR8 逐个换成真页面。
enum DrawerPage: String, Hashable, CaseIterable {
    case memory, mind, history, drafts, game, plugins, settings

    var title: String {
        switch self {
        case .memory:   return "记忆 · Ombre-Brain"
        case .mind:     return "心流日志"
        case .history:  return "聊天记录"
        case .drafts:   return "草稿信箱"
        case .game:     return "游戏"
        case .plugins:  return "插件商店"
        case .settings: return "设置"
        }
    }

    var icon: String {
        switch self {
        case .memory:   return "brain"
        case .mind:     return "waveform"
        case .history:  return "clock.arrow.circlepath"
        case .drafts:   return "envelope"
        case .game:     return "gamecontroller"
        case .plugins:  return "puzzlepiece.extension"
        case .settings: return "gearshape"
        }
    }
}

/// 抽屉面板：从左侧滑入的菜单。只负责画和报点击，开关状态在 ContentView。
struct DrawerPanel: View {
    let agentName: String
    var draftCount: Int = 0          // 草稿信箱待寄数：>0 时该行带数字角标
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
                        // 待寄草稿数：TA 有信等着机主过目才亮，别常驻（角标底色按 Theme 惯例
                        // 用 accent 紫，不用系统红——见 Theme.swift 顶注）。
                        if page == .drafts, draftCount > 0 {
                            Text("\(draftCount)")
                                .font(.caption2.bold())
                                .foregroundStyle(.white)
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(Capsule().fill(Color.theme))
                        }
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
