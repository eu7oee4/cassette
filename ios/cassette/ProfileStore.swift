import SwiftUI

/// 身份资料：两个头像（我 / 对方），本地持久化到 Documents/Profile/。
/// （顶栏标题走 settings.agentName，不在这里。）
@MainActor
final class ProfileStore: ObservableObject {
    /// 两个头像的图片（nil = 用占位图）。
    @Published private(set) var meAvatar: UIImage?
    @Published private(set) var otherAvatar: UIImage?

    private let fm = FileManager.default

    private var dir: URL {
        fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Profile", isDirectory: true)
    }
    private func avatarURL(_ sender: MessageSender) -> URL {
        dir.appendingPathComponent(sender == .me ? "me.png" : "other.png")
    }

    init() {
        let d = fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Profile", isDirectory: true)
        try? fm.createDirectory(at: d, withIntermediateDirectories: true)
        meAvatar = UIImage(contentsOfFile: d.appendingPathComponent("me.png").path)
        otherAvatar = UIImage(contentsOfFile: d.appendingPathComponent("other.png").path)
    }

    /// 取某一方的头像图片（可能为 nil）。
    func avatar(for sender: MessageSender) -> UIImage? {
        sender == .me ? meAvatar : otherAvatar
    }

    /// 设置某一方的头像：缩放到合适大小 + 写文件 + 刷新界面。
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
