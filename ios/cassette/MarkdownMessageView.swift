import SwiftUI
import MarkdownUI
import Highlightr

/// 把消息里的 Markdown 原文渲染成对应格式（粗体/斜体/删除线/行内码/链接/列表/引用/表格/代码块）。
/// 存储层始终保留原文，只在这里渲染。
struct MarkdownMessageView: View {
    let text: String

    var body: some View {
        Markdown(Self.cjkEmphasisFixed(text))
            .markdownTheme(theme)
            .markdownCodeSyntaxHighlighter(HighlightrCodeSyntaxHighlighter.shared)
            // 真机上 MarkdownUI 对"混合样式的长段落"（粗体+正文+行内码混排、列表项）在气泡
            // maxWidth 约束下测高不足 → 压成省略号截断（模拟器不复现，实机复现）。
            // 整个视图强制"宽随容器、高随内容"，段落/列表一起兜住。
            .fixedSize(horizontal: false, vertical: true)
        // 注意：不开 .textSelection——系统「选中文本」会和气泡的长按(切模式)/双击(翻面)抢手势。
        // 复制文字走时间戳旁的 square.on.square 按钮，代码块走它自己的复制按钮。
    }

    /// 修 CommonMark 对中文的加粗判定：`……的。**一旦` 这种「闭合 ** 前是 CJK 标点、后紧跟文字」
    /// 按规范不算合法闭合，整段 ** 原样漏出。在每对 ** 的内容两端垫零宽空格(U+200B，不可见)，
    /// 让定界符两侧都是"非空白非标点"，强调必然成立。只在渲染层做，存储/复制仍是原文。
    /// 反引号分段：奇数段=行内码/代码围栏内容，不动（``` 围栏恰好也把内容切在奇数段上）。
    static func cjkEmphasisFixed(_ s: String) -> String {
        guard s.contains("**") else { return s }
        let parts = s.components(separatedBy: "`")
        let fixed = parts.enumerated().map { i, part -> String in
            guard i % 2 == 0 else { return part }
            return part.replacingOccurrences(
                of: #"\*\*(?=\S)([^*]+?)(?<=\S)\*\*"#,
                with: "**\u{200B}$1\u{200B}**",
                options: .regularExpression
            )
        }
        return fixed.joined(separator: "`")
    }

    private var theme: MarkdownUI.Theme {
        MarkdownUI.Theme()
            .text {
                ForegroundColor(.primary)
            }
            .link {
                ForegroundColor(.accentColor)
                UnderlineStyle(.single)
            }
            .emphasis {
                // *斜体* 用来标"动作/状态"（旁白），和对白区分：中文没斜体字形，改用淡色；
                // 英文顺带保留斜体。
                FontStyle(.italic)
                ForegroundColor(Color.primary.opacity(0.5))
            }
            .code {
                FontFamilyVariant(.monospaced)
                FontSize(.em(0.88))
                BackgroundColor(Color(.systemGray4))
            }
            .codeBlock { configuration in
                CodeBlockView(configuration: configuration)
            }
            .listItem { configuration in
                // MarkdownUI 已知毛病：列表项在宽度受限容器（气泡 maxWidth）里测高不足，
                // 长文换行会被压成省略号截断。强制"宽度随容器、高度随内容"就恢复正常换行。
                configuration.label
                    .fixedSize(horizontal: false, vertical: true)
            }
            .blockquote { configuration in
                configuration.label
                    .padding(.leading, 12)
                    .overlay(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 1.5)
                            .fill(Color.gray.opacity(0.5))
                            .frame(width: 3)
                    }
                    .markdownTextStyle { ForegroundColor(Color.primary.opacity(0.75)) }
            }
            .table { configuration in
                // 宽表格能左右滑，不被裁掉。
                ScrollView(.horizontal, showsIndicators: false) {
                    configuration.label
                }
                .markdownMargin(top: 6, bottom: 6)
            }
    }
}

/// 代码块：深色卡片 + 顶栏（语言标签 + 一键复制）+ 语法高亮内容（可横向滚动）。
private struct CodeBlockView: View {
    let configuration: CodeBlockConfiguration
    @State private var copied = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(configuration.language ?? "")
                    .font(.caption2)
                    .foregroundStyle(.white.opacity(0.55))
                Spacer()
                Button(action: copy) {
                    Image(systemName: copied ? "checkmark" : "square.on.square")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.85))
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)

            ScrollView(.horizontal, showsIndicators: false) {
                configuration.label
                    .relativeLineSpacing(.em(0.2))
                    .padding(.horizontal, 12)
                    .padding(.bottom, 12)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(Color(red: 0.16, green: 0.17, blue: 0.21))   // atom-one-dark ≈ #282c34
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .markdownMargin(top: 8, bottom: 8)
    }

    private func copy() {
        UIPasteboard.general.string = configuration.content
        copied = true
        Task {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            copied = false
        }
    }
}

/// 用 Highlightr（highlight.js）给代码块上色，接进 MarkdownUI 的高亮协议。
struct HighlightrCodeSyntaxHighlighter: CodeSyntaxHighlighter {
    static let shared = HighlightrCodeSyntaxHighlighter()

    private let highlightr: Highlightr?

    init() {
        let hl = Highlightr()
        hl?.setTheme(to: "atom-one-dark")
        self.highlightr = hl
    }

    func highlightCode(_ code: String, language: String?) -> Text {
        guard let highlightr,
              let attributed = highlightr.highlight(code, as: language, fastRender: true) else {
            return Text(code).foregroundColor(.white)
        }
        return Text(AttributedString(attributed))
    }
}
