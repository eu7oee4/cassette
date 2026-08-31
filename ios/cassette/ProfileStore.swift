import SwiftUI

/// 身份资料：我的头像 + 每个角色的头像，本地持久化到 Documents/Profile/。
/// （顶栏标题走 settings.agentName，不在这里。）
/// 多角色（M2）：对方头像按角色分文件——默认角色沿用老的 other.png（零迁移），
/// 其他角色是 char_<id>.png。
///
/// ⚠️ **这里没有「当前是谁」，这是刻意的。** 老版本存过一份 currentCharID + otherAvatar
/// 缓存，靠 switchCharacter() 被调到才刷新——而那行蹲在 ContentView.switchCharacter 的
/// `guard id != currentCharID` 后面。guard 一提前 return，聊天记录/标题/草稿都换了人，
/// 头像还停在上一位身上（2026-08-30 实锤：小卡开着游戏时切到 Cassius，Cassius 顶着
/// 小卡的脸，来回切两次才自愈——文件是好的，坏的是这份缓存）。
/// 现在身份一律由调用方在**取头像那一刻**传进来，没有可失步的东西（串台六条的⑥：
/// 能用结构表达身份就别用字段，能用字段就别用全局）。
/// 旁证：从来没串过的会话列表和房间页，用的就是这个取法。
@MainActor
final class ProfileStore: ObservableObject {
    /// 我的头像（全角色共用一张——「我」只有一个）。nil = 用占位图。
    @Published private(set) var meAvatar: UIImage?

    private let fm = FileManager.default
    /// 角色头像的读盘缓存。**故意不是 @Published**：读路径会在 body 求值里回填它，
    /// 发布出去就是「Publishing changes from within view updates」。回填不需要重画
    /// （拿到的已经是对的那张），真正需要重画的写入方自己 objectWillChange.send()。
    private var charCache: [String: UIImage] = [:]

    private var dir: URL {
        fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Profile", isDirectory: true)
    }

    private func charAvatarURL(_ charID: String) -> URL {
        dir.appendingPathComponent(charID == "default" ? "other.png" : "char_\(charID).png")
    }

    private func avatarURL(_ sender: MessageSender, char: String) -> URL {
        sender == .me ? dir.appendingPathComponent("me.png") : charAvatarURL(char)
    }

    init() {
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        meAvatar = UIImage(contentsOfFile: dir.appendingPathComponent("me.png").path)
    }

    /// 某个角色的头像（nil = 这位没设过头像）。会话列表 / 房间 / 聊天气泡共用这一个取法。
    func avatarImage(forCharacter charID: String) -> UIImage? {
        if let hit = charCache[charID] { return hit }
        guard let img = UIImage(contentsOfFile: charAvatarURL(charID).path) else { return nil }
        charCache[charID] = img
        return img
    }

    /// 取某一方的头像。**对方是谁必须传进来**——不给默认值，缺身份要在编译期就暴露，
    /// 别让它静默变成「错身份」（串台六条的⑤）。sender == .me 时 char 用不上。
    func avatar(for sender: MessageSender, char: String) -> UIImage? {
        sender == .me ? meAvatar : avatarImage(forCharacter: char)
    }

    /// 设置某一方的头像：缩放 + 写文件 + 刷新界面。对方 = **char 指名的那位**。
    ///
    /// char 必须由调用方在**发起那一刻**抓好：选图要过一次异步（iCloud 里的图下载能好
    /// 几秒），等落地时再问「现在是谁」，人在这期间切走了，图就写到新角色头上了——
    /// 和 2026-08-28 那次「A 的设置被写进 B」是同一个坑（延迟写入 + 可变的全局当前）。
    func setAvatar(_ sender: MessageSender, image: UIImage, char: String) {
        let scaled = image.downscaled(maxDimension: 512)
        guard let png = scaled.pngData() else { return }
        try? png.write(to: avatarURL(sender, char: char), options: .atomic)
        if sender == .me {
            meAvatar = scaled                 // @Published，自己会通知
        } else {
            objectWillChange.send()           // charCache 不是 @Published，写入方负责通知
            charCache[char] = scaled
        }
    }
}

extension UIImage {
    /// 等比缩放，使最长边不超过 maxDimension（已经更小就原样返回）。
    func downscaled(maxDimension: CGFloat) -> UIImage {
        let maxSide = max(size.width, size.height)
        guard maxSide > maxDimension, maxSide > 0 else { return self }
        let scale = maxDimension / maxSide
        let newSize = CGSize(width: size.width * scale, height: size.height * scale)
        let renderer = UIGraphicsImageRenderer(size: newSize)
        return renderer.image { _ in self.draw(in: CGRect(origin: .zero, size: newSize)) }
    }
}
