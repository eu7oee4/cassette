import SwiftUI

/// 会话列表：每个角色一行（头像 / 名字 / 最后一条预览 / 未读角标）。
/// 入口在顶栏标题（点名字进来）和抽屉。二期同居世界的「房子视图」落地后它降级为手机通讯录。
struct ConversationsPage: View {
    @ObservedObject var charStore: CharacterListStore
    @ObservedObject var chatStore: ChatStore
    @EnvironmentObject var profileStore: ProfileStore
    let currentID: String
    /// 流式生成中不许切（NoSave 的流式气泡还没落盘，切走会丢）。
    let switchDisabled: Bool
    let onSelect: (CharacterInfo) -> Void

    var body: some View {
        List(charStore.items) { c in
            Button {
                onSelect(c)
            } label: {
                HStack(spacing: 12) {
                    avatarView(for: c.id)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(displayName(c))
                            .font(.body.weight(.medium))
                            .foregroundStyle(.primary)
                        let preview = chatStore.preview(for: c.id)
                        if !preview.isEmpty {
                            Text(preview)
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    Spacer()
                    if let n = chatStore.unread[c.id], n > 0, c.id != currentID {
                        Text("\(n)")
                            .font(.caption2.bold())
                            .foregroundStyle(.white)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(Capsule().fill(Color.theme))
                    }
                    if c.id == currentID {
                        Image(systemName: "checkmark")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(Color.theme)
                    }
                }
                .contentShape(Rectangle())
            }
            .disabled(switchDisabled && c.id != currentID)
        }
        .listStyle(.plain)
        .navigationTitle("会话")
        .navigationBarTitleDisplayMode(.inline)
        .overlay(alignment: .bottom) {
            if switchDisabled {
                Text("正在回复中，说完这轮再切")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(8)
            }
        }
        .task { await charStore.refresh() }
    }

    private func displayName(_ c: CharacterInfo) -> String {
        c.display_name.isEmpty ? (c.id == "default" ? "cassette" : c.id) : c.display_name
    }

    @ViewBuilder
    private func avatarView(for charID: String) -> some View {
        if let img = profileStore.avatarImage(forCharacter: charID) {
            Image(uiImage: img)
                .resizable()
                .scaledToFill()
                .frame(width: 44, height: 44)
                .clipShape(Circle())
        } else {
            Circle()
                .fill(Color.theme.opacity(0.15))
                .frame(width: 44, height: 44)
                .overlay(Image(systemName: "pawprint")
                    .font(.system(size: 18))
                    .foregroundStyle(Color.theme))
        }
    }
}
