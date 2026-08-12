import Foundation

/// 聊天记录的唯一主人：内存持有**当前会话**的全部消息，并持久化到本地 JSON 文件。
/// app 重启不丢；编辑 / 重新生成 / 搜索都基于这里。后端无状态，历史随 app 走。
///
/// 多角色（M2）：按会话分文件 `Documents/conversations/<charID>/chat_history.json`，
/// 一个角色一个会话。旧的单文件 `Documents/chat_history.json` 首启整体搬进 default。
/// 非当前会话的来件（wake 主动消息等）直接写对方的文件 + 未读数 +1，不动内存。
@MainActor
final class ChatStore: ObservableObject {
    @Published private(set) var messages: [ChatMessage] = []
    /// 未读数：charID → 条数（当前会话恒 0）。UserDefaults 持久化。
    @Published private(set) var unread: [String: Int] = [:]
    private(set) var conversationID: String

    private let fileManager = FileManager.default
    private let unreadKey = "conv_unread"

    private var docs: URL {
        fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    private func fileURL(for conv: String) -> URL {
        docs.appendingPathComponent("conversations", isDirectory: true)
            .appendingPathComponent(conv, isDirectory: true)
            .appendingPathComponent("chat_history.json")
    }

    /// 当前会话的存储位置。
    private var fileURL: URL { fileURL(for: conversationID) }

    init() {
        conversationID = UserDefaults.standard.string(forKey: CurrentCharacter.key) ?? "default"
        unread = (UserDefaults.standard.dictionary(forKey: unreadKey) as? [String: Int]) ?? [:]
        migrateLegacyIfNeeded()
        messages = Self.loadMessages(from: fileURL, fileManager: fileManager)
    }

    /// 旧单文件布局 → conversations/default/。只搬一次（目标存在就不动，防覆盖）。
    private func migrateLegacyIfNeeded() {
        let legacy = docs.appendingPathComponent("chat_history.json")
        let target = fileURL(for: "default")
        guard fileManager.fileExists(atPath: legacy.path),
              !fileManager.fileExists(atPath: target.path) else { return }
        try? fileManager.createDirectory(at: target.deletingLastPathComponent(),
                                         withIntermediateDirectories: true)
        try? fileManager.moveItem(at: legacy, to: target)
    }

    // MARK: - 会话切换

    /// 切到另一个角色的会话：当前落盘 → 换 id → 载入 → 清未读。
    /// ⚠️ 调用方自己保证不在流式生成中切（流式的 NoSave 消息还没落盘）。
    func switchConversation(_ id: String) {
        guard id != conversationID else { return }
        save()
        conversationID = id
        UserDefaults.standard.set(id, forKey: CurrentCharacter.key)
        messages = Self.loadMessages(from: fileURL, fileManager: fileManager)
        clearUnread(id)
    }

    /// 会话列表行的预览（最后一条非灰字消息）。存 UserDefaults，别为画列表解码整份历史。
    func preview(for conv: String) -> String {
        UserDefaults.standard.string(forKey: "conv_preview_\(conv)") ?? ""
    }

    private func updatePreview(_ conv: String, messages: [ChatMessage]) {
        let last = messages.last(where: { !$0.isSystem && !$0.isMemoryNote && !$0.isBrowseNote })
        UserDefaults.standard.set(String((last?.plainText ?? "").prefix(60)),
                                  forKey: "conv_preview_\(conv)")
    }

    private func bumpUnread(_ conv: String) {
        unread[conv, default: 0] += 1
        UserDefaults.standard.set(unread, forKey: unreadKey)
    }

    func clearUnread(_ conv: String) {
        guard unread[conv] != nil else { return }
        unread.removeValue(forKey: conv)
        UserDefaults.standard.set(unread, forKey: unreadKey)
    }

    /// 除当前会话外的未读总数（顶栏小圆点用）。
    var otherUnreadTotal: Int {
        unread.filter { $0.key != conversationID }.values.reduce(0, +)
    }

    // MARK: - 读写操作

    /// 给消息补身份：没写 senderID 的按「我 / 当前会话角色」推断后落盘（旧数据兼容层）。
    private func stamped(_ m: ChatMessage, conv: String? = nil) -> ChatMessage {
        guard m.senderID == nil else { return m }
        var msg = m
        msg.senderID = m.sender == .me ? "me" : (conv ?? conversationID)
        return msg
    }

    /// 追加一条消息。
    func append(_ message: ChatMessage) {
        messages.append(stamped(message))
        save()
    }

    /// 追加一条系统提示消息（居中灰字）。统一入口。
    /// 纯 UI：显示按 kind 走居中样式，发给后端时整条过滤掉（见 ChatService 的 isSystem）——
    /// 它是 app 说给人看的话，混进历史就成了以 role:user 冒充用户说过的。
    func appendSystemMessage(_ text: String) {
        append(ChatMessage(sender: .me, kind: .system(text), timestamp: Date()))
    }

    /// 追加一条「存了记忆」提示（居中灰字）。纯 UI：显示在聊天里，但不发回后端。
    func appendMemoryNote(_ text: String) {
        append(ChatMessage(sender: .other, kind: .memoryNote(text), timestamp: Date()))
    }

    /// 修改某条消息的文字，保留其 id、发送方、身份。
    /// updateTimestamp=true 时把时间刷新为当前（用于「编辑并重新回复」＝相当于重发）；
    /// false 时保留原时间（用于「仅保存」的纯改错字）。
    func editText(id: UUID, newText: String, updateTimestamp: Bool = false) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        let old = messages[i]
        messages[i] = ChatMessage(id: old.id, sender: old.sender,
                                  kind: .text(newText),
                                  timestamp: updateTimestamp ? Date() : old.timestamp,
                                  senderID: old.senderID, channel: old.channel)
        save()
    }

    /// 流式：追加一条消息但先不落盘（流式期间高频调用，省得每片都写整份文件）。
    /// UI 照常刷新（@Published）；流结束时用 editText 落一次盘对齐。
    func appendNoSave(_ message: ChatMessage) {
        messages.append(stamped(message))
    }

    /// 流式：更新某条消息的文字、先不落盘（同上）。保持 isStreaming=true（气泡走便宜的纯 Text）。
    func updateTextNoSave(id: UUID, newText: String) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        let old = messages[i]
        messages[i] = ChatMessage(id: old.id, sender: old.sender,
                                  kind: .text(newText), timestamp: old.timestamp,
                                  isStreaming: true,
                                  senderID: old.senderID, channel: old.channel)
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

    /// 并入一条对方主动发来的消息（来自后端待送达盒子：断连补投 / 主动消息）。
    /// conversation 不传 = 当前会话（原行为）；传了别的角色 = 直接写对方文件 + 未读 +1。
    /// 去重：同文字 + 同秒 的对方消息已存在就跳过（防 ack 失败重复拉取时插两条）。
    /// 按时间戳插到正确位置，保证顺序。
    func insertProactive(text: String, timestamp: Date, conversation: String? = nil) {
        let conv = conversation ?? conversationID
        let msg = stamped(ChatMessage(sender: .other, kind: .text(text), timestamp: timestamp),
                          conv: conv)
        if conv == conversationID {
            insertIntoCurrent(msg) { existing in
                existing.sender == .other && existing.plainText == text
                    && Int(existing.timestamp.timeIntervalSince1970)
                        == Int(timestamp.timeIntervalSince1970)
            }
        } else {
            insertIntoFile(msg, conv: conv) { existing in
                existing.sender == .other && existing.plainText == text
                    && Int(existing.timestamp.timeIntervalSince1970)
                        == Int(timestamp.timeIntervalSince1970)
            }
        }
    }

    /// 并入一条对方主动发来的表情（醒来时配的表情）。去重：同表情图 + 同秒 已存在就跳过。
    func insertProactiveSticker(url: URL, description: String, timestamp: Date,
                                conversation: String? = nil) {
        let conv = conversation ?? conversationID
        let msg = stamped(ChatMessage(sender: .other, kind: .sticker(url, description),
                                      timestamp: timestamp), conv: conv)
        let dup: (ChatMessage) -> Bool = { existing in
            guard existing.sender == .other,
                  Int(existing.timestamp.timeIntervalSince1970)
                      == Int(timestamp.timeIntervalSince1970),
                  case .sticker(let u, _) = existing.kind else { return false }
            return u == url
        }
        if conv == conversationID {
            insertIntoCurrent(msg, isDuplicate: dup)
        } else {
            insertIntoFile(msg, conv: conv, isDuplicate: dup)
        }
    }

    /// 当前会话：查重 → 按时间戳插位 → 落盘。
    private func insertIntoCurrent(_ msg: ChatMessage, isDuplicate: (ChatMessage) -> Bool) {
        guard !messages.contains(where: isDuplicate) else { return }
        if let i = messages.firstIndex(where: { $0.timestamp > msg.timestamp }) {
            messages.insert(msg, at: i)
        } else {
            messages.append(msg)
        }
        save()
    }

    /// 非当前会话：读对方文件 → 查重插位 → 写回 + 未读 +1 + 刷预览。不碰内存里的当前会话。
    private func insertIntoFile(_ msg: ChatMessage, conv: String,
                                isDuplicate: (ChatMessage) -> Bool) {
        let url = fileURL(for: conv)
        var msgs = Self.loadMessages(from: url, fileManager: fileManager)
        guard !msgs.contains(where: isDuplicate) else { return }
        if let i = msgs.firstIndex(where: { $0.timestamp > msg.timestamp }) {
            msgs.insert(msg, at: i)
        } else {
            msgs.append(msg)
        }
        Self.saveMessages(msgs, to: url, fileManager: fileManager)
        updatePreview(conv, messages: msgs)
        bumpUnread(conv)
    }

    // MARK: - 持久化

    /// 逐条容错的解码壳：单条坏了只丢那一条，不让整份历史跟着报废。
    private struct FailableMessage: Decodable {
        let message: ChatMessage?
        init(from decoder: Decoder) throws { message = try? ChatMessage(from: decoder) }
    }

    private static func loadMessages(from url: URL, fileManager: FileManager) -> [ChatMessage] {
        guard let data = try? Data(contentsOf: url) else { return [] }
        if let decoded = try? JSONDecoder().decode([FailableMessage].self, from: data) {
            return decoded.compactMap(\.message)
        }
        // 整份 JSON 都坏了（半截写入等）：先把现场备份走，再空载——
        // 绝不能静默清空后让下一次 save() 覆盖掉唯一副本（那是数据丢失级事故）。
        let broken = url.appendingPathExtension("broken")
        try? fileManager.removeItem(at: broken)
        try? fileManager.copyItem(at: url, to: broken)
        return []
    }

    private static func saveMessages(_ msgs: [ChatMessage], to url: URL,
                                     fileManager: FileManager) {
        do {
            try fileManager.createDirectory(at: url.deletingLastPathComponent(),
                                            withIntermediateDirectories: true)
            let data = try JSONEncoder().encode(msgs)
            try data.write(to: url, options: .atomic)
        } catch {
            print("保存聊天记录失败: \(error)")
        }
    }

    private func save() {
        Self.saveMessages(messages, to: fileURL, fileManager: fileManager)
        updatePreview(conversationID, messages: messages)
    }
}
