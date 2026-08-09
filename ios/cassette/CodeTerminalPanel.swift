import SwiftUI

/// 面板此刻画出来多高——报给 ContentView，它转手给 ChatView 当气泡的内容内边距，
/// 让最新气泡正好停在黑条上边。**只报值、不改 frame**，所以不会反过来撑大气泡区（那就成环了）。
struct TerminalHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// 当前档位（0/⅔/1）。ContentView 拿它当「气泡那边要不要跟着动画」的开关：
/// **只有人主动换档才动画**，弹窗选项进出导致的高度变化保持瞬时——
/// 后者一动画就是整个气泡列表跟着重排 220 毫秒，正是之前那个卡顿+虚影。
struct TerminalRatioKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// Code 模式的内联终端：**盖在**气泡区上（不是压缩它），不用切页面就能看他在干什么、
/// 按掉权限弹窗。
///
/// 交互（结构从上到下：黑条 → 画面 → 按钮行，再往下才是输入框）：
/// - 黑条单击开满屏、再单击收回；右侧那个「⅔」按钮切中间那一档。
/// - 档位别改回双击：双击手势一在，单击就得等判定窗口过期才响应，肉眼可见地迟钝。
///
/// **高度只有一个上限：气泡区有多高**（`available`，由外面量好传进来）。
/// 以前是拿屏高一路减（顶栏/键盘/附件条/按钮行/给输入框预留的余地）估出来的，估算链上
/// 任一环偏了就出事：估出来的值触到 `max(100,…)` 地板时面板不再缩，多出来的从顶上溢出
/// 盖住顶栏（实测：5 个弹窗选项 + 图 + 文件 + 键盘时盖住 86pt）。现在夹进气泡区就完事——
/// 满档正好严丝合缝贴在顶栏下面，空间不够就自己裁，**越不过去**。
/// 键盘、附件条、输入框长高也都不用单独算了：它们一动气泡区就变矮，`available` 自己跟上。
///
/// 画面的**显示范围和内容滚动位置是两回事**——画面永远按可用高度布局、内容底部对齐，
/// 外面那层 frame 只是个裁剪窗口。所以不管切到哪一档，最新输出始终贴在窗口底边。
/// 要往回翻旧内容，在画面里上滑。
struct CodeTerminalPanel: View {
    let service: ChatService
    @Binding var expanded: Bool
    /// 气泡区此刻有多高 = 面板高度的唯一上限。
    var available: CGFloat

    @State private var content = ""
    @State private var dialog: [ChatService.CodeDialogOption] = []
    @State private var alive = true
    @State private var lastOk = Date()
    @State private var now = Date()             // 驱动"多久没刷新"的显示
    /// 档位存**比例**不存绝对高度（0=收起 / 0.62=2/3 屏 / 1=占满）：键盘弹出、贴了照片、
    /// 弹窗按钮变多——可用空间一变，比例不动、高度自己缩放，不用记原来是哪一档。
    @State private var stopRatio: CGFloat = 0
    @State private var scrolling = false         // 手指正在画面里滑
    @State private var heldContent: String? = nil // 滑动期间攒下的新画面，落定再上

    /// 画面刷新间隔：展开时跟得紧；收起时只为了知道"有没有弹窗在等"，慢慢来就行。
    private let fastPoll: Duration = .milliseconds(1200)
    private let slowPoll: Duration = .seconds(6)
    private let barHeight: CGFloat = 34
    private let staleAfter: TimeInterval = 5    // 超过这么久没刷出来就明说（别无声冻住）
    private let halfRatio: CGFloat = 0.62       // 中间那一档

    /// 按钮行想占多高（弹窗选项是竖排的，五个选项就是一大条，底下还有一排方向键）。
    private var keyBarWants: CGFloat {
        dialog.isEmpty ? 50 : CGFloat(dialog.count) * 46 + 55
    }

    /// 面板总高：收起时只剩黑条；展开时按比例取气泡区的一段，夹进气泡区。
    private var totalHeight: CGFloat {
        guard stopRatio > 0 else { return barHeight }
        return max(barHeight, min(stopRatio * available, available))
    }
    /// 黑条以下还剩多少，给「画面 + 按钮行」分。
    private var below: CGFloat { max(0, totalHeight - barHeight) }
    /// ⚠️ 按钮行装不下时让它**自己滚**，绝不去裁黑条：黑条一没，人就没法收回终端了
    /// （5 个选项 + 键盘时是真的装不下，只能靠人先收键盘——那也得有个黑条可点）。
    private var keyBarHeight: CGFloat { min(keyBarWants, below) }
    private var screenHeight: CGFloat { max(0, below - keyBarHeight) }

    private var isStale: Bool { now.timeIntervalSince(lastOk) > staleAfter }

    var body: some View {
        VStack(spacing: 0) {
            grabBar
            if below > 0 {
                screen
                keyBar
            }
        }
        .background(Color.terminalBG)
        .frame(height: totalHeight, alignment: .top)
        .clipped()
        // ⚠️ clipped() 只裁绘制、不裁命中测试——补这句，不然看不见的部分照样吃触摸。
        .contentShape(Rectangle())
        .preference(key: TerminalHeightKey.self, value: totalHeight)
        .preference(key: TerminalRatioKey.self, value: stopRatio)
        // 外部改了展开状态（点黑条 / 退出 Code 模式）→ 档位跟上。
        // ⚠️ 收起时才无条件归零；打开时**只在原本收着的情况下**给满档——否则从收起状态点
        // 「⅔」时，setRatio 先设好 0.62、再把 expanded 翻成 true，这里会顺手把它重置成
        // 全屏，人就永远停不到 2/3。
        .onChange(of: expanded) { _, on in
            if !on { stopRatio = 0 } else if stopRatio == 0 { stopRatio = 1 }
        }
        // 键盘不用自己监听了：键盘一弹，气泡区就变矮，available 自己跟上。
        .onAppear { stopRatio = expanded ? 1 : 0 }
        .task(id: expanded) {
            // 轮询：展开时 1.2s，收起时 6s。离开这个视图（退出 Code 模式）自动取消。
            await poll()
            while !Task.isCancelled {
                try? await Task.sleep(for: expanded ? fastPoll : slowPoll)
                await poll()
                now = Date()
            }
        }
    }

    // MARK: - 黑条（顶部条 = 抓手）

    private var grabBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "chevron.compact.up")
                .rotationEffect(.degrees(below > 0 ? 180 : 0))
                .foregroundStyle(.white.opacity(0.75))
            Text(below > 0 ? "点击此处收回终端窗口" : "点击此处展开终端窗口")
                .font(.system(size: 12))
                .foregroundStyle(.white.opacity(0.9))
            // 收起时也要知道他卡在弹窗上了——不然人在聊天里干等，那边等着按键。
            if below == 0 && !dialog.isEmpty {
                Text("待确认")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.black)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.orange))
            }
        }
        .frame(maxWidth: .infinity)
        .frame(height: barHeight)
        .background(Color.terminalBar)
        .contentShape(Rectangle())
        .onTapGesture { setRatio(stopRatio > 0 ? 0 : 1) }
        // 中间那一档单独给个按钮：放在黑条上当双击的话，单击就得等双击判定窗口过期，
        // 开合会肉眼可见地迟半拍。
        .overlay(alignment: .trailing) {
            Button { setRatio(stopRatio == halfRatio ? 1 : halfRatio) } label: {
                Text("⅔")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(stopRatio == halfRatio ? .black : .white.opacity(0.75))
                    .frame(width: 30, height: 24)
                    .background(Capsule().fill(stopRatio == halfRatio
                                               ? Color.white.opacity(0.85) : Color.white.opacity(0.12)))
            }
            .padding(.trailing, 10)
        }
    }

    /// 换档：**动画只加在这儿**，不用 `.animation(value:)` 罩着整个面板。
    /// 罩着的话，弹窗选项一出现（keyBar 变高 → 总高变）也会被当成一次动画，把同批变化的
    /// 按钮换人、终端正文换字一起交叉淡出——那就是"选项卡弹出时卡一下、还带个虚影"。
    /// 高度因为选项/键盘/附件条变化时应该是瞬时的，只有人主动换档才滑。
    private func setRatio(_ r: CGFloat) {
        withAnimation(.easeOut(duration: 0.22)) {
            stopRatio = r
            expanded = r > 0
        }
    }

    // MARK: - 画面

    private var screen: some View {
        TerminalScreen(
            text: content.isEmpty ? (alive ? "连接中…" : "") : content,
            // 手指在滑就别换内容：轮询 1.2 秒一次，正好在滑到一半时换掉整屏，
            // 滚动会被打断一下（"滑一次、中间顿一下、再接着滑完"）。攒着，落定再上。
            onScrollingChanged: { moving in
                scrolling = moving
                if !moving, let held = heldContent {
                    content = held
                    heldContent = nil
                }
            },
            // 点画面收键盘（打字时想看终端，点一下就行，不用先收键盘再点）。
            // 走 UIKit 的手势而不是 SwiftUI 的 simultaneousGesture：底下是个 UIScrollView，
            // 手势谁先谁后不好赌。
            onTap: dismissKeyboard
        )
        .frame(height: screenHeight)
        .clipped()
        .overlay(alignment: .top) {
            if !alive {
                notice("会话不在了（回顶栏切一次 Code 模式）", color: .red)
            } else if isStale {
                notice("画面 \(Int(now.timeIntervalSince(lastOk))) 秒没刷新（在重试）", color: .orange)
            }
        }
    }

    private func notice(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 11))
            .foregroundStyle(.black)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 4)
            .background(color.opacity(0.85))
    }

    // MARK: - 按钮行（永远贴底）
    //
    // 装不下才套 ScrollView：五个选项 + 键盘弹着的时候，黑条以下的空间放不下整排按钮，
    // 让它自己滚，**绝不靠裁**——从上往下裁第一个没的就是黑条，人就再也收不回终端了。
    //
    // ⚠️ 但**装得下就别套**：ScrollView 底下那个 UIScrollView 的 delaysContentTouches
    // 会把触摸先扣住一会儿等着看是不是滑动，按钮就变成「轻点没反应、按久一点才亮」
    // （实机报的）。弹窗按钮是要一点就中的东西，平时这条路上不该有滚动视图。

    @ViewBuilder
    private var keyBar: some View {
        Group {
            if keyBarWants > keyBarHeight {
                ScrollView(.vertical) { keyBarContent }
                    .scrollBounceBehavior(.basedOnSize)
            } else {
                keyBarContent
            }
        }
        .frame(height: keyBarHeight)
        .background(Color.terminalBar)
    }

    private var keyBarContent: some View {
        VStack(spacing: 6) {
            if dialog.isEmpty {
                HStack(spacing: 6) {
                    ForEach(Self.commonKeys, id: \.key) { item in
                        keyButton(item.label) { press(item.key) }
                    }
                }
            } else {
                // 弹窗等着按键：**按钮就是弹窗里的选项**，有几个显示几个、文案照抄原文。
                ForEach(dialog) { opt in
                    Button { press(opt.key) } label: {
                        HStack(spacing: 8) {
                            Text(opt.key)
                                .font(.system(size: 12, weight: .bold, design: .monospaced))
                                .foregroundStyle(.black)
                                .frame(width: 20, height: 20)
                                .background(Circle().fill(Color.orange))
                            Text(opt.label)
                                .font(.system(size: 13))
                                .foregroundStyle(.white)
                                .lineLimit(2)
                                .multilineTextAlignment(.leading)
                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.terminalKey))
                    }
                }
                // 方向键只在弹窗这一支给：有些面板（选文件、挑方案）要上下移光标。
                // 平时不给，见 commonKeys 上面那段。
                HStack(spacing: 6) {
                    keyButton("↑") { press("Up") }
                    keyButton("↓") { press("Down") }
                    keyButton("Esc") { press("Escape") }
                }
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 8)
    }

    /// 没弹窗时的常用键（顺序就是屏幕上从左到右的顺序）。
    ///
    /// ⚠️ **这里没有 ↑↓，别加回来**：空输入框按一下 ↑ 会把上一条消息整个调回输入框，
    /// 而那条往往是一万多字的注入上下文；之后你在聊天里发的消息会被它整个当成前缀带出去
    /// （实锤过一次，76 字的消息变成 13470 字）。方向键只在弹窗里有用——放到弹窗那一支去。
    private static let commonKeys: [(key: String, label: String)] = [
        ("Enter", "⏎"), ("Escape", "Esc"), ("C-c", "^C"), ("Tab", "Tab"),
    ]

    private func keyButton(_ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: 13))
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.terminalKey))
        }
    }

    // MARK: - 后端往来

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder),
                                        to: nil, from: nil, for: nil)
    }

    private func press(_ keys: String) {
        Task {
            try? await service.codeKeys(keys)
            try? await Task.sleep(for: .milliseconds(300))   // 等画面反应过来再抓
            await poll()
        }
    }

    private func poll() async {
        guard let s = try? await service.codeCapture() else { return }   // 抖一下就下轮再来
        // 只在真变了才赋值：画面没动的时候（TA 在想、在跑长命令）不去碰 @State，
        // 省掉一次两百多行文本的重画。滚动时这一下最明显。
        if s.alive != alive { alive = s.alive }
        if s.content != content {
            // 手指在滑的时候先攒着（见 onScrollPhaseChange），别打断滚动
            if scrolling { heldContent = s.content } else { content = s.content }
        }
        let opts = s.dialog ?? []
        // ⚠️ 整体比（key + 文案），**别退回只比 key**：权限弹窗的编号恒定是 1/2/3，带信息的
        // 是文案（"Yes, and don't ask again for `git` commands in …"）。两个弹窗落在同一个
        // 轮询间隔里（中间那一帧"没弹窗"没被扫到）时，只比 key 就换不上——屏幕上是上一个
        // 弹窗的文案，按下去执行的却是这一个的语义。
        if opts != dialog { dialog = opts }
        lastOk = Date()
        now = Date()
    }
}

// MARK: - 终端画面（UIKit 的 UITextView，不是 SwiftUI 的 Text）
//
// 为什么不用 Text：一屏是 80 列 × 240 行 ≈ 1.9 万字，SwiftUI 的 Text 不是懒的——
// 每换一次内容就得把整段重新排版一遍，而 TA 在跑活的时候画面每 1.2 秒变一次。
// 那就是「生成中滑气泡/滑终端/打字都时不时卡一下、TA 一停下来就顺了」的来源：
// 停下来时画面不变，poll() 里 `s.content != content` 那道守卫压根不赋值，也就不排版。
// TextKit 只排看得见的那几十行，换字是增量的。

/// 不折行 + 能横滑 + 换内容/换档之后把最新输出重新贴到窗口底边。
private final class BottomPinnedTextView: UITextView {
    private var lastSize: CGSize = .zero

    /// 内容想要多宽（不折行的话最长那行得摆得下）。
    /// ⚠️ 光在换内容时设一次 `textContainer.size` 没用——**UITextView 每次
    /// layoutSubviews 都会把容器宽度重设回自己的 bounds**（实测：设成 1111，一次布局
    /// 之后变回 370），于是照样按屏宽折行。必须每次布局之后再盖回去。
    private(set) var desiredWidth: CGFloat = 0

    /// 画布宽**只增不减**。
    /// ⚠️ 别每帧照着当前内容重设：这个值会随画面变（顶上那条 80 列框线随输出往上滚，
    /// 滚出 pane 之后最长行就从 544 掉到 430）。contentSize 一缩，UIScrollView 就把
    /// 横向偏移夹回去——表现是「往右滑，过一会儿自己弹回最左」（实机报的）。
    func growDesiredWidth(to w: CGFloat) {
        guard w > desiredWidth else { return }
        desiredWidth = w
        setNeedsLayout()
    }

    private func applyWidth() {
        guard desiredWidth > 0 else { return }
        let pad = textContainerInset.left + textContainerInset.right
        let w = max(desiredWidth, bounds.width - pad)
        if textContainer.size.width != w {
            textContainer.size = CGSize(width: w, height: .greatestFiniteMagnitude)
        }
        // contentSize.width 也不会自己跟着容器走（UITextView 把它钉在 bounds 宽），
        // 不盖这一下就是「不折行了，但横着滑不动、右边看不到」。
        let content = max(w + pad, bounds.width)
        if abs(contentSize.width - content) > 0.5 {
            contentSize = CGSize(width: content, height: contentSize.height)
        }
    }

    override func layoutSubviews() {
        applyWidth()
        super.layoutSubviews()
        applyWidth()
        restorePinnedX()
        // 只在窗口尺寸真变了才重贴（换档、键盘进出）。每次布局都贴会跟用户上滑打架。
        if bounds.size != lastSize {
            lastSize = bounds.size
            pinToBottom()
        }
    }

    /// 贴底：拿**最后一个字的排版矩形**算落点，然后自己设偏移。
    ///
    /// 两条都别走：
    /// - `contentSize.height - bounds.height`：contentSize 是惰性估的，刚换完内容还没定，
    ///   落点忽对忽错（症状：展开全屏有时停在开头还滚不动，下次又好了）。
    /// - `scrollRangeToVisible(结尾)`：它**连横向一起滚**——最后一行短，"露出最后一个字"
    ///   就等于把人拽回行首；而且是延后执行的，事后再把横向偏移改回来追不上。
    ///   症状：往右滑，一到下次刷新（1.2 秒）就弹回最左（实机报的）。
    ///
    /// `boundingRect(forGlyphRange:)` 只会把要问的那一小段排出来，惰性不丢，落点又准。
    func pinToBottom() {
        let ns = text as NSString
        guard ns.length > 0 else { return }
        let last = NSRange(location: ns.length - 1, length: 1)
        let glyphs = layoutManager.glyphRange(forCharacterRange: last, actualCharacterRange: nil)
        let rect = layoutManager.boundingRect(forGlyphRange: glyphs, in: textContainer)
        guard rect.height > 0 else { return }
        let bottom = rect.maxY + textContainerInset.top + textContainerInset.bottom
        let y = max(0, bottom - bounds.height)
        if abs(contentOffset.y - y) > 0.5 {
            // 只动纵向，横向原样留着
            setContentOffset(CGPoint(x: contentOffset.x, y: y), animated: false)
        }
    }

    /// 用户此刻横向停在哪。
    /// ⚠️ **给 `text` 赋值会把滚动位置清零**，而且是**延后**清的——画面每 1.2 秒换一次，
    /// 就等于每 1.2 秒把横向偏移抹一次（实机症状：往右滑，过一会儿自己弹回最左）。
    /// 赋值后立刻读 contentOffset 还是旧值，当场"恢复"根本判不出来要恢复。
    /// 所以和容器宽度同一个套路：**每次布局之后照着这个值盖回去**；用户自己在滑的时候
    /// 才更新它。
    private var pinnedX: CGFloat = 0

    /// 手指驱动的横向滚动 → 记下新位置（由 delegate 调）。
    func rememberScrollX() { pinnedX = contentOffset.x }

    private func restorePinnedX() {
        guard !isDragging, !isDecelerating else { return }   // 人在滑，别抢
        let maxX = max(0, contentSize.width - bounds.width)
        let want = min(pinnedX, maxX)
        if abs(contentOffset.x - want) > 0.5 {
            contentOffset.x = want
        }
    }

    /// 再补一拍：万一这一帧的布局还没落定，下一个 runloop 再贴一次。
    /// 手指正按着的时候不抢（那是人在自己翻）。
    func pinToBottomSoon() {
        pinToBottom()
        DispatchQueue.main.async { [weak self] in
            guard let self, !self.isDragging, !self.isDecelerating else { return }
            self.pinToBottom()
        }
    }
}

private struct TerminalScreen: UIViewRepresentable {
    let text: String
    let onScrollingChanged: (Bool) -> Void
    let onTap: () -> Void

    private static let font = UIFont.monospacedSystemFont(ofSize: 11, weight: .regular)

    func makeUIView(context: Context) -> BottomPinnedTextView {
        // ⚠️ 必须 TextKit 1（`usingTextLayoutManager: false`）。iOS 16 起 UITextView 默认
        // 走 TextKit 2，那边 `textContainer.size` 只是个参考值、排版由 NSTextLayoutManager
        // 自己说了算——设了不折行的容器宽度也照样按视图宽折行（实机截图里 80 列的画面被
        // 折成两行，对齐全乱），而且 contentSize 是惰性估算的，贴底会算错。
        // TextKit 1 里「容器宽度写死 + widthTracksTextView=false」是横向滚动的老配方，
        // 惰性排版靠 allowsNonContiguousLayout 拿回来，性能不亏。
        let tv = BottomPinnedTextView(usingTextLayoutManager: false)
        tv.layoutManager.allowsNonContiguousLayout = true   // 只排看得见的那几十行
        tv.isEditable = false
        // 不开选择：选择支持要给每个字建可命中的区域，画面一直在变的时候这一项就是大头。
        tv.isSelectable = false
        tv.font = Self.font
        tv.textColor = UIColor(Color.terminalText)
        tv.backgroundColor = UIColor(Color.terminalBG)
        tv.textContainerInset = UIEdgeInsets(top: 8, left: 10, bottom: 8, right: 10)
        tv.textContainer.lineFragmentPadding = 0
        // 不折行：终端画面按 80 列排的，折一行整块对齐就毁了。宽度不跟着视图走 +
        // 按裁剪处理换行，内容自己横向铺开，UITextView 自带的横向滚动就能翻。
        tv.textContainer.widthTracksTextView = false
        tv.textContainer.heightTracksTextView = false
        tv.textContainer.lineBreakMode = .byClipping
        tv.showsHorizontalScrollIndicator = false
        tv.contentInsetAdjustmentBehavior = .never   // 贴底算式里不用再兜安全区
        // 滚动**不**收键盘：常有的用法是翻到前面照着上面的内容打字，一滚就收反而碍事。
        tv.keyboardDismissMode = .none
        tv.delegate = context.coordinator

        let tap = UITapGestureRecognizer(target: context.coordinator,
                                         action: #selector(Coordinator.handleTap))
        tap.cancelsTouchesInView = false   // 别把触摸从滚动手势那儿抢走
        tv.addGestureRecognizer(tap)
        return tv
    }

    func updateUIView(_ tv: BottomPinnedTextView, context: Context) {
        context.coordinator.onScrollingChanged = onScrollingChanged
        context.coordinator.onTap = onTap
        guard tv.text != text else { return }
        tv.growDesiredWidth(to: Self.contentWidth(text))   // 见 applyWidth / growDesiredWidth
        tv.text = text                                     // 横向偏移由 restorePinnedX 兜住
        tv.pinToBottomSoon()
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    /// 终端 pane 的列数（后端 code_bridge.PANE_WIDTH）。等宽字体下「80 列」就是画面的
    /// 右边界，也是任何一行的宽度上限——中文占 2 列但只渲染 1.6 格，不会超出自己的格子。
    /// 拿它当画布宽度的**下限**：这样画布从第一帧起就是个常量，不会随内容忽宽忽窄。
    private static let paneColumns: CGFloat = 80

    /// 画布要多宽 = 最长那行**真实排出来**有多宽（不低于 80 列）。
    ///
    /// 别按「格子数」估：终端画面里全是框线字符（`─│╭`），它们是非 ASCII 但只占一格，
    /// 按两格估会把画布撑出一大片死黑——实测那帧估出 1111、实际只有 544，多出来的
    /// 567pt 比整块屏幕还宽，滑到最右边就是一片黑，看着像内容被一刀切了。
    ///
    /// 逐行全量实测又太贵（212 行 54ms，每 1.2 秒来一次就是新的卡顿），所以走上界剪枝：
    /// 先给每行算一个**宽松的**上界排序，量到「剩下的上界都超不过当前最宽」就停。
    /// 实测同一帧：结果和全量一字不差，只量 81 行、3.2ms。
    /// ⚠️ 上界必须宽松（这里非 ASCII 按 2.5 格）——上界估窄了会提前收工漏掉真正最宽的行，
    /// 那就又开始切字了。宽一点只是多量几行。
    private static func contentWidth(_ s: String) -> CGFloat {
        let cell = ("0" as NSString).size(withAttributes: [.font: font]).width
        let lines = s.split(separator: "\n", omittingEmptySubsequences: false)
        let upper = lines.map { line -> CGFloat in
            var units: CGFloat = 0
            for u in line.unicodeScalars { units += u.isASCII ? 1 : 2.5 }
            return units * cell
        }
        var order = Array(upper.indices)
        order.sort { upper[$0] > upper[$1] }
        var widest = paneColumns * cell
        for i in order {
            if upper[i] <= widest { break }
            widest = max(widest, (String(lines[i]) as NSString)
                .size(withAttributes: [.font: font]).width)
        }
        return ceil(widest)
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var onScrollingChanged: (Bool) -> Void = { _ in }
        var onTap: () -> Void = { }

        @objc func handleTap() { onTap() }

        func scrollViewWillBeginDragging(_ s: UIScrollView) { onScrollingChanged(true) }
        /// 手指/惯性驱动的滚动才记横向位置——换内容那种程序性的滚动不算。
        func scrollViewDidScroll(_ s: UIScrollView) {
            guard s.isDragging || s.isDecelerating || s.isTracking else { return }
            (s as? BottomPinnedTextView)?.rememberScrollX()
        }
        func scrollViewDidEndDragging(_ s: UIScrollView, willDecelerate decelerate: Bool) {
            if !decelerate { onScrollingChanged(false) }
        }
        func scrollViewDidEndDecelerating(_ s: UIScrollView) { onScrollingChanged(false) }
        func scrollViewDidEndScrollingAnimation(_ s: UIScrollView) { onScrollingChanged(false) }
    }
}

extension Color {
    static let terminalBG = Color(red: 0.05, green: 0.05, blue: 0.06)
    static let terminalBar = Color(red: 0.09, green: 0.09, blue: 0.11)
    static let terminalKey = Color(red: 0.15, green: 0.15, blue: 0.18)
    static let terminalText = Color(white: 0.85)
}
