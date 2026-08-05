import Foundation
import Markdown

/// 一条消息切出来的一段：对白进气泡，块级内容进卡片。
enum MessageSegment {
    case bubble(String)   // 对白（含列表/表格/引用，都在气泡里）
    case card(String)     // 代码块（脱出气泡，深色块，靠发送者侧、同气泡列宽）
}

/// 聊天习惯：段内单个换行也当"硬换行"保留（Markdown 默认会把单换行折成空格）。
/// 行尾补两个空格＝Markdown 硬换行；空行仍作段落分隔。只用于对白气泡，不碰代码/表格。
func hardenLineBreaks(_ s: String) -> String {
    s.components(separatedBy: "\n\n")
        .map { $0.replacingOccurrences(of: "\n", with: "  \n") }
        .joined(separator: "\n\n")
}

/// 快速判断有没有块级元素——没有就走单气泡快路，不必解析。
func hasBlockMarkdown(_ text: String) -> Bool {
    for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.hasPrefix("```") || line.hasPrefix("~~~") { return true }         // 代码围栏
        if line.hasPrefix(">") { return true }                                    // 引用
        if line.hasPrefix("- ") || line.hasPrefix("* ") || line.hasPrefix("+ ") { return true }  // 无序列表
        if line.range(of: #"^\d+[.)]\s"#, options: .regularExpression) != nil { return true }     // 有序列表
        if line.contains("|"),
           line.range(of: #"^\|?[\s:|-]*-[\s:|-]*\|?$"#, options: .regularExpression) != nil {    // 表格分隔行
            return true
        }
    }
    return false
}

/// 用 swift-markdown 按顶层块把消息切成有序的"对白气泡段 + 块级卡片段"。
/// 纯对白（无块级）直接返回单气泡，不解析。
func messageSegments(_ text: String) -> [MessageSegment] {
    guard hasBlockMarkdown(text) else { return [.bubble(hardenLineBreaks(text))] }

    let document = Document(parsing: text)
    var segments: [MessageSegment] = []
    var prose: [String] = []

    func flushProse() {
        if !prose.isEmpty {
            segments.append(.bubble(prose.joined(separator: "\n\n")))
            prose.removeAll()
        }
    }

    // 只有代码块脱出气泡；列表/表格/引用留在气泡里（当对白段处理）。
    for block in document.blockChildren {
        if block is CodeBlock {
            flushProse()
            segments.append(.card(block.format()))
        } else {
            let md = block.format()
            // 只有段落/标题的段内单换行要硬化；列表/表格/引用的 markdown 保持原样别动。
            prose.append((block is Paragraph || block is Heading) ? hardenLineBreaks(md) : md)
        }
    }
    flushProse()

    return segments.isEmpty ? [.bubble(hardenLineBreaks(text))] : segments
}
