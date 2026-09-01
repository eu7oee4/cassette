import SwiftUI

/// 聊天页整套色 token（PLAN_chatui §1.1）：浅/深各显式一份，深浅切换整套换。
/// bg 目前仍取系统语义色（观感与从前一致）；背景图 bgImage/bgImageDim 是 U6 的活。
struct ChatPalette {
    let bg: Color            // 聊天流背景
    let bubbleAI: Color      // AI 气泡统一灰底（§3.1，跟是哪个角色无关）
    let textOnDark: Color    // 机主命名 text_light：深底上的浅色字
    let textOnLight: Color   // 机主命名 text_dark：浅底上的深色字
    let theme: Color         // 主题色（§1.4 拍板：保留但暂无处可用，别硬塞）
}

extension ChatPalette {
    static let light = ChatPalette(
        bg: Color(uiColor: .systemGroupedBackground),
        bubbleAI: Color(uiColor: .systemGray5),
        textOnDark: .white,
        textOnLight: .black,
        theme: Color(hex: 0x352526)
    )
    static let dark = ChatPalette(
        bg: Color(uiColor: .systemGroupedBackground),
        bubbleAI: Color(uiColor: .systemGray5),
        textOnDark: .white,
        textOnLight: .black,
        theme: Color(hex: 0x352526)
    )

    static func current(_ scheme: ColorScheme) -> ChatPalette {
        scheme == .dark ? .dark : .light
    }
}

/// 人物色一套（PLAN_chatui §1.1）：base 本体（头像描边/名字/默认头像），
/// light 浅身＝深色模式的染色底，dark 深身＝浅色模式的染色底，
/// bullet 气泡内小装饰（用途待定 §8），logo 小字提醒的前缀符号（U3 接线，
/// 机主的符号待定 §8，🐾 的实色 SVG 也是 U3 的活——先拿字符占位）。
struct CharPalette {
    let base: Color
    let light: Color
    let dark: Color
    let bullet: Color
    let logo: String
}

/// §1.2 染色的唯一口径（全局只有这一条）：
/// 浅色模式 → 人物色的 dark 当底 + text_light（白字）；
/// 深色模式 → 人物色的 light 当底 + text_dark（黑字）。
/// 适用面只有两个：机主的气泡、要机主动手的卡片（permit 卡/问答卡）。
enum ChatTint {
    static func fill(_ p: CharPalette, _ scheme: ColorScheme) -> Color {
        scheme == .dark ? p.light : p.dark
    }
    static func text(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? ChatPalette.dark.textOnLight : ChatPalette.light.textOnDark
    }
}

/// 小屋（同居世界）的整套配色：房子/房间视图是一个独立的暖色深色世界，不随系统深浅色走。
/// 换主题 = 换 `Color.house` 指向的 palette（一处改，整个小屋换肤）——token 齐全，
/// 新主题只要填满这几个字段。
struct HousePalette {
    let bg: Color            // 页面背景
    let surface: Color       // 房间卡片 / 面板底
    let surfaceHi: Color     // 抬高一层的面板（未选 tab、输入框底）＝原稿的 tint
    let accent: Color        // 强调色（选中 tab、按钮、你在这里）
    let accentLight: Color   // 强调色的浅身（头像描边、弱强调）
    let onAccent: Color      // 强调色上的文字
    let textPrimary: Color   // 主文字
    let textSecondary: Color // 次要文字（空房、时间戳、系统灰线）＝原稿的 subtle
    let line: Color          // 描边 / 分隔线＝原稿的 border
}

extension HousePalette {
    /// 蜂蜜焦糖（机主 2026-08-16 给的精确色值，浅色奶油系）。
    static let honeyCaramel = HousePalette(
        bg: Color(hex: 0xF7F0E3),
        surface: Color(hex: 0xFFFDF5),
        surfaceHi: Color(hex: 0xF5E4C8),
        accent: Color(hex: 0xC0793A),
        accentLight: Color(hex: 0xDEBA8A),
        onAccent: Color(hex: 0xFFFDF5),
        textPrimary: Color(hex: 0x33260F),
        textSecondary: Color(hex: 0x917A5A),
        line: Color(hex: 0xE9DEC9)
    )

    /// 燕麦奶咖（机主 2026-08-16 第三套，浅米底 + 深棕强调）。
    static let oatMilk = HousePalette(
        bg: Color(hex: 0xF5EFE6),
        surface: Color(hex: 0xFEFCF6),
        surfaceHi: Color(hex: 0xECDDC8),      // tint
        accent: Color(hex: 0x7D5A44),
        accentLight: Color(hex: 0xB89B82),
        onAccent: Color(hex: 0xFEFCF6),
        textPrimary: Color(hex: 0x2C2013),
        textSecondary: Color(hex: 0x8A7A65),  // subtle
        line: Color(hex: 0xE3D8C8)            // border
    )

    /// 玫瑰灰（机主 2026-08-16 第二套，暖黑底 + 玫瑰红强调）。
    static let roseAsh = HousePalette(
        bg: Color(hex: 0x181716),
        surface: Color(hex: 0x252322),
        surfaceHi: Color(hex: 0x352526),      // tint
        accent: Color(hex: 0xB86166),
        accentLight: Color(hex: 0x6E3B3E),
        onAccent: Color(hex: 0xECE8E4),
        textPrimary: Color(hex: 0xECE8E4),
        textSecondary: Color(hex: 0x908A84),  // subtle
        line: Color(hex: 0x363231)            // border
    )
}

/// 角色识别色：「谁说的」一眼可辨——名字、头像描边、默认头像、染色底都从这儿查。
/// 与主题解耦（换主题不换身份色）；新角色没配就按 id 哈希出一套稳定色。
/// light/dark 两身是 2026-09-01 的第一版（base 掺 40% 白 / 掺 25% 黑），看效果再调。
enum IdentityColor {
    static func palette(for entityID: String) -> CharPalette {
        switch entityID {
        case "user":            // 眠眠：玫瑰
            return CharPalette(base: Color(hex: 0xB86166),
                               light: Color(hex: 0xD4A0A3),
                               dark: Color(hex: 0x8A494D),
                               bullet: Color(hex: 0xB86166),
                               logo: "")                    // 机主的符号待定（§8）
        case "default":         // 小卡：雾蓝
            return CharPalette(base: Color(hex: 0x7FA8C9),
                               light: Color(hex: 0xB2CBDE),
                               dark: Color(hex: 0x5F7E97),
                               bullet: Color(hex: 0x7FA8C9),
                               logo: "🐾")
        case "cass":            // Cassius：琥珀
            return CharPalette(base: Color(hex: 0xC9A15B),
                               light: Color(hex: 0xDFC79D),
                               dark: Color(hex: 0x977944),
                               bullet: Color(hex: 0xC9A15B),
                               logo: "✦")
        default:
            var h = 0
            for u in entityID.unicodeScalars { h = (h &* 31 &+ Int(u.value)) & 0xFFFF }
            let hue = Double(h % 360) / 360.0
            return CharPalette(base: Color(hue: hue, saturation: 0.38, brightness: 0.72),
                               light: Color(hue: hue, saturation: 0.30, brightness: 0.86),
                               dark: Color(hue: hue, saturation: 0.45, brightness: 0.52),
                               bullet: Color(hue: hue, saturation: 0.38, brightness: 0.72),
                               logo: "·")
        }
    }

    static func color(for entityID: String) -> Color { palette(for: entityID).base }
}

extension Color {
    /// 小屋当前主题：换主题改这一行。
    static let house = HousePalette.oatMilk

    /// 机主人物色（PLAN_chatui §1.4 拍板：凡要用色的地方一律用它——按钮/角标/
    /// 链接/默认头像；主题色 theme 另存在 ChatPalette 里，暂时无处可用）。
    static var userAccent: Color { IdentityColor.palette(for: "user").base }

    /// 0xRRGGBB 十六进制建色。
    init(hex: UInt) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xff) / 255,
                  green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255,
                  opacity: 1)
    }
}
