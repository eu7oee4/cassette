import Foundation

/// 谁发的这条消息。
enum MessageSender: String, Codable {
    case me      // 我，气泡靠右
    case other   // 对方，气泡靠左
}

/// 一条聊天消息。Codable：序列化存本地（持久化 / 搜索的基础）。
struct ChatMessage: Identifiable, Codable, Equatable {
    let id: UUID
    let sender: MessageSender
    /// 身份（多角色）："me" / 角色 id。sender 枚举只管气泡靠哪边，这个字段管"是谁"——
    /// 二期群聊/同居里同一边可能有好几个人。旧数据 nil：按 sender + 所在会话推断即可。
    var senderID: String?
    /// 对话通道（二期同居预埋）："text"(手机短信，默认) / "room"(同一空间的真实交互)。
    /// nil 按 text 处理。
    var channel: String?
    let kind: Kind
    let timestamp: Date

    /// 流式增长中（纯 UI 瞬态，不进 Codable）：为 true 时气泡用便宜的纯 Text 渲染，
    /// 避免每来一片都重解析 markdown/代码高亮卡顿；done 定稿后置 false，切回 markdown。
    var isStreaming: Bool = false

    enum Kind: Codable, Equatable {
        case text(String)
        case sticker(URL, String) // 表情包：本地图 + 描述。渲染成图，但 plainText 带描述发给后端
        case image(URL)           // 聊天图片：本地存储。发送那轮以 base64 附给后端（多模态）
        case file(URL, String)    // 发的文件（PDF/文本/docx）：沙盒本地文件 + 原始文件名，渲染成文件卡片
        case webpage(String, String)  // TA 做的网页：page_id + 标题，渲染成网页卡片（点开看）
        case system(String)       // 系统提示（居中灰字）
        case memoryNote(String)   // 「存了记忆」等提示（居中灰字，纯 UI，不发回后端）
        case browseNote([String]) // 「浏览了 N 个网页」灰字：点击展开网址列表（纯 UI，不发回后端）
    }

    init(id: UUID = UUID(), sender: MessageSender, kind: Kind, timestamp: Date,
         isStreaming: Bool = false, senderID: String? = nil, channel: String? = nil) {
        self.id = id
        self.sender = sender
        self.senderID = senderID
        self.channel = channel
        self.kind = kind
        self.timestamp = timestamp
        self.isStreaming = isStreaming
    }

    // 手写 CodingKeys：isStreaming 是 UI 瞬态，不落盘。senderID/channel 可选，
    // 合成 Codable 走 decodeIfPresent——旧 JSON 没这俩键照样解。
    enum CodingKeys: String, CodingKey {
        case id, sender, senderID, channel, kind, timestamp
    }

    /// 这条消息的纯文本。用于发给后端的历史 & 搜索。
    var plainText: String {
        switch kind {
        case .text(let t):       return t
        case .sticker(_, let d): return d.isEmpty ? "[表情包]" : "[表情包：\(d)]"
        case .image:             return "[图片]"   // 历史轮次里图片以占位文本注入
        case .file(_, let n):    return "[文件:\(n)]"
        case .webpage(_, let t): return "[网页:\(t)]"
        case .system(let t):     return t
        case .memoryNote(let t): return t
        case .browseNote(let u): return "[浏览了 \(u.count) 个网页]"
        }
    }

    /// 是否是系统提示消息。
    var isSystem: Bool {
        if case .system = kind { return true }
        return false
    }

    /// 是否是「存了记忆」提示（居中灰字渲染、但不发回后端）。
    var isMemoryNote: Bool {
        if case .memoryNote = kind { return true }
        return false
    }

    /// 是否是「浏览了 N 个网页」提示（可展开灰字，纯 UI，不发回后端）。
    var isBrowseNote: Bool {
        if case .browseNote = kind { return true }
        return false
    }
}
