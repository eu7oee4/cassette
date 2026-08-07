import SwiftUI

/// Code 模式的内联终端：贴在输入框上方，不用切页面就能看他在干什么、按掉权限弹窗。
///
/// 交互（结构从上到下：黑条 → 画面 → 按钮行 → 输入框）：
/// - 黑条单击开满屏、再单击收回；右侧那个「⅔」按钮切中间那一档。
/// - **拖拽调档暂时没有**：拖的过程中会抖、停在中间有时也会自己跳。根因不在这个文件里
///   ——面板长高会压缩 ChatView，而那边是倒置滚动 + 冻结快照那一套，布局被牵动后又反过来
///   影响这边。要做的话得把面板改成 overlay（盖住气泡区而不是压缩它），让 ChatView 的
///   布局全程不动，那是另一件事。**别只是把 DragGesture 加回来**。
/// - 档位也别改回双击：双击手势一在，单击就得等判定窗口过期才响应，肉眼可见地迟钝。
///
/// 画面的**显示范围和内容滚动位置是两回事**——画面永远按可用高度布局、内容底部对齐，
/// 外面那层 frame 只是个裁剪窗口。所以不管拖到哪一档，最新输出始终贴在窗口底边，
/// 按钮行也一直在底下（它在裁剪窗口之外）。要往回翻旧内容，在画面里上滑。
struct CodeTerminalPanel: View {
    let service: ChatService
    @Binding var expanded: Bool
    /// 黑条以下那些东西（待发照片条/文件条/表情面板 + 输入栏）此刻实际有多高，由外面实测传进来。
    /// 不实测就会踩到：贴了张照片再贴个文件，输入框直接被顶出屏幕。
    var bottomBars: CGFloat = 56

    @State private var content = ""
    @State private var dialog: [ChatService.CodeDialogOption] = []
    @State private var alive = true
    @State private var lastOk = Date()
    @State private var now = Date()             // 驱动"多久没刷新"的显示
    /// 档位存**比例**不存绝对高度（0=收起 / 0.62=2/3 屏 / 1=占满）：键盘弹出、贴了照片、
    /// 弹窗按钮变多——可用空间一变，比例不动、高度自己缩放，不用记原来是哪一档。
    @State private var stopRatio: CGFloat = 0
    @State private var keyboard: CGFloat = 0     // 键盘占了多高
    @State private var scrolling = false         // 手指正在画面里滑
    @State private var heldContent: String? = nil // 滑动期间攒下的新画面，落定再上
    // 屏幕尺寸和安全区只在出现时读一次。**绝不能在 body 里实时读 UIKit 的这些值**：
    // 它们在布局过程中会变，读到新值 → 高度变 → 又触发布局 → 再读到新值，来回震荡，
    // 拖拽时看着就是疯狂频闪（实机踩到过）。
    @State private var screenH: CGFloat = 800
    @State private var safeTop: CGFloat = 47
    @State private var safeBottom: CGFloat = 34

    /// 画面刷新间隔：展开时跟得紧；收起时只为了知道"有没有弹窗在等"，慢慢来就行。
    private let fastPoll: Duration = .milliseconds(1200)
    private let slowPoll: Duration = .seconds(6)
    private let barHeight: CGFloat = 34
    private let staleAfter: TimeInterval = 5    // 超过这么久没刷出来就明说（别无声冻住）
    // 顶栏（猫爪/标题/开关那条）+ 它底下那道分隔线。宁可估大一两点留条看不见的缝，
    // 也别估小——估小了就是输入框被顶掉一条边。
    private let headerHeight: CGFloat = 50
    private let halfRatio: CGFloat = 0.62       // 中间那一档
    /// 布局上给输入框留的生长余地（它 1~5 行自动长高，每行约 20pt）。
    /// 不留的话面板把空间占死，输入框换行了也长不起来。这块留白**不会露出来**——
    /// 底下的负 padding 让面板向上盖住它。
    private let growRoom: CGFloat = 80

    /// 按钮行占多高（弹窗选项是竖排的，五个选项就是一大条，底下还有一排方向键）。
    private var keyBarHeight: CGFloat {
        dialog.isEmpty ? 50 : CGFloat(dialog.count) * 46 + 55
    }

    /// 画面此刻最多能占多高 = 屏幕减掉所有别人要用的。键盘顶掉底部安全区（两者取大，
    /// 别重复扣）。
    private var maxHeight: CGFloat {
        let taken = safeTop + headerHeight + barHeight + keyBarHeight
                    + bottomBars + max(keyboard, safeBottom) + growRoom
        return max(100, screenH - taken)
    }
    private var height: CGFloat { stopRatio * maxHeight }

    private var isStale: Bool { now.timeIntervalSince(lastOk) > staleAfter }

    var body: some View {
        VStack(spacing: 0) {
            grabBar
            if height > 0 {
                screen
                keyBar
            }
        }
        .background(Color.terminalBG)
        // 展开时向上盖住那块 growRoom 留白（外加几点估算误差）：布局上它是给输入框长高
        // 留的余地、气泡区那一条就剩这么高，视觉上被面板自己的背景盖掉，所以顶上不会
        // 漏出半条气泡。负 padding 只减自己的占位、内容照样画出去。
        // 收起时必须归零，不然黑条会往上跑去盖住气泡。
        .padding(.top, height > 0 ? -(growRoom + 6) : 0)
        .animation(.easeOut(duration: 0.22), value: height)
        // 外部改了展开状态（点黑条 / 退出 Code 模式）→ 档位跟上。
        // ⚠️ 收起时才无条件归零；打开时**只在原本收着的情况下**给满档——否则从收起状态点
        // 「⅔」时，setRatio 先设好 0.62、再把 expanded 翻成 true，这里会顺手把它重置成
        // 全屏，人就永远停不到 2/3。
        .onChange(of: expanded) { _, on in
            if !on { stopRatio = 0 } else if stopRatio == 0 { stopRatio = 1 }
        }
        .onAppear {
            let scene = UIApplication.shared.connectedScenes.first as? UIWindowScene
            screenH = scene?.screen.bounds.height ?? 800
            safeTop = scene?.windows.first?.safeAreaInsets.top ?? 47
            safeBottom = scene?.windows.first?.safeAreaInsets.bottom ?? 34
            stopRatio = expanded ? 1 : 0
        }
        .onReceive(NotificationCenter.default.publisher(
            for: UIResponder.keyboardWillShowNotification)) { note in
            let frame = note.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect
            keyboard = frame?.height ?? 0
        }
        .onReceive(NotificationCenter.default.publisher(
            for: UIResponder.keyboardWillHideNotification)) { _ in
            keyboard = 0
        }
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
                .rotationEffect(.degrees(height > 0 ? 180 : 0))
                .foregroundStyle(.white.opacity(0.75))
            Text(height > 0 ? "点击此处收回终端窗口" : "点击此处展开终端窗口")
                .font(.system(size: 12))
                .foregroundStyle(.white.opacity(0.9))
            // 收起时也要知道他卡在弹窗上了——不然人在聊天里干等，那边等着按键。
            if height == 0 && !dialog.isEmpty {
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

    private func setRatio(_ r: CGFloat) {
        stopRatio = r
        expanded = r > 0
    }

    // MARK: - 画面

    private var screen: some View {
        ScrollView(.vertical) {
            // 横向不换行、可横滑：终端画面按 80 列排的，硬折行会把对齐全毁掉
            ScrollView(.horizontal, showsIndicators: false) {
                // 不开 textSelection：两百多行等宽文本每 1.2 秒重画一次，选择支持要给
                // 每个字建可命中的区域，生成时画面一直在变，这一项就是卡顿的大头。
                Text(content.isEmpty ? (alive ? "连接中…" : "") : content)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Color.terminalText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
            }
        }
        // 滚动**不**收键盘：常有的用法是翻到前面照着上面的内容打字，一滚就收键盘反而碍事。
        // 要收键盘点一下画面（下面那个 tap）。
        .scrollDismissesKeyboard(.never)
        // 手指在滑就别换内容：轮询 1.2 秒一次，正好在滑到一半时重建两百多行文本，
        // 滚动会被打断一下（"滑一次、中间顿一下、再接着滑完"）。攒着，落定再上。
        .onScrollPhaseChange { _, phase in
            scrolling = phase != .idle
            if !scrolling, let held = heldContent {
                content = held
                heldContent = nil
            }
        }
        // 内容永远按全屏高度布局、底部对齐 → 最新输出贴着窗口底边
        .defaultScrollAnchor(.bottom)
        .frame(height: maxHeight, alignment: .bottom)
        // 外面这层才是"看得见多少"的裁剪窗口，拖拽只动它
        .frame(height: height, alignment: .bottom)
        .clipped()
        // ⚠️ 少了这行整个面板会像卡死一样（实机踩到）：**clipped() 只裁绘制、不裁命中测试**，
        // 被裁掉的那部分画面（按 maxHeight 布局、向上溢出）照样吃触摸——它盖在黑条和气泡区
        // 上面，于是黑条点不动也拖不动，在气泡区滑动滑的还是终端内容。
        .contentShape(Rectangle())
        // 点画面收键盘（打字时想看终端，点一下就行，不用先收键盘再点）
        .simultaneousGesture(TapGesture().onEnded { dismissKeyboard() })
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

    private var keyBar: some View {
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
        .background(Color.terminalBar)
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

extension Color {
    static let terminalBG = Color(red: 0.05, green: 0.05, blue: 0.06)
    static let terminalBar = Color(red: 0.09, green: 0.09, blue: 0.11)
    static let terminalKey = Color(red: 0.15, green: 0.15, blue: 0.18)
    static let terminalText = Color(white: 0.85)
}
