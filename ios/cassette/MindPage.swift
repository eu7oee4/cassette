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
    let next_wake_note: String?
}

struct MindStored: Decodable {
    let tool: String              // hold | grow | trace
    let text: String
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
            if entry.action == "message", entry.pushed != true, let c = trimmed(entry.content) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("🔇")
                    Text(c).font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if let stored = entry.stored, !stored.isEmpty {
                ForEach(Array(stored.enumerated()), id: \.offset) { _, s in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(storedIcon(s.tool))
                        Text(s.text).font(.callout)
                            .fixedSize(horizontal: false, vertical: true)
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

    /// stored 条目图标：✏️改记忆 / 🌱自我认知候选（I 工具，要经几轮 dream 见证才转正）/ 📥存记忆。
    private func storedIcon(_ tool: String) -> String {
        switch tool {
        case "trace": return "✏️"
        case "i":     return "🌱"
        default:      return "📥"
        }
    }

    private var sourceBadge: String {
        entry.source == "chat" ? "🗨️ 聊天" : "🌙 醒来"
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
