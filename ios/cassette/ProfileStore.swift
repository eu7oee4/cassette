import SwiftUI

/// 身份资料：我的头像 + **当前会话角色**的头像，本地持久化到 Documents/Profile/。
/// （顶栏标题走 settings.agentName，不在这里。）
/// 多角色（M2）：对方头像按角色分文件——默认角色沿用老的 other.png（零迁移），
/// 其他角色是 char_<id>.png。切会话时 switchCharacter 重载。
@MainActor
final class ProfileStore: ObservableObject {
    /// 两个头像的图片（nil = 用占位图）。otherAvatar = 当前会话角色的。
    @Published private(set) var meAvatar: UIImage?
    @Published private(set) var otherAvatar: UIImage?

    private let fm = FileManager.default
    private var currentCharID: String

    private var dir: URL {
        fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Profile", isDirectory: true)
    }

    private func charAvatarURL(_ charID: String) -> URL {
        dir.appendingPathComponent(charID == "default" ? "other.png" : "char_\(charID).png")
    }

    private func avatarURL(_ sender: MessageSender) -> URL {
        sender == .me ? dir.appendingPathComponent("me.png") : charAvatarURL(currentCharID)
    }

    init() {
        currentCharID = CurrentCharacter.id
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        meAvatar = UIImage(contentsOfFile: dir.appendingPathComponent("me.png").path)
        otherAvatar = UIImage(contentsOfFile: charAvatarURL(currentCharID).path)
    }

    /// 切换当前会话角色：重载对方头像。
    func switchCharacter(_ charID: String) {
        currentCharID = charID
        otherAvatar = UIImage(contentsOfFile: charAvatarURL(charID).path)
    }

    /// 取某一方的头像图片（可能为 nil）。
    func avatar(for sender: MessageSender) -> UIImage? {
        sender == .me ? meAvatar : otherAvatar
    }

    /// 任意角色的头像（会话列表行用），不影响当前状态。
    func avatarImage(forCharacter charID: String) -> UIImage? {
        UIImage(contentsOfFile: charAvatarURL(charID).path)
    }

    /// 设置某一方的头像：缩放到合适大小 + 写文件 + 刷新界面。对方 = 当前会话角色。
    func setAvatar(_ sender: MessageSender, image: UIImage) {
        let scaled = image.downscaled(maxDimension: 512)
        guard let png = scaled.pngData() else { return }
        try? png.write(to: avatarURL(sender), options: .atomic)
        if sender == .me { meAvatar = scaled } else { otherAvatar = scaled }
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
