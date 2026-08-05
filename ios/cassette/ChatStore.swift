import Foundation

/// 聊天记录的唯一主人：内存持有全部消息，并持久化到本地 JSON 文件。
/// app 重启不丢；编辑 / 重新生成 / 搜索都基于这里。后端无状态，历史随 app 走。
@MainActor
final class ChatStore: ObservableObject {
    @Published private(set) var messages: [ChatMessage] = []

    private let fileManager = FileManager.default

    /// 本地存储位置：Documents/chat_history.json
    private var fileURL: URL {
        let docs = fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return docs.appendingPathComponent("chat_history.json")
    }

    init() {
        load()
    }

    // MARK: - 读写操作

    /// 追加一条消息。
    func append(_ message: ChatMessage) {
        messages.append(message)
        save()
    }

    /// 追加一条系统提示消息（居中灰字）。统一入口。
    /// sender 用 .me 只是为了发给后端时算作用户轮；显示按 kind 走居中样式。
    func appendSystemMessage(_ text: String) {
        append(ChatMessage(sender: .me, kind: .system(text), timestamp: Date()))
    }

    /// 追加一条「存了记忆」提示（居中灰字）。纯 UI：显示在聊天里，但不发回后端。
    func appendMemoryNote(_ text: String) {
        append(ChatMessage(sender: .other, kind: .memoryNote(text), timestamp: Date()))
    }

    /// 修改某条消息的文字，保留其 id、发送方。
    /// updateTimestamp=true 时把时间刷新为当前（用于「编辑并重新回复」＝相当于重发）；
    /// false 时保留原时间（用于「仅保存」的纯改错字）。
    func editText(id: UUID, newText: String, updateTimestamp: Bool = false) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        let old = messages[i]
        messages[i] = ChatMessage(id: old.id, sender: old.sender,
                                  kind: .text(newText),
                                  timestamp: updateTimestamp ? Date() : old.timestamp)
        save()
    }

    /// 流式：追加一条消息但先不落盘（流式期间高频调用，省得每片都写整份文件）。
    /// UI 照常刷新（@Published）；流结束时用 editText 落一次盘对齐。
    func appendNoSave(_ message: ChatMessage) {
        messages.append(message)
    }

    /// 流式：更新某条消息的文字、先不落盘（同上）。保持 isStreaming=true（气泡走便宜的纯 Text）。
    func updateTextNoSave(id: UUID, newText: String) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        let old = messages[i]
        messages[i] = ChatMessage(id: old.id, sender: old.sender,
                                  kind: .text(newText), timestamp: old.timestamp, isStreaming: true)
    }

    /// 删除某条消息。
    func remove(id: UUID) {
        messages.removeAll { $0.id == id }
        save()
    }

    /// 删掉某条消息【之后】的所有消息（保留它本身）。
    /// 用于「编辑并重新回复」——改完这条后，把它后面的旧对话清掉，再重答。
    func truncateAfter(id: UUID) {
        guard let i = messages.firstIndex(where: { $0.id == id }), i + 1 < messages.count else { return }
        messages.removeSubrange((i + 1)...)
        save()
    }

    /// 并入一条对方主动发来的消息（来自后端待送达盒子：断连补投 / 以后的主动消息）。
    /// 去重：同文字 + 同秒 的对方消息已存在就跳过（防 ack 失败重复拉取时插两条）。
    /// 按时间戳插到正确位置，保证顺序。
    func insertProactive(text: String, timestamp: Date) {
        let ts = Int(timestamp.timeIntervalSince1970)
        let dup = messages.contains {
            $0.sender == .other && $0.plainText == text
                && Int($0.timestamp.timeIntervalSince1970) == ts
        }
        guard !dup else { return }
        let msg = ChatMessage(sender: .other, kind: .text(text), timestamp: timestamp)
        if let i = messages.firstIndex(where: { $0.timestamp > timestamp }) {
            messages.insert(msg, at: i)
        } else {
            messages.append(msg)
        }
        save()
    }

    /// 并入一条对方主动发来的表情（醒来时配的表情）。去重：同表情图 + 同秒 已存在就跳过。
    func insertProactiveSticker(url: URL, description: String, timestamp: Date) {
        let ts = Int(timestamp.timeIntervalSince1970)
        let dup = messages.contains { m in
            guard m.sender == .other,
                  Int(m.timestamp.timeIntervalSince1970) == ts,
                  case .sticker(let u, _) = m.kind else { return false }
            return u == url
        }
        guard !dup else { return }
        let msg = ChatMessage(sender: .other, kind: .sticker(url, description), timestamp: timestamp)
        if let i = messages.firstIndex(where: { $0.timestamp > timestamp }) {
            messages.insert(msg, at: i)
        } else {
            messages.append(msg)
        }
        save()
    }

    // MARK: - 持久化

    /// 逐条容错的解码壳：单条坏了只丢那一条，不让整份历史跟着报废。
    private struct FailableMessage: Decodable {
        let message: ChatMessage?
        init(from decoder: Decoder) throws { message = try? ChatMessage(from: decoder) }
    }

    private func load() {
        guard let data = try? Data(contentsOf: fileURL) else {
            messages = []
            return
        }
        if let decoded = try? JSONDecoder().decode([FailableMessage].self, from: data) {
            messages = decoded.compactMap(\.message)
            return
        }
        // 整份 JSON 都坏了（半截写入等）：先把现场备份走，再空载——
        // 绝不能静默清空后让下一次 save() 覆盖掉唯一副本（那是数据丢失级事故）。
        let broken = fileURL.appendingPathExtension("broken")
        try? fileManager.removeItem(at: broken)
        try? fileManager.copyItem(at: fileURL, to: broken)
        messages = []
    }

    private func save() {
        do {
            let data = try JSONEncoder().encode(messages)
            try data.write(to: fileURL, options: .atomic)
        } catch {
            print("保存聊天记录失败: \(error)")
        }
    }
}
