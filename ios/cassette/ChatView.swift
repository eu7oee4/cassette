import SwiftUI

/// 聊天消息列表：可滚动，我方靠右、对方靠左，带头像和时间戳。
struct ChatView: View {
    let messages: [ChatMessage]
    var isWaiting: Bool = false   // 等待对方回复中：底部显示"正在输入"

    var onEdit: (ChatMessage) -> Void = { _ in }   // 时间戳旁小按钮：进编辑弹窗
    var onDelete: (ChatMessage) -> Void = { _ in } // 长按气泡：弹删除确认
    var onTapChatArea: () -> Void = { }            // 点聊天空白区
    var onTapAvatar: (MessageSender) -> Void = { _ in }   // 点头像：改昵称/换头像
    var editRefreshTick: Int = 0   // 用户亲手编辑/删除的信号：+1 → 手术式合并进冻结快照
    var scrollTarget: UUID? = nil                  // 外部跳转请求（聊天记录页点行）
    var onScrollTargetHandled: () -> Void = { }    // 跳转消费完通知外面清 nil

    var body: some View {
        GeometryReader { geo in
        ScrollViewReader { proxy in
        // 倒置滚动（聊天 app 主流做法）：整个滚动视图上下翻转，每条内容再各自翻回来。
        // "底部"=滚动偏移 0，键盘弹出/新消息进来都免费贴底跟随（这是翻转的核心价值，别拆）。
        // 它的已知病根——**滚动进行中**插入消息会污染 LazyVStack 的 contentSize
        // （症状：底部气泡露半只、怎么滑都弹回的假贴底卡死）——用下面的
        // onScrollPhaseChange 冻结快照修：手在滚动，布局就绝不动。
        ScrollView {
            LazyVStack(spacing: 14) {
                // 翻转后代码里越靠前 = 屏幕越靠底。
                // "正在输入"放最前，消息倒序。可见性走冻结快照（shownIsWaiting）——
                // 它在列表最底端，滚动/离底期间跟着 isWaiting 插进拔出＝滚动中动布局，
                // 正是 contentSize 污染的老病根（流式 text_break 会反复翻 isWaiting）。
                if shownIsWaiting {
                    TypingIndicatorRow()
                        .flippedUpsideDown()
                        .transition(.opacity)
                }
                ForEach(shownMessages.reversed()) { message in
                    Group {
                        if Self.isVisuallyEmpty(message) {
                            // 空内容行（如断流残留的空文字消息）整行不渲染——
                            // 不然会剩一条只有时间戳/纯空白的隐形行占位
                            EmptyView()
                        } else if message.isSystem || message.isMemoryNote {
                            SystemMessageRow(text: message.plainText)
                                .contentShape(Rectangle())
                                .onLongPressGesture { onDelete(message) }
                        } else {
                            MessageRow(message: message,
                                       contentWidth: geo.size.width - 24,
                                       onEdit: onEdit,
                                       onDelete: onDelete,
                                       onTapAvatar: onTapAvatar)
                        }
                    }
                    .background {
                        // 跳转落地闪高亮：白光、横向占满屏（负 padding 吃掉列表左右边距）
                        if flashRowId == message.id {
                            Color.white.opacity(0.85)
                                .padding(.horizontal, -12)
                        }
                    }
                    .flippedUpsideDown()
                    .id(message.id)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 12)
        }
        .flippedUpsideDown()
        // 贴底动作全走 ScrollPosition.scrollTo(edge:)：翻转布局"底"=滚动坐标顶边，
        // 滚到"边"不依赖 LazyVStack 行高估算——proxy.scrollTo(锚点) 跨长列表会
        // 滑到半路+末尾窜一下（估算高度途中修正），实机复现过。
        .scrollPosition($scrollPos)
        // 滚动时收键盘。⚠️ 必须 .immediately——.interactively 和翻转(scaleEffect -1)
        // 是坏组合：交互式收键盘的 inset 过渡会把滚动位置卡成假贴底，实机复现过。
        .scrollDismissesKeyboard(.immediately)
        // 核心修复：滚动进行中冻结消息快照（布局零变动），松手落定再一次性同步。
        .onScrollPhaseChange { _, newPhase in
            if newPhase == .idle {
                let fingerScrolled = frozenMessages != nil
                // 冻结期间内容变没变（和冻结基线比条数+末条长度）——没变就没有"要追的漂移"，
                // 用户停哪儿是哪儿（实锤 bug：停在离底一小截处松手被吸到底）。
                let baseline = awayFrozen ?? frozenMessages
                let changedWhileFrozen = baseline.map {
                    $0.count != messages.count
                        || ($0.last?.plainText.count ?? 0) != (messages.last?.plainText.count ?? 0)
                } ?? false
                frozenMessages = nil            // 手指落定 → 手势冻结解除
                frozenIsWaiting = nil
                if fingerScrolled { followBottom = atBottom }   // 手指落定的位置表达意图
                if nearBottom {
                    awayFrozen = nil            // 回到底部 → 离底冻结也解除，积压变化此刻进布局
                    awayIsWaiting = nil
                    if pendingOwnSnap || (changedWhileFrozen && followBottom) || unseenCount > 0 {
                        pendingOwnSnap = false
                        unseenCount = 0
                        followBottom = true
                        DispatchQueue.main.async { snapToBottom() }
                    }
                } else if pendingOwnSnap {
                    // 人在上面但自己发了消息：主动回底（解冻由回底后的 idle 再走一遍收尾）
                    awayFrozen = nil
                    awayIsWaiting = nil
                    pendingOwnSnap = false
                    unseenCount = 0
                    followBottom = true
                    DispatchQueue.main.async { snapToBottom() }
                }
            } else if newPhase != .animating && frozenMessages == nil {
                // 只有手指驱动的滚动（tracking/interacting/decelerating）才冻结快照。
                // .animating 是我们自己 snapToBottom 的贴底动画——它也冻的话，自己刚发的
                // 消息会在动画期间被计成"新消息"胶囊，动画里长出的内容也没人再贴底。
                // ⚠️ 冻结必须捕获**当前显示的**快照（awayFrozen 活动期间继承它，不抓 live
                // messages）——否则手指落下的瞬间显示内容从旧快照切成新的＝滚动中动布局，
                // 正是 contentSize 污染的老病根。
                frozenMessages = awayFrozen ?? messages
                frozenIsWaiting = awayIsWaiting ?? isWaiting
            }
        }
        // 用户亲手编辑（保存/改并重答）：立即合并进冻结快照上屏。
        // 冻结要挡的是"别人塞进来的变化"（新消息/流式增长），亲手编辑是明确的"我要它现在变"——
        // 不合并的话，离底冻结期间编辑旧消息（必然发生在离底，得滑上去才点得到）气泡纹丝不动，
        // 要滑回底部解冻才显示，看着像没存上。
        .onChange(of: editRefreshTick) { _, _ in
            mergeEditsIntoFrozen()
        }
        // 流式钉底：正文每长一截，只要没冻结且人在底部，就即时（无动画）钉回底边。
        // 病根：流式增长不改 messages.count，贴底动画进行中长出的内容会让偏移悄悄漂离底边，
        // 之后没有任何事件再触发贴底 → "差一截滑不到底"。逐 chunk 钉底把漂移当场归零；
        // 已经在底边时 scrollTo(edge:) 是无操作，不抖。
        .onChange(of: tailContentLength) { _, _ in
            // 只在跟随模式钉底：用户主动停在底上方一小截时，流式增长不吸（followBottom
            // 不受系统动画影响 → 贴底动画期间的漂移照样被追回）。
            if !isFrozen && followBottom {
                scrollPos.scrollTo(edge: .top)
            }
        }
        // 定稿钉底：done 时气泡从纯 Text 切回 Markdown 渲染，**行高会跳变**但内容长度
        // 基本不变——上面的长度钉底不触发，偏移差就永远留在那（"生成完了还差一截"实锤）。
        // 盯尾部流式条数的翻转，定稿瞬间补一次钉底。
        .onChange(of: tailStreamingCount) { _, _ in
            if !isFrozen && followBottom {
                scrollPos.scrollTo(edge: .top)
            }
        }
        // 在不在底部附近（翻转坐标：偏移 <100 = 在底）。回到底部即清新消息角标；
        // 一离开底部立刻立起离底冻结（此后内容变化全攒着，防屏外行高错位）。
        .onScrollGeometryChange(for: Bool.self) { g in
            g.contentOffset.y < 100
        } action: { _, isNear in
            nearBottom = isNear
            if isNear {
                if frozenMessages == nil { unseenCount = 0 }
            } else if awayFrozen == nil {
                // 铁律：冻结捕获的必须是「当前显示的」快照——手势冻结活动期间滑过离底阈值时
                // 抓 live 会让显示内容在解除手势冻结的瞬间旧换新（同 e7c16bb 修的镜像方向）。
                awayFrozen = frozenMessages ?? messages
                awayIsWaiting = frozenIsWaiting ?? isWaiting
            }
        }
        // 真·底部（<24）单独跟踪：手指落定时用它判定"想跟随还是想停留"。
        .onScrollGeometryChange(for: Bool.self) { g in
            g.contentOffset.y < 24
        } action: { _, isAt in
            atBottom = isAt
        }
        // 新消息上屏：自己刚发的 → 贴底且绝不变"新消息"胶囊；但**滚动进行中不能就地解冻**
        // （滚动中动布局正是 contentSize 污染的病根），记 pendingOwnSnap，落定解冻时补贴底。
        // 静止且在底部 → 跟随贴底；滚动中/翻旧消息 → 不动布局，角标计数。
        .onChange(of: messages.count) { oldCount, newCount in
            if messages.last?.sender == .me {
                if frozenMessages != nil {
                    pendingOwnSnap = true       // 手指还在滚：落定时再回底
                } else {
                    // 自己发消息 = 明确的"回到当下"：解冻（若有）+回底+恢复跟随
                    awayFrozen = nil
                    awayIsWaiting = nil
                    unseenCount = 0
                    followBottom = true
                    DispatchQueue.main.async { snapToBottom() }
                }
            } else if !isFrozen && nearBottom {
                // 新消息把停在底部附近(<100)的人带下去（微信惯例），顺带恢复跟随
                unseenCount = 0
                followBottom = true
                DispatchQueue.main.async { snapToBottom() }
            } else {
                unseenCount += max(0, newCount - oldCount)
            }
        }
        // 新消息角标：翻旧消息时来了新消息 → 底部浮一个胶囊，点了落底。
        .overlay(alignment: .bottom) {
            if unseenCount > 0 {
                Button {
                    unseenCount = 0
                    followBottom = true
                    snapToBottom()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.down")
                        Text("\(unseenCount) 条新消息")
                    }
                    .font(.footnote.bold())
                    .foregroundStyle(.white)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .background(Capsule().fill(Color.theme))
                    .shadow(color: .black.opacity(0.15), radius: 4, y: 1)
                }
                .padding(.bottom, 8)
                .transition(.opacity)
            }
        }
        // 键盘可见性：决定点击聊天区的行为（见下面 onTapGesture）。
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
            keyboardVisible = true
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in
            keyboardVisible = false
        }
        // 点击聊天区域：键盘开着 → 只收键盘（翻转布局在容器变高时自动贴底跟随，
        // 这时再叠一个 scrollTo 动画会和键盘动画打架、乱滑）；
        // 键盘没开 → 才是"点一下滑到底"。
        .contentShape(Rectangle())
        .onTapGesture {
            let wasKeyboardVisible = keyboardVisible
            dismissKeyboard()
            onTapChatArea()
            if !wasKeyboardVisible {
                snapToBottom()
            }
        }
        // 外部跳转请求（聊天记录页点行 / 以后引用灰条共用）：跳到那条气泡 + 闪高亮。
        .onChange(of: scrollTarget) { _, target in
            guard let target else { return }
            jump(to: target, proxy: proxy)
            onScrollTargetHandled()
        }
        }
        }
    }

    // 跳转落地后闪高亮的行 id（1.5s 自动熄）。
    @State private var flashRowId: UUID? = nil

    /// 跳到任意一条：长 Lazy 列表按锚点跳会因行高估算落偏（mianmian 实锤"滑到半路+
    /// 末尾窜一下"）→ 无动画连跳三拍让布局收敛落准；跳完退出跟随模式（人在读旧消息，
    /// 别被流式吸回底）。
    private func jump(to messageId: UUID, proxy: ScrollViewProxy) {
        guard messages.contains(where: { $0.id == messageId }) else { return }
        followBottom = false
        frozenMessages = nil
        frozenIsWaiting = nil
        proxy.scrollTo(messageId, anchor: .center)
        var attempts = 0
        Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { t in
            attempts += 1
            proxy.scrollTo(messageId, anchor: .center)
            if attempts >= 3 { t.invalidate() }
        }
        withAnimation(.easeIn(duration: 0.2)) { flashRowId = messageId }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            if flashRowId == messageId {
                withAnimation(.easeOut(duration: 0.5)) { flashRowId = nil }
            }
        }
    }

    /// 内容视觉为空的消息（正在流式的除外——它有呼吸点要显示）：整行跳过不渲染。
    private static func isVisuallyEmpty(_ m: ChatMessage) -> Bool {
        guard !m.isStreaming else { return false }
        return m.plainText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    // 新消息跟随/角标状态（见 onScrollGeometryChange / onChange(messages.count)）。
    @State private var nearBottom = true    // 视口是否贴在底部附近（翻转坐标 offset<100）
    @State private var atBottom = true      // 视口是否贴在真·底部（offset<24）——区分"停在底附近"和"在底"
    // 跟随模式：手指落定在真·底部=想跟随（流式钉底/解冻补贴底生效）；落定在上面=想停留（谁都别吸）。
    // 系统动画不改变它——贴底动画期间的偏移漂移因此能被钉底追回，而用户主动停留不会被误吸。
    @State private var followBottom = true
    @State private var unseenCount = 0      // 翻旧消息期间攒下的新消息条数（角标显示）
    @State private var pendingOwnSnap = false  // 滚动中自己发了消息：落定解冻时补贴底（滚动中不动布局）
    // 滚动进行中冻结的消息快照：手在滑，布局绝不动（防 LazyVStack contentSize 被滚动中
    // 插入污染成假贴底）。nil = 未冻结，直接渲染 messages。
    @State private var frozenMessages: [ChatMessage]? = nil
    // 离底冻结快照：人不在底部（哪怕静止在上面读）也冻结布局。病根实测：
    // 滚上去"停着读"时流式内容在屏幕外持续长高，LazyVStack 对屏外行用旧缓存高度，
    // contentSize 与真实内容错位 → 回来差一截且手动滑不到底（橡皮筋弹回），差值≈停留期间
    // 长出的内容量。不在底部就把一切内容变化攒着，回底落定时一次性解冻+贴底（此时流式行
    // 在屏内、测高精确）。
    @State private var awayFrozen: [ChatMessage]? = nil
    private var shownMessages: [ChatMessage] { frozenMessages ?? awayFrozen ?? messages }
    private var isFrozen: Bool { frozenMessages != nil || awayFrozen != nil }
    // "正在输入"行的可见性也要冻结（与消息快照同生命周期）：它不在 messages 里，
    // 不冻的话冻结期间照样插拔布局。
    @State private var frozenIsWaiting: Bool? = nil
    @State private var awayIsWaiting: Bool? = nil
    private var shownIsWaiting: Bool { frozenIsWaiting ?? awayIsWaiting ?? isWaiting }

    /// 尾部 8 条内容长度和：流式钉底的触发 key（拆成属性，onChange 里内联闭包会让类型检查超时）。
    private var tailContentLength: Int {
        var n = 0
        for m in messages.suffix(8) { n += m.plainText.count }
        return n
    }

    /// 尾部 8 条里正在流式的条数：定稿钉底的触发 key（isStreaming 翻 false＝markdown 重排行高跳变）。
    private var tailStreamingCount: Int {
        var n = 0
        for m in messages.suffix(8) where m.isStreaming { n += 1 }
        return n
    }
    // 键盘是否弹着（willShow/willHide 通知维护）：点击聊天区时用来分流行为。
    @State private var keyboardVisible = false
    // 贴底控制柄（iOS 17+）：scrollTo(edge:) 精确滚到坐标顶边=视觉底部。
    @State private var scrollPos = ScrollPosition()

    /// 贴底：翻转布局里视觉底部 = 滚动坐标 .top 边，按"边"滚，零估算误差。
    private func snapToBottom() {
        withAnimation(.easeOut(duration: 0.2)) { scrollPos.scrollTo(edge: .top) }
    }

    /// 手术式合并：把冻结快照里同 id 的行换成活数据、活数据里已没有的行撤下（长按删除也走
    /// 这条信号）；**快照绝不长出新行**——编辑/删除立即上屏，新消息照旧攒到解冻，
    /// 冻结对流式增长的防错位保护一点不动。
    /// （抽成方法不内联在 onChange 里：这个 body 的内联闭包有让 Swift 类型检查超时的前科。）
    private func mergeEditsIntoFrozen() {
        if let frozen = frozenMessages {
            frozenMessages = frozen.compactMap { m in messages.first(where: { $0.id == m.id }) }
        }
        if let away = awayFrozen {
            awayFrozen = away.compactMap { m in messages.first(where: { $0.id == m.id }) }
        }
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil
        )
    }
}

/// 流式气泡尾部的呼吸点：生成中一直脉动，气泡定稿（isStreaming=false）后随之消失。
struct StreamingPulseDot: View {
    @State private var on = false
    var body: some View {
        Circle()
            .fill(Color.secondary.opacity(0.55))
            .frame(width: 7, height: 7)
            .scaleEffect(on ? 1.0 : 0.55)
            .opacity(on ? 1.0 : 0.35)
            .animation(.easeInOut(duration: 0.55).repeatForever(autoreverses: true), value: on)
            .onAppear { on = true }
    }
}

/// "对方正在输入"指示：左侧对方头像 + 一个装着跳动圆点的气泡。
private struct TypingIndicatorRow: View {
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            AvatarView(sender: .other)
            TypingDots()
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .background(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(Color(.systemGray5))
                )
            Spacer(minLength: 40)
        }
    }
}

/// 三个轮流变亮的小圆点。
private struct TypingDots: View {
    @State private var phase = 0
    private let timer = Timer.publish(every: 0.3, on: .main, in: .common).autoconnect()

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(Color.secondary)
                    .frame(width: 7, height: 7)
                    .opacity(phase == i ? 1.0 : 0.3)
            }
        }
        .onReceive(timer) { _ in
            phase = (phase + 1) % 3
        }
    }
}

/// 系统提示：居中灰字，不带头像/气泡。统一样式，各种提示都用它。
private struct SystemMessageRow: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 2)
    }
}

/// 单行消息：根据发送方决定左右布局与头像位置。
private struct MessageRow: View {
    let message: ChatMessage
    var contentWidth: CGFloat = 320
    var onEdit: (ChatMessage) -> Void = { _ in }
    var onDelete: (ChatMessage) -> Void = { _ in }
    var onTapAvatar: (MessageSender) -> Void = { _ in }

    private var isMe: Bool { message.sender == .me }

    /// 文字消息的原文（非文字返回 nil）。
    private var rawText: String? {
        if case .text(let t) = message.kind { return t }
        return nil
    }

    /// 对白气泡的最大宽度。给个明确宽度，MarkdownUI 才会正常换行。
    private var bubbleMaxWidth: CGFloat { max(200, contentWidth * 0.72) }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if !isMe {
                Button { onTapAvatar(.other) } label: { AvatarView(sender: .other) }
                    .buttonStyle(.plain)
            }
            VStack(alignment: isMe ? .trailing : .leading, spacing: 5) {
                content
                    .contentShape(Rectangle())
                    .onLongPressGesture { onDelete(message) }   // 长按气泡本体删（拖动自动取消识别，不挡滚动）
                metaRow
            }
            .frame(maxWidth: .infinity, alignment: isMe ? .trailing : .leading)
            if isMe {
                Button { onTapAvatar(.me) } label: { AvatarView(sender: .me) }
                    .buttonStyle(.plain)
            }
        }
    }

    /// 消息内容：文字按段渲染（对白气泡 + 代码块卡片）；表情是自适应比例的图。
    @ViewBuilder private var content: some View {
        switch message.kind {
        case .text(let raw):
            if message.isStreaming {
                streamingBubble(raw)      // 流式增长中：纯 Text，避免每片重解析 markdown 卡顿
            } else {
                textBody(raw)
            }
        case .sticker(let url, _):
            // 表情保持自适应比例（透明底/长条表情裁成方形会毁）
            StickerImage(url: url)
                .frame(maxWidth: 160, maxHeight: 160)
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        case .system, .memoryNote:
            EmptyView()
        }
    }

    /// 文字消息：按段渲染（对白进气泡，代码块脱出成深色卡片）。
    @ViewBuilder private func textBody(_ raw: String) -> some View {
        VStack(alignment: isMe ? .trailing : .leading, spacing: 5) {
            ForEach(Array(messageSegments(raw).enumerated()), id: \.offset) { _, seg in
                segmentView(seg)
            }
        }
    }

    /// 流式增长中的气泡：纯 Text（不解析 markdown/不上代码高亮），逐片更新才丝滑。
    /// 定稿（isStreaming=false）后由 textBody 走完整 markdown 渲染。
    /// 尾部呼吸点：生成中常亮，定稿即消——不然正文一开始冒（"正在输入"收起后）就没任何"还在写"的指示。
    private func streamingBubble(_ raw: String) -> some View {
        HStack(alignment: .bottom, spacing: 6) {
            Text(raw)
            StreamingPulseDot()
                .padding(.bottom, 5)
        }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(isMe ? Color.bubbleMe : Color(.systemGray5))
            )
            .frame(maxWidth: bubbleMaxWidth, alignment: isMe ? .trailing : .leading)
    }

    @ViewBuilder private func segmentView(_ seg: MessageSegment) -> some View {
        switch seg {
        case .bubble(let md):
            MarkdownMessageView(text: md)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(isMe ? Color.bubbleMe : Color(.systemGray5))
                )
                .frame(maxWidth: bubbleMaxWidth, alignment: isMe ? .trailing : .leading)
        case .card(let md):
            // 只有代码块进这里：深色块自带背景，宽度取气泡列宽、靠发送者那一侧对齐。
            MarkdownMessageView(text: md)
                .frame(maxWidth: bubbleMaxWidth, alignment: isMe ? .trailing : .leading)
        }
    }

    /// 时间戳 + 小按钮（我方按钮在时间戳内侧，对方在外侧）。
    private var metaRow: some View {
        HStack(spacing: 8) {
            if isMe {
                actionButtons
                timestampText
            } else {
                timestampText
                actionButtons
            }
        }
        .padding(.horizontal, 4)
    }

    private var timestampText: some View {
        Text(Self.timeFormatter.string(from: message.timestamp))
            .font(.caption2)
            .foregroundStyle(.secondary)
    }

    @ViewBuilder
    private var actionButtons: some View {
        HStack(spacing: 6) {
            // 双方的文字消息：gobackward 进编辑弹窗（弹窗里按钮按发送者不同）+ 复制。
            if rawText != nil {
                iconButton("gobackward") { onEdit(message) }
                iconButton("square.on.square") { UIPasteboard.general.string = message.plainText }
            }
        }
    }

    private func iconButton(_ systemName: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 13))
                .foregroundStyle(.secondary)
                .frame(width: 26, height: 22)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm"   // 带年月日（跨年/翻旧记录要认得出）
        return f
    }()
}

/// 从本地文件 URL 加载表情图；加载不出来时显示占位。
/// 用 AppFiles.loadImage 重新锚定当前容器，避免重装后旧路径失效。
struct StickerImage: View {
    let url: URL

    var body: some View {
        if let image = AppFiles.loadImage(url) {
            Image(uiImage: image)
                .resizable()
                .scaledToFit()
        } else {
            RoundedRectangle(cornerRadius: 16)
                .fill(Color(.systemGray5))
                .overlay(Image(systemName: "photo").foregroundStyle(.secondary))
        }
    }
}

/// 圆形头像。有真实头像就显示图片，否则显示占位。大小可调。
struct AvatarView: View {
    @EnvironmentObject private var profile: ProfileStore
    let sender: MessageSender
    var size: CGFloat = 34

    var body: some View {
        Group {
            if let img = profile.avatar(for: sender) {
                Image(uiImage: img)
                    .resizable()
                    .scaledToFill()
            } else {
                // 我方默认头像用主题深色（别用 accentColor——它在自定义视图里会回落成系统蓝）
                (sender == .me ? Color.theme : Color(.systemGray3))
                    .overlay(
                        Image(systemName: sender == .me ? "person.fill" : "person")
                            .font(.system(size: size * 0.47))
                            .foregroundStyle(.white)
                    )
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
    }
}

/// 上下翻转（用于倒置滚动）：旋转 180° 再水平镜像回来 = 只做垂直翻转、不镜像、不动滚动条位置。
private extension View {
    func flippedUpsideDown() -> some View {
        self.rotationEffect(.radians(.pi)).scaleEffect(x: -1, y: 1, anchor: .center)
    }
}
