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

/// 小屋（同居世界）的整套配色：房子/房间视图是一个独立的暖色深色世界，不随系统深浅色走。
/// 换主题 = 换 `Color.house` 指向的 palette（一处改，整个小屋换肤）——token 齐全，
/// 新主题只要填满这几个字段。
struct HousePalette {
    let bg: Color            // 页面背景（最深）
    let surface: Color       // 房间卡片 / 面板底
    let surfaceHi: Color     // 抬高一层的面板（未选 tab、输入框底）
    let accent: Color        // 强调色（选中 tab、按钮、你在这里）
    let onAccent: Color      // 强调色上的文字
    let textPrimary: Color   // 主文字
    let textSecondary: Color // 次要文字（空房、时间戳、系统灰线）
    let line: Color          // 描边 / 分隔线
}

extension HousePalette {
    /// 蜂蜜焦糖（2026-08-16 从 v0 稿采样；机主给精确色值后在此替换）。
    static let honeyCaramel = HousePalette(
        bg: Color(hex: 0x150A06),
        surface: Color(hex: 0x241710),
        surfaceHi: Color(hex: 0x2D1E18),
        accent: Color(hex: 0xE29858),
        onAccent: Color(hex: 0x2D1E18),
        textPrimary: Color(hex: 0xCDC2B9),
        textSecondary: Color(hex: 0xA69489),
        line: Color(hex: 0x3A281D)
    )
}

extension Color {
    /// 当前生效的主题（以后做换主题时，改成从 UserDefaults 读用户选中的那套）。
    static let current = ThemePalette.violet

    /// 小屋当前主题：换主题改这一行。
    static let house = HousePalette.honeyCaramel

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
