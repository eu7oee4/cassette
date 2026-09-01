import SwiftUI

/// 权限卡（PLAN_native §6 / chatui U4）：TA 的一次写类调用原地挂起，等机主拍板。
/// 染色走 §1.2 唯一口径（角色人物色 + 反色字）；卡上是 SDK 那句整话（有的话）
/// + 参数原文（Bash 命令全文 / Edit 的文件和改动），批准/拒绝两个按钮横排，
/// 拒绝可附一句理由；批准免二次确认（§6：挂起有超时兜底，误触的代价是一次
/// 可撤销的写）。超时整卡置灰标「已超时」（§5.3）。
struct PermitCardView: View {
    @Environment(\.colorScheme) private var scheme
    let card: PermitCard
    let charID: String
    /// 拍板：allow + 拒绝理由（批准时理由为空）。
    var onDecide: (_ allow: Bool, _ reason: String) -> Void = { _, _ in }
    /// 超时置灰后机主点「收起」。
    var onClose: () -> Void = { }

    @State private var reason = ""
    @State private var expired = false

    private var pal: CharPalette { IdentityColor.palette(for: charID) }
    private var fill: Color { expired ? Color(uiColor: .systemGray4) : ChatTint.fill(pal, scheme) }
    private var ink: Color { expired ? .secondary : ChatTint.text(scheme) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("想动手 · \(card.tool)")
                    .font(.caption2.bold())
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(ink.opacity(0.14), in: Capsule())
                Spacer()
                if expired {
                    Button("收起") { onClose() }
                        .font(.caption)
                        .buttonStyle(.plain)
                }
            }
            if let t = card.title, !t.isEmpty {
                Text(t).font(.subheadline.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
            } else if let s = card.summary, !s.isEmpty {
                Text(s).font(.subheadline.weight(.semibold))
                    .lineLimit(2).truncationMode(.middle)
            }
            if let d = card.detail, !d.isEmpty {
                // 参数原文：等宽小字，太长在卡内自己滚（§6：机主批的就是这些字节）
                ScrollView {
                    Text(d)
                        .font(.caption.monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                }
                .frame(maxHeight: 150)
                .background(ink.opacity(0.10),
                            in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            if !expired {
                TextField("不批的话，想说一句吗（可空）", text: $reason)
                    .font(.caption)
                    .padding(.horizontal, 10).padding(.vertical, 7)
                    .background(ink.opacity(0.10),
                                in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                HStack(spacing: 6) {
                    decideButton("批准", strong: true) { onDecide(true, "") }
                    decideButton("拒绝", strong: false) {
                        onDecide(false, reason.trimmingCharacters(in: .whitespacesAndNewlines))
                    }
                }
            }
        }
        .padding(14)
        .foregroundStyle(ink)
        .background(fill, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(alignment: .topLeading) {
            if expired {
                Text("已超时")
                    .font(.caption2.bold())
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Color(uiColor: .systemGray2), in: Capsule())
                    .foregroundStyle(.white)
                    .offset(x: 10, y: -8)
            }
        }
        .task(id: card.id) {
            guard let dl = card.deadline else { return }
            let wait = TimeInterval(dl) - Date().timeIntervalSince1970
            if wait > 0 {
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
            expired = true                          // 后端同刻已超时自拒，这儿只管置灰
        }
    }

    private func decideButton(_ label: String, strong: Bool,
                              action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.subheadline.weight(.semibold))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
                .background(ink.opacity(strong ? 0.22 : 0.12),
                            in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
    }
}
