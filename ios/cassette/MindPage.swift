import SwiftUI

// MARK: - 数据模型（对齐后端 /mind）

/// 一条心流：TA 某次醒来（或聊天时存记忆）想了什么、说了什么、存了什么。
struct MindEntry: Decodable {
    let id: String?               // 内容哈希（左滑删除用定位）
    let ts: Int
    let source: String?           // "wake" | "chat"
    let action: String?           // none | message | error（chat 条目为空串）
    let thoughts: String?
    let content: String?          // message 的正文（被拦下的也在——TA 想说而没说出口的话）
    let pushed: Bool?
    let note: String?             // capped_daily / capped_interval / stale_user_msg
    let stored: [MindStored]?
    let browse: [String]?         // 这次醒来逛的网页（🌐 行；聊天里的浏览有自己的灰字，不进这里）
    let next_wake_note: String?
}

struct MindStored: Decodable {
    let tool: String              // hold | feel | grow | trace | i
    let text: String
    let ok: Bool?                 // 那次调用真成了吗（老记录没这字段 → nil，当成功）
    let error: String?            // ok=false 时的原因
    var failed: Bool { ok == false }
}

extension ChatService {
    func getMindEntries() async throws -> [MindEntry] {
        let data = try await perform(authedRequest("GET", "/mind?limit=150"))
        struct Wrap: Decodable { let items: [MindEntry] }
        do { return try JSONDecoder().decode(Wrap.self, from: data).items }
        catch { throw ChatServiceError.badResponse }
    }

    func deleteMindEntry(id: String) async throws {
        struct Body: Encodable { let id: String }
        let body = try JSONEncoder().encode(Body(id: id))
        _ = try await perform(authedRequest("POST", "/mind/delete", jsonBody: body))
    }
}

// MARK: - 心流日志页（抽屉 → push；标准左上返回）

/// TA 的意识时间线：醒来的内心独白 / 发出或被拦的消息 / 存进长期记忆的内容。
struct MindPage: View {
    private let service = ChatService()

    @State private var entries: [MindEntry] = []
    @State private var loading = true
    @State private var errorText: String? = nil

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorText {
                ContentUnavailableView("读不到心流", systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else if entries.isEmpty {
                ContentUnavailableView("还没有记录", systemImage: "moon.zzz",
                                       description: Text("TA 还没醒来过。"))
            } else {
                List {
                    ForEach(Array(entries.enumerated()), id: \.offset) { _, e in
                        MindRow(entry: e)
                            .swipeActions(edge: .trailing) {
                                if let id = e.id {
                                    Button(role: .destructive) {
                                        Task {
                                            try? await service.deleteMindEntry(id: id)
                                            await load()
                                        }
                                    } label: { Label("删除", systemImage: "trash") }
                                }
                            }
                    }
                }
                .listStyle(.plain)
            }
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("心流日志")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        errorText = nil
        do { entries = try await service.getMindEntries() }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端" }
        loading = false
    }
}

// MARK: - 单条

/// 来源徽章 + 时间；决定图标 + 内心；被拦的消息正文；存进记忆的内容；顺手定的闹钟。
/// 内心和记忆正文是 TA 自己的话，保持原样。
private struct MindRow: View {
    let entry: MindEntry
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(sourceBadge).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                Spacer()
                Text(timeText).font(.caption).foregroundStyle(.secondary)
            }

            if let t = trimmed(entry.thoughts) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if let icon = actionIcon { Text(icon) }
                    Text(t).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            // 被拦下的消息正文：TA 想说而没送出去的话（发出去的正文在聊天里，不重复放）。
            // 带一句原因——不然只看到一段没头没尾的话，不知道为什么没收到。
            if entry.action == "message", entry.pushed != true, let c = trimmed(entry.content) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("🔇")
                        Text(c).font(.callout)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text(blockedReason)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .padding(.leading, 26)
                }
            }

            if let stored = entry.stored, !stored.isEmpty {
                ForEach(Array(stored.enumerated()), id: \.offset) { _, s in
                    // 没成的那次也摆出来（🚫 + 原因）：TA 下次醒来看到就知道该补什么参数，
                    // 眠眠也不会再对着一条其实没落盘的记忆纳闷。
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(s.failed ? "🚫" : storedIcon(s.tool))
                            Text(s.text).font(.callout)
                                .foregroundStyle(s.failed ? AnyShapeStyle(.secondary)
                                                          : AnyShapeStyle(.primary))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if s.failed {
                            Text(failedReason(s))
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                                .padding(.leading, 26)
                        }
                    }
                }
            }

            // 醒来时逛的网页：网址可点跳 Safari（和聊天灰字展开同款交互）。
            if let browse = entry.browse, !browse.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("🌐")
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(browse.enumerated()), id: \.offset) { _, u in
                            Button {
                                if let url = URL(string: u) { openURL(url) }
                            } label: {
                                Text(u)
                                    .font(.caption)
                                    .foregroundStyle(Color.theme)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }

            if let nw = trimmed(entry.next_wake_note) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("⏰")
                    Text(nw).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func trimmed(_ s: String?) -> String? {
        guard let s = s?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty else { return nil }
        return s
    }

    /// stored 条目图标：🫧感受类记忆（hold feel=1，挂在一条已有记忆上）/ ✏️改记忆 /
    /// 🌱自我认知候选（I 工具，要经几轮 dream 见证才转正）/ 📥普通存记忆。
    private func storedIcon(_ tool: String) -> String {
        switch tool {
        case "feel":  return "🫧"
        case "trace": return "✏️"
        case "i":     return "🌱"
        default:      return "📥"
        }
    }

    /// 没成的那次：这本来想干什么 + 工具给的原因。
    private func failedReason(_ s: MindStored) -> String {
        let what: String
        switch s.tool {
        case "feel":  what = "这份心情没存下"
        case "trace": what = "这次修改没生效"
        case "i":     what = "这个念头没存下"
        default:      what = "这条没存下"
        }
        let why = (s.error ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return why.isEmpty ? what : "\(what)——\(why)"
    }

    private var sourceBadge: String {
        entry.source == "chat" ? "🗨️ 聊天" : "🌙 醒来"
    }

    /// 这段话为什么没送出去（note 机器码翻成人话）。下次醒来 TA 会看到这段、自己决定要不要重说。
    private var blockedReason: String {
        switch entry.note {
        case "stale_user_msg":   return "这段话没送出去——刚写完你正好先开口了，内容就过时了"
        case "capped_daily":     return "这段话没送出去——今天的主动消息条数已达上限"
        case "capped_interval":  return "这段话没送出去——距上一条主动消息还没到最小间隔"
        case "capped_quiet":     return "这段话没送出去——你刚说过话，还在静默期里"
        default:                 return "这段话没送出去——被打扰控制拦下了"
        }
    }

    private var timeText: String {
        let f = DateFormatter()
        f.dateFormat = "MM/dd HH:mm"
        f.timeZone = TimeZone(identifier: "Asia/Shanghai")
        return f.string(from: Date(timeIntervalSince1970: TimeInterval(entry.ts)))
    }

    /// 决定图标；chat 条目（只存了记忆）没有 action → nil。
    private var actionIcon: String? {
        switch entry.action {
        case "none":    return "💭"
        case "message": return (entry.pushed ?? false) ? "💬" : "🔇"
        case "error":   return "⚠️"
        default:        return nil
        }
    }
}
