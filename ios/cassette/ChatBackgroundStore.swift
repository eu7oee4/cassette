import SwiftUI
import UIKit

/// 聊天背景图 + 蒙版（PLAN_chatui §1.3 / U6）。纯本机偏好，不进后端。
/// 浅/深模式各一张（§1.3 正文口径）；蒙版是盖在图上的一层 bg 色半透明层，
/// 滑到底＝纯色背景——它存在的理由：小字提醒和系统消息直接坐在背景上，
/// 字色固定灰，遇到高对比度照片会读不出来，蒙版是一个滑块救全场的做法。
final class ChatBackgroundStore: ObservableObject {
    @Published private(set) var lightImage: UIImage?
    @Published private(set) var darkImage: UIImage?
    /// 蒙版浓度 0（纯图）～1（纯色）。默认 0.4 是第一版，机主看效果再调。
    @Published var dim: Double {
        didSet { UserDefaults.standard.set(dim, forKey: "chatBGDim") }
    }

    private let fm = FileManager.default
    private var dir: URL {
        fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("ChatBackground", isDirectory: true)
    }
    private func url(dark: Bool) -> URL {
        dir.appendingPathComponent(dark ? "dark.jpg" : "light.jpg")
    }

    init() {
        let saved = UserDefaults.standard.object(forKey: "chatBGDim") as? Double
        dim = saved ?? 0.4
        lightImage = UIImage(contentsOfFile: url(dark: false).path)
        darkImage = UIImage(contentsOfFile: url(dark: true).path)
    }

    func image(for scheme: ColorScheme) -> UIImage? {
        scheme == .dark ? darkImage : lightImage
    }

    func setImage(_ data: Data, dark: Bool) {
        guard let img = UIImage(data: data) else { return }
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        // 统一转 JPEG 落盘（HEIC/PNG 都收），大图先缩到 ~2000pt 边长省内存
        let scaled = img.scaledDown(maxSide: 2000)
        try? scaled.jpegData(compressionQuality: 0.85)?.write(to: url(dark: dark))
        if dark { darkImage = scaled } else { lightImage = scaled }
    }

    func clearImage(dark: Bool) {
        try? fm.removeItem(at: url(dark: dark))
        if dark { darkImage = nil } else { lightImage = nil }
    }
}

private extension UIImage {
    func scaledDown(maxSide: CGFloat) -> UIImage {
        let side = max(size.width, size.height)
        guard side > maxSide else { return self }
        let k = maxSide / side
        let newSize = CGSize(width: size.width * k, height: size.height * k)
        let fmt = UIGraphicsImageRendererFormat.default()
        fmt.scale = 1
        return UIGraphicsImageRenderer(size: newSize, format: fmt).image { _ in
            draw(in: CGRect(origin: .zero, size: newSize))
        }
    }
}
