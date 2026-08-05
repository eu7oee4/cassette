import SwiftUI

/// 一套主题色：现在两个常用色（以后可能扩到三个）。
/// 换主题功能（以后做）＝切换整套 palette，而不是散改各处颜色。
struct ThemePalette {
    let accent: Color    // 深色：按钮 / 角标 / 新消息胶囊 / 默认头像
    let bubbleMe: Color  // 浅色：我方气泡底（自带透明度，深色模式下也柔和）
}

extension ThemePalette {
    /// 内置主题：紫。以后加主题＝多一个 static let，换主题＝改 Color.current 的来源。
    static let violet = ThemePalette(
        accent: Color(hex: 0x673AB7),
        bubbleMe: Color(hex: 0x9575CD).opacity(0.30)
    )
}

extension Color {
    /// 当前生效的主题（以后做换主题时，改成从 UserDefaults 读用户选中的那套）。
    static let current = ThemePalette.violet

    static var theme: Color { current.accent }
    static var bubbleMe: Color { current.bubbleMe }

    /// 0xRRGGBB 十六进制建色。
    init(hex: UInt) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xff) / 255,
                  green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255,
                  opacity: 1)
    }
}
