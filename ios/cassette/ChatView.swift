import SwiftUI

/// 聊天消息列表：可滚动，我方靠右、对方靠左，带头像和时间戳。
struct ChatView: View {
    let messages: [ChatMessage]
    /// 这些气泡属于**哪个角色**的会话。头像靠它取（ProfileStore 里没有「当前是谁」，
    /// 见那边的注释）——身份跟着数据一起传下来，不去问一个可变的全局。
    let charID: String
    var isWaiting: Bool = false   // 等待对方回复中：底部显示三个点的输入气泡
    var interjectWaiting: Set<UUID> = []   // 插话已送进在飞的那轮、等他看到的用户气泡（小字「已送达，等他看到」）

    var onEdit: (ChatMessage) -> Void = { _ in }   // 时间戳旁小按钮：进编辑弹窗
    var onDelete: (ChatMessage) -> Void = { _ in } // 长按气泡：弹删除确认
    var onTapChatArea: () -> Void = { }            // 点聊天空白区
    var onTapAvatar: (MessageSender) -> Void = { _ in }   // 点头像：改昵称/换头像
    var onTapImage: (URL) -> Void = { _ in }       // 点图片气泡：全屏查看
    var onTapImageStack: ([URL], Int) -> Void = { _, _ in }   // 点堆叠卡：从当前图起翻页
    var onTapFile: (URL) -> Void = { _ in }        // 点文件卡片：QuickLook 预览
    var onTapWebpage: (String, String) -> Void = { _, _ in }  // 点网页卡片：打开网页
    var onDeleteStack: ([ChatMessage]) -> Void = { _ in }     // 长按堆叠卡：删整组
    var onTapPR: (Int) -> Void = { _ in }          // 点 PR 系统行：跳 PR 详情（PLAN_prs §5）
    var editRefreshTick: Int = 0   // 用户亲手编辑/删除的信号：+1 → 手术式合并进冻结快照
    var backToNowTick: Int = 0     // 「编辑并重新回复」的信号：+1 → 解冻回底，等着看新答案
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
            // 行间距不走 spacing，由每行自己的 meta.gap 决定（同一人连发 3、换人/隔时间 14）。
            LazyVStack(spacing: 0) {
                // 翻转后代码里越靠前 = 屏幕越靠底。
                // "正在输入"放最前，消息倒序。可见性走冻结快照（shownIsWaiting）——
                // 它在列表最底端，滚动/离底期间跟着 isWaiting 插进拔出＝滚动中动布局，
                // 正是 contentSize 污染的老病根（流式 text_break 会反复翻 isWaiting）。
                if shownIsWaiting {
                    // 接着 TA 上一个气泡就按连发排（紧间距、不挂头像），否则按换人排
                    let cont = typingContinuesRun
                    TypingBubbleRow(charID: charID, showAvatar: !cont)
                        .padding(.top, cont ? 3 : 14)
                        .flippedUpsideDown()
                        .transition(.opacity)
                }
                ForEach(displayEntries.reversed()) { entry in
                    let row = entry.row
                    VStack(spacing: 0) {
                        // 隔 5 分钟以上才出一条居中时间（iMessage 式），不再每条挂时间戳
                        if let t = entry.meta.timeLabel {
                            TimeSeparatorRow(text: t)
                                .padding(.bottom, 10)
                        }
                        Group {
                            switch row {
                            case .single(let message):
                                if Self.isVisuallyEmpty(message) {
                                    // 空内容行（如断流残留的空文字消息）整行不渲染——
                                    // 不然会剩一条只有时间戳/纯空白的隐形行占位
                                    EmptyView()
                                } else if case .prNote(let prID, let text) = message.kind {
                                    PRNoteRow(text: text) { onTapPR(prID) }
                                        .onLongPressGesture { onDelete(message) }
                                } else if message.isSystem {
                                    SystemMessageRow(text: message.plainText)
                                        .contentShape(Rectangle())
                                        .onLongPressGesture { onDelete(message) }
                                } else if message.isMemoryNote {
                                    // 小字提醒和系统消息分成两档（§3.4/§3.9）：小字是某个
                                    // 角色做的事（靠左带 logo），系统消息是 app 自己说的话（居中）。
                                    NoteRow(text: message.plainText,
                                            senderID: message.senderID ?? charID)
                                        .contentShape(Rectangle())
                                        .onLongPressGesture { onDelete(message) }
                                } else if case .browseNote(let urls) = message.kind {
                                    BrowseNoteRow(urls: urls)
                                        .onLongPressGesture { onDelete(message) }
                                } else {
                                    MessageRow(message: message,
                                               charID: charID,
                                               showAvatar: entry.meta.showAvatar,
                                               contentWidth: geo.size.width - 24,
                                               waitingNote: interjectWaiting.contains(message.id),
                                               menuTarget: activeMenu?.ref,
                                               onMenu: { activeMenu = $0 },
                                               onSelectText: { selectText = SelectTextItem(text: $0) },
                                               onEdit: onEdit,
                                               onDelete: onDelete,
                                               onTapAvatar: onTapAvatar,
                                               onTapImage: onTapImage,
                                               onTapFile: onTapFile,
                                               onTapWebpage: onTapWebpage)
                                }
                            case .imageStack(let group):
                                ImageStackRow(messages: group,
                                              charID: charID,
                                              showAvatar: entry.meta.showAvatar,
                                              onTapStack: onTapImageStack,
                                              onTapAvatar: { onTapAvatar(group[0].sender) })
                                    .contentShape(Rectangle())
                                    .onLongPressGesture { onDeleteStack(group) }
                            }
                        }
                    }
                    .background {
                        // 跳转落地闪高亮：白光、横向占满屏（负 padding 吃掉列表左右边距）
                        if flashRowId == row.id {
                            Color.white.opacity(0.85)
                                .padding(.horizontal, -12)
                        }
                    }
                    .padding(.top, entry.meta.gap)
                    .flippedUpsideDown()
                    .id(row.id)
                }
            }
            .padding(.horizontal, 12)
            // ⚠️ 翻转布局：**layout 的 top 就是视觉的底**（往底部加内边距要加 .top）。
            .padding(.top, 12)
            .padding(.bottom, 12)
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
            if newPhase != .idle { activeMenu = nil }   // 一滚就收长按菜单（位置是激活时量的）
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
        // 「编辑并重新回复」＝明确的"回到当下、等着看新答案"：解冻 + 回底 + 恢复跟随。
        // 以前这件事是白蹭的——truncateAfter 让 count 变小，撞进下面"自己发的消息"那条
        // 分支顺带回了底。删除语义修好之后（下面的 guard）那条路没了，得有个显式信号。
        .onChange(of: backToNowTick) { _, _ in
            if frozenMessages != nil {
                pendingOwnSnap = true       // 手指还在滚：落定时再回底
            } else {
                awayFrozen = nil
                awayIsWaiting = nil
                unseenCount = 0
                followBottom = true
                DispatchQueue.main.async { snapToBottom() }
            }
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
            // 删除也让 count 变（变小），会掉进下面任一条贴底分支 → 删完一条就被甩到底部。
            // 删除的语义是「就地撤行、保持位置」，那条路由 editRefreshTick 负责（手术式
            // 合并进冻结快照），这里只做状态收尾、绝不滚动。
            guard newCount > oldCount else {
                unseenCount = min(unseenCount, newCount)   // 删掉的那条不该还算在未读里
                return
            }
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
                    .foregroundStyle(Color.onChatInk)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .background(Capsule().fill(Color.chatInk))
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
            // 长按菜单开着：这一下只收菜单，别的什么都不做
            if activeMenu != nil {
                activeMenu = nil
                return
            }
            let wasKeyboardVisible = keyboardVisible
            dismissKeyboard()
            onTapChatArea()
            if !wasKeyboardVisible {
                snapToBottom()
            }
        }
        // 长按菜单浮层：画在列表外面（不翻转、不受 LazyVStack 层级影响），按气泡屏幕位置摆
        .overlay(alignment: .topLeading) {
            if let menu = activeMenu {
                BubbleMenuOverlay(menu: menu, viewport: geo.frame(in: .global)) {
                    activeMenu = nil
                }
                .transition(.opacity.combined(with: .scale(scale: 0.94)))
            }
        }
        .animation(.easeOut(duration: 0.15), value: activeMenu?.ref)
        // 长按菜单「选择」：挂在列表外层弹（行在 LazyVStack 里随时可能被回收）
        .sheet(item: $selectText) { item in
            SelectableTextSheet(text: item.text)
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

    // 长按菜单挂在哪个气泡上（全列表同时只有一个）；「选择」弹页要的字。
    @State private var activeMenu: ActiveBubbleMenu? = nil
    @State private var selectText: SelectTextItem? = nil

    // 跳转落地后闪高亮的行 id（1.5s 自动熄）。
    @State private var flashRowId: UUID? = nil

    /// 跳到任意一条：长 Lazy 列表按锚点跳会因行高估算落偏（mianmian 实锤"滑到半路+
    /// 末尾窜一下"）→ 无动画连跳三拍让布局收敛落准；跳完退出跟随模式（人在读旧消息，
    /// 别被流式吸回底）。
    private func jump(to messageId: UUID, proxy: ScrollViewProxy) {
        followBottom = false
        frozenMessages = nil
        frozenIsWaiting = nil
        guard let rid = rowId(for: messageId) else { return }   // 目标可能收在堆叠卡里
        proxy.scrollTo(rid, anchor: .center)
        var attempts = 0
        Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { t in
            attempts += 1
            proxy.scrollTo(rid, anchor: .center)
            if attempts >= 3 { t.invalidate() }
        }
        withAnimation(.easeIn(duration: 0.2)) { flashRowId = rid }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            if flashRowId == rid {
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

    /// 展示行：连续 ≥2 张同发送者的图片消息收成一张堆叠照片卡，其余原样单行。
    private enum ChatRowItem: Identifiable {
        case single(ChatMessage)
        case imageStack([ChatMessage])
        var id: UUID {
            switch self {
            case .single(let m):      return m.id
            case .imageStack(let ms): return ms[0].id   // 组 id=首图 id，追加进组不换行身份
            }
        }
    }

    private var displayRows: [ChatRowItem] {
        var rows: [ChatRowItem] = []
        var run: [ChatMessage] = []
        func flushRun() {
            if run.count >= 2 { rows.append(.imageStack(run)) }
            else { run.forEach { rows.append(.single($0)) } }
            run = []
        }
        for m in shownMessages {
            if case .image = m.kind {
                if let last = run.last, last.sender != m.sender { flushRun() }
                run.append(m)
            } else {
                flushRun()
                rows.append(.single(m))
            }
        }
        flushRun()
        return rows
    }

    /// 行的排版信息：头顶要不要出时间、和上一行隔多远、左侧头像露不露。
    private struct RowMeta {
        var timeLabel: String? = nil
        var gap: CGFloat = 0
        var showAvatar = true
    }

    private struct DisplayEntry: Identifiable {
        let row: ChatRowItem
        let meta: RowMeta
        var id: UUID { row.id }
    }

    /// 展示行 + 排版信息一次算完（别在 ForEach 里逐行回头查——长记录会变 O(n²)）。
    /// 规则：距上一行 >5 分钟出居中时间；同一发送者连发（中间没夹小字/系统行/时间）
    /// 贴紧 3pt，否则 14pt；TA 的头像只挂在连发的第一条上。
    /// 输入气泡算不算 TA 上一串的延续：最后一行是 TA 的气泡、且离现在不到 5 分钟
    /// （超了真回复会带居中时间 + 头像，输入气泡提前排成一样，冒字时不跳）。
    private var typingContinuesRun: Bool {
        guard let last = displayRows.last else { return false }
        switch last {
        case .single(let m):
            return m.sender == .other && !Self.isVisuallyEmpty(m)
                && !(m.isSystem || m.isMemoryNote || m.isBrowseNote)
                && Date().timeIntervalSince(m.timestamp) <= 300
        case .imageStack(let ms):
            return ms[0].sender == .other
                && Date().timeIntervalSince(ms[ms.count - 1].timestamp) <= 300
        }
    }

    private var displayEntries: [DisplayEntry] {
        var out: [DisplayEntry] = []
        var prevTime: Date? = nil
        var prevBubbleSender: MessageSender? = nil   // 上一行是气泡类才有值
        for row in displayRows {
            let sender: MessageSender
            let first: Date, last: Date
            let isBubble: Bool
            switch row {
            case .single(let m):
                if Self.isVisuallyEmpty(m) {
                    out.append(DisplayEntry(row: row, meta: RowMeta(showAvatar: false)))
                    continue
                }
                sender = m.sender
                first = m.timestamp; last = m.timestamp
                isBubble = !(m.isSystem || m.isMemoryNote || m.isBrowseNote)
            case .imageStack(let ms):
                sender = ms[0].sender
                first = ms[0].timestamp; last = ms[ms.count - 1].timestamp
                isBubble = true
            }
            var meta = RowMeta()
            if prevTime == nil || first.timeIntervalSince(prevTime!) > 300 {
                meta.timeLabel = Self.separatorLabel(first)
            }
            let continued = meta.timeLabel == nil && isBubble && prevBubbleSender == sender
            meta.gap = out.isEmpty ? 0 : (continued ? 3 : 14)
            meta.showAvatar = !continued
            out.append(DisplayEntry(row: row, meta: meta))
            prevTime = last
            prevBubbleSender = isBubble ? sender : nil
        }
        return out
    }

    /// 居中时间的写法：今天 `14:32`、昨天 `昨天 14:32`、一周内 `星期三 14:32`、
    /// 今年 `10月8日 14:32`、更早带年份。
    private static func separatorLabel(_ d: Date) -> String {
        let cal = Calendar.current
        let now = Date()
        let hm = hmFormatter.string(from: d)
        if cal.isDateInToday(d) { return hm }
        if cal.isDateInYesterday(d) { return "昨天 \(hm)" }
        if let days = cal.dateComponents([.day], from: cal.startOfDay(for: d),
                                         to: cal.startOfDay(for: now)).day,
           days >= 0, days < 7 {
            return "\(weekdayFormatter.string(from: d)) \(hm)"
        }
        if cal.component(.year, from: d) == cal.component(.year, from: now) {
            return monthDayFormatter.string(from: d)
        }
        return fullFormatter.string(from: d)
    }

    private static func formatter(_ f: String) -> DateFormatter {
        let df = DateFormatter()
        df.locale = Locale(identifier: "zh_CN")
        df.dateFormat = f
        return df
    }
    private static let hmFormatter = formatter("HH:mm")
    private static let weekdayFormatter = formatter("EEEE")
    private static let monthDayFormatter = formatter("M月d日 HH:mm")
    private static let fullFormatter = formatter("yyyy年M月d日 HH:mm")

    /// 消息 id → 展示行 id（目标可能被收进堆叠卡，行 id=组首图 id）。跳转用。
    private func rowId(for messageId: UUID) -> UUID? {
        for row in displayRows {
            switch row {
            case .single(let m):
                if m.id == messageId { return row.id }
            case .imageStack(let ms):
                if ms.contains(where: { $0.id == messageId }) { return row.id }
            }
        }
        return nil
    }
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

/// 角色 logo（§2）：小字提醒的前缀符号，跟着小字的灰走（所以不能用彩色 emoji）。
/// 小卡=爪印（SF Symbol 单色模板），Cassius=✦（单色文本字符）；
/// 机主的符号待定（§8）→ 空，整个前缀不占位。头像 ≠ logo，别混。
private struct CharLogo: View {
    let senderID: String
    var body: some View {
        let p = IdentityColor.palette(for: senderID)
        if let symbol = p.logoSymbol {
            Image(systemName: symbol).font(.system(size: 10))
        } else if !p.logo.isEmpty {
            Text(p.logo).font(.caption)
        }
    }
}

/// 小字提醒（§3.4）：`{logo} {内容}`，固定灰、靠左，直接坐在背景上——
/// 跟居中的系统消息分开：小字是某个角色做的事，系统消息是 app 自己说的话。
private struct NoteRow: View {
    let text: String
    let senderID: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 5) {
            CharLogo(senderID: senderID)
            Text(text)
        }
        .font(.caption)
        .foregroundStyle(Color.noteGray)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.leading, 4)
        .padding(.vertical, 2)
    }
}

/// 输入中气泡（10-12）：TA 下一个气泡的位置先放一个灰气泡，三个点依次起伏——
/// 不念名字、不挂 logo（顶栏已经写着是谁）。同色同圆角，冒字时原地换成真气泡。
private struct TypingBubbleRow: View {
    @Environment(\.colorScheme) private var scheme
    let charID: String
    let showAvatar: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if showAvatar {
                AvatarView(sender: .other, charID: charID)
            } else {
                Color.clear.frame(width: 34, height: 1)
            }
            TypingDots()
                .padding(.horizontal, 14)
                .frame(height: 38)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(ChatPalette.current(scheme).bubbleAI))
            Spacer(minLength: 0)
        }
    }
}

/// 三个点一波一波往上走：每个点错开 0.2s，相位驱动大小和深浅。
private struct TypingDots: View {
    var body: some View {
        TimelineView(.animation) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            HStack(spacing: 5) {
                ForEach(0..<3, id: \.self) { i in
                    // 1.2s 一个周期；每个点只在自己那一段亮起，其余时间歇着
                    let phase = (t / 1.2 - Double(i) * 0.18).truncatingRemainder(dividingBy: 1)
                    let k = phase < 0.5 ? sin(phase * 2 * .pi) : 0
                    Circle()
                        .fill(Color.secondary)
                        .frame(width: 7, height: 7)
                        .opacity(0.35 + 0.5 * max(0, k))
                        .offset(y: -2.5 * max(0, k))
                }
            }
        }
    }
}

/// 居中时间（每隔 >5 分钟一条）：比系统消息再轻一档。
private struct TimeSeparatorRow: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .foregroundStyle(Color.noteGray)
            .frame(maxWidth: .infinity, alignment: .center)
    }
}

/// 系统提示（§3.9）：居中灰字，无 logo、无头像、无气泡——
/// app 说的话，不属于任何角色（跟靠左带 logo 的小字提醒 NoteRow 的分界就在这）。
private struct SystemMessageRow: View {
    let text: String
    var body: some View {
        Text(text)
            .multilineTextAlignment(.center)
            .font(.caption)
            .foregroundStyle(Color.noteGray)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 2)
    }
}

/// PR 系统行（PLAN_prs §5）：跟系统消息同一档（居中灰字，app 自己说的话），多一个 › 表示能点。
private struct PRNoteRow: View {
    let text: String
    let onTap: () -> Void
    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 3) {
                Text("📝 \(text)")
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
            }
            .font(.caption)
            .foregroundStyle(Color.noteGray)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// 浏览灰字：收起=「浏览了 N 个网页 ›」，点击展开网址列表（再点收起），网址点开进 Safari。
/// 展开状态是行内瞬态（@State）：滚远被 LazyVStack 回收就回到收起——这是提示不是页面，够用。
/// 长按删除挂在收起头上（外层给的手势）；网址行自己是按钮，不参与长按。
private struct BrowseNoteRow: View {
    let urls: [String]
    @State private var expanded = false
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(spacing: 4) {
            HStack(spacing: 3) {
                Text("浏览了 \(urls.count) 个网页")
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                    .rotationEffect(.degrees(expanded ? 90 : 0))
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .contentShape(Rectangle())
            .onTapGesture {
                withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
            }
            if expanded {
                VStack(spacing: 3) {
                    ForEach(Array(urls.enumerated()), id: \.offset) { _, u in
                        Button {
                            if let url = URL(string: u) { openURL(url) }
                        } label: {
                            Text(u)
                                .font(.caption2)
                                .foregroundStyle(Color.chatInk.opacity(0.7))
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 24)
            }
        }
        .frame(maxWidth: .infinity, alignment: .center)
        .padding(.vertical, 2)
    }
}

/// 单行消息：TA 靠左（连发只在第一条挂头像），我靠右不挂头像。
/// 长按**单个气泡**＝自绘浮动菜单（复制/选择/编辑/删除），时间戳不逐条挂，改由列表出居中时间。
/// 为什么不用系统 .contextMenu：列表是翻转布局，系统按下/抬起的截图动画拿到的是倒着的视图，
/// 自定义 preview 只管弹出后那一帧，管不到过程（10-08 真机实锤，字整段倒过来）。
private struct MessageRow: View {
    @Environment(\.colorScheme) private var scheme
    let message: ChatMessage
    let charID: String                  // 头像认这个，不认「当前是谁」
    var showAvatar: Bool = true         // 同一人连发的后几条不挂头像（留等宽空位对齐）
    var contentWidth: CGFloat = 320
    var waitingNote: Bool = false       // 插话已送进在飞的那轮、等他看到（PLAN_interject §4.5）
    var menuTarget: BubbleRef? = nil    // 长按菜单当前挂在哪个气泡上（全列表唯一）
    var onMenu: (ActiveBubbleMenu) -> Void = { _ in }
    var onSelectText: (String) -> Void = { _ in }
    var onEdit: (ChatMessage) -> Void = { _ in }
    var onDelete: (ChatMessage) -> Void = { _ in }
    var onTapAvatar: (MessageSender) -> Void = { _ in }
    var onTapImage: (URL) -> Void = { _ in }
    var onTapFile: (URL) -> Void = { _ in }
    var onTapWebpage: (String, String) -> Void = { _, _ in }

    private var isMe: Bool { message.sender == .me }

    /// §1.2：机主气泡走 ChatTint（浅色＝黑底白字）；AI 气泡统一灰底默认字色。
    private var bubbleFill: Color {
        isMe ? ChatTint.fill(IdentityColor.palette(for: "user"), scheme)
             : ChatPalette.current(scheme).bubbleAI
    }
    private var bubbleText: Color? { isMe ? ChatTint.text(scheme) : nil }

    /// 对白气泡的最大宽度。给个明确宽度，MarkdownUI 才会正常换行。
    /// 我这边不挂头像了，多让出头像那一截给气泡。
    private var bubbleMaxWidth: CGFloat { max(200, contentWidth * (isMe ? 0.78 : 0.72)) }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if !isMe {
                if showAvatar {
                    Button { onTapAvatar(.other) } label: { AvatarView(sender: .other, charID: charID) }
                        .buttonStyle(.plain)
                } else {
                    Color.clear.frame(width: 34, height: 1)
                }
            }
            VStack(alignment: isMe ? .trailing : .leading, spacing: 2) {
                content
                if waitingNote {
                    Text("已送达，等他看到")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 4)
                }
            }
            .frame(maxWidth: .infinity, alignment: isMe ? .trailing : .leading)
        }
    }

    /// 给一个气泡挂长按菜单。segment＝这条消息里的第几个气泡；copyText 非 nil＝文字气泡
    /// （给复制/选择/编辑），nil＝图片表情文件这类（只给删除）。
    private func withMenu<V: View>(_ v: V, segment: Int, copyText: String?) -> some View {
        let ref = BubbleRef(messageID: message.id, segment: segment)
        return v.modifier(BubbleMenuModifier(
            active: menuTarget == ref,
            onActivate: { frame in
                onMenu(ActiveBubbleMenu(ref: ref, frame: frame, trailing: isMe,
                                        items: menuItems(copyText)))
            }))
    }

    private func menuItems(_ copyText: String?) -> [BubbleMenuItem] {
        var out: [BubbleMenuItem] = []
        if let t = copyText {
            out.append(BubbleMenuItem(title: "复制") { UIPasteboard.general.string = t })
            out.append(BubbleMenuItem(title: "选择") { onSelectText(t) })
            out.append(BubbleMenuItem(title: "编辑") { onEdit(message) })
        }
        // 删除是整条消息（一条消息可能拆成好几个气泡，存储单位是消息）
        out.append(BubbleMenuItem(title: "删除", destructive: true) { onDelete(message) })
        return out
    }

    /// 选择文本页显示的字：把行内 markdown 记号（** ` 链接等）去掉，复制出来是干净的字。
    private static func selectableText(_ raw: String) -> String {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard let attr = try? AttributedString(markdown: raw, options: opts) else { return raw }
        return String(attr.characters)
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
            withMenu(StickerImage(url: url)
                        .frame(maxWidth: 160, maxHeight: 160)
                        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous)),
                     segment: 0, copyText: nil)
        case .image(let url):
            // 和堆叠卡同款：160×160 填充裁切 + 18 圆角，聊天里所有照片观感一致
            withMenu(Group {
                        if let img = AppFiles.loadImage(url) {
                            Image(uiImage: img).resizable().scaledToFill()
                        } else {
                            Color(.systemGray5)
                                .overlay(Image(systemName: "photo").foregroundStyle(.secondary))
                        }
                    }
                    .frame(width: 160, height: 160)
                    .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .onTapGesture { onTapImage(url) },
                     segment: 0, copyText: nil)
        case .file(let url, let name):
            // 文件卡片：图标 + 文件名，点开 QuickLook 预览
            withMenu(attachmentCard(icon: "doc.fill", title: name,
                                    subtitle: url.pathExtension.uppercased())
                        .onTapGesture { onTapFile(url) },
                     segment: 0, copyText: nil)
        case .webpage(let pid, let title):
            // TA 做的网页：卡片和文件同款质感，点开就是页面
            withMenu(attachmentCard(icon: "doc.richtext.fill", title: title, subtitle: "网页 · 点开查看")
                        .onTapGesture { onTapWebpage(pid, title) },
                     segment: 0, copyText: nil)
        case .system, .memoryNote, .browseNote, .prNote:
            EmptyView()
        }
    }

    /// 文件/网页卡：我发的跟气泡同色（黑底白字）；TA 发的白卡 + 细描边
    /// （聊天背景是 systemGroupedBackground 时 secondarySystemBackground 几乎同色，看着像透明——真机反馈）。
    private func attachmentCard(icon: String, title: String, subtitle: String) -> some View {
        let ink: Color = bubbleText ?? .primary
        return HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 24))
                .foregroundStyle(isMe ? ink.opacity(0.85) : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.callout.weight(.medium)).lineLimit(2)
                    .foregroundStyle(ink)
                Text(subtitle)
                    .font(.caption2)
                    .foregroundStyle(isMe ? ink.opacity(0.6) : Color.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: min(240, bubbleMaxWidth), alignment: .leading)
        .background(isMe ? bubbleFill : Color(.systemBackground),
                    in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay {
            if !isMe {
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(Color(.systemGray4), lineWidth: 0.5)
            }
        }
    }

    /// 文字消息：按段渲染（对白进气泡，代码块脱出成深色卡片）。
    @ViewBuilder private func textBody(_ raw: String) -> some View {
        VStack(alignment: isMe ? .trailing : .leading, spacing: 3) {
            ForEach(Array(messageSegments(raw).enumerated()), id: \.offset) { i, seg in
                segmentView(seg, index: i)
            }
        }
    }

    /// 流式增长中的气泡：纯 Text（不解析 markdown/不上代码高亮），逐片更新才丝滑。
    /// 空行换气泡（U2 路 C）：前面的段定型不再动，新气泡从下面长出来；围栏内不切，
    /// 定稿（isStreaming=false）后由 textBody 走块级解析纠正——流式切错不落盘。
    /// 尾部呼吸点：只挂最后一段，生成中常亮，定稿即消。
    private func streamingBubble(_ raw: String) -> some View {
        let segs = streamingSegments(raw)
        return VStack(alignment: isMe ? .trailing : .leading, spacing: 3) {
            ForEach(Array(segs.enumerated()), id: \.offset) { i, seg in
                HStack(alignment: .bottom, spacing: 6) {
                    Text(seg)
                    if i == segs.count - 1 {
                        StreamingPulseDot()
                            .padding(.bottom, 5)
                    }
                }
                .foregroundStyle(bubbleText ?? .primary)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(bubbleFill)
                )
                .frame(maxWidth: bubbleMaxWidth, alignment: isMe ? .trailing : .leading)
            }
        }
    }

    @ViewBuilder private func segmentView(_ seg: MessageSegment, index: Int) -> some View {
        switch seg {
        case .bubble(let md):
            // 菜单挂在气泡本体上（frame 之前）：变暗/菜单位置都贴着气泡，不是整列宽
            withMenu(MarkdownMessageView(text: md, textColor: bubbleText)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(
                            RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .fill(bubbleFill)
                        ),
                     segment: index, copyText: Self.selectableText(md))
                .frame(maxWidth: bubbleMaxWidth, alignment: isMe ? .trailing : .leading)
        case .card(let md):
            // 只有代码块进这里：深色块自带背景，宽度取气泡列宽、靠发送者那一侧对齐。
            MarkdownMessageView(text: md)
                .frame(maxWidth: bubbleMaxWidth, alignment: isMe ? .trailing : .leading)
        }
    }
}

/// 长按菜单的落点：哪条消息的第几个气泡。
struct BubbleRef: Equatable {
    let messageID: UUID
    let segment: Int
}

private struct BubbleMenuItem: Identifiable {
    let title: String
    var destructive = false
    let action: () -> Void
    var id: String { title }
}

/// 开着的长按菜单：挂在哪个气泡、气泡在屏幕上的位置（激活那一刻量）、靠哪侧、有哪几项。
struct ActiveBubbleMenu {
    let ref: BubbleRef
    let frame: CGRect
    let trailing: Bool
    fileprivate let items: [BubbleMenuItem]
}

/// 气泡长按（微信式浮条）：长按 → 轻震 + 气泡压暗 + 上报位置；菜单本身由 ChatView 画在
/// 列表外层的浮层上（BubbleMenuOverlay）。
/// 为什么不画在气泡自己的 overlay 里：LazyVStack 不认 zIndex，上一行会把菜单盖住（10-08 模拟器实锤）。
private struct BubbleMenuModifier: ViewModifier {
    let active: Bool
    let onActivate: (CGRect) -> Void

    @State private var pending = false

    func body(content: Content) -> some View {
        content
            .overlay {
                if active {
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(Color.gray.opacity(0.28))
                        .allowsHitTesting(false)
                }
            }
            // 只在长按那一刻量一次全局位置——别用 onGeometryChange 常驻，滚动时每个气泡每帧都会回调。
            // .global 拿到的是翻转之后的真实屏幕坐标（同 PhotoStackCard 拖动的口径）。
            .background {
                GeometryReader { g in
                    Color.clear.onChange(of: pending) { _, on in
                        guard on else { return }
                        pending = false
                        onActivate(g.frame(in: .global))
                    }
                }
            }
            .onLongPressGesture(minimumDuration: 0.35) {
                UIImpactFeedbackGenerator(style: .medium).impactOccurred()
                pending = true
            }
            .animation(.easeOut(duration: 0.15), value: active)
    }
}

/// 列表外层的菜单浮层：按气泡的屏幕位置摆。
/// 竖向：优先浮在气泡上方；上方被顶栏挡住就挂下方；气泡比屏还高就钉在可见区顶部。
/// 横向：我的贴气泡右沿、TA 的贴气泡左沿，再夹进屏幕内。
private struct BubbleMenuOverlay: View {
    let menu: ActiveBubbleMenu
    let viewport: CGRect          // 聊天区的屏幕范围（浮层自己的坐标原点就是它的左上角）
    let onDismiss: () -> Void

    var body: some View {
        BubbleMenuBar(items: menu.items, onDismiss: onDismiss)
            .offset(x: x - viewport.minX, y: y - viewport.minY)
    }

    private var size: CGSize { BubbleMenuBar.size(count: menu.items.count) }

    private var x: CGFloat {
        let raw = menu.trailing ? menu.frame.maxX - size.width : menu.frame.minX
        return min(max(raw, viewport.minX + 8), viewport.maxX - size.width - 8)
    }

    private var y: CGFloat {
        let gap: CGFloat = 8, f = menu.frame
        let top = viewport.minY + gap, bottom = viewport.maxY - gap
        if f.minY - gap - size.height >= top { return f.minY - gap - size.height }  // 上方
        if f.maxY + gap + size.height <= bottom { return f.maxY + gap }             // 下方
        return max(top, f.minY)                                                    // 钉在可见区顶部
    }
}

private struct BubbleMenuBar: View {
    let items: [BubbleMenuItem]
    let onDismiss: () -> Void

    static let itemWidth: CGFloat = 50
    static let height: CGFloat = 38
    static let hPad: CGFloat = 4
    /// 尺寸是定死的（每项等宽），浮层摆位直接算，不用量。
    static func size(count: Int) -> CGSize {
        CGSize(width: CGFloat(count) * itemWidth + hPad * 2, height: height)
    }

    var body: some View {
        HStack(spacing: 0) {
            ForEach(items) { item in
                Button {
                    onDismiss()
                    item.action()
                } label: {
                    Text(item.title)
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(item.destructive ? Color(hex: 0xFF6B60) : .white)
                        .frame(width: Self.itemWidth, height: Self.height)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, Self.hPad)
        .background(Color(hex: 0x1C1C1E), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .shadow(color: .black.opacity(0.18), radius: 10, y: 4)
    }
}

/// 「选择」要的那段字：带个 id 好挂 .sheet(item:)。
struct SelectTextItem: Identifiable {
    let id = UUID()
    let text: String
}

/// 「选择文本」页：只读 UITextView，进来全选，拖手柄挑一段——系统菜单的「拷贝」只拷选中的。
/// （SwiftUI 的 .textSelection 在 iOS 上只能整段拷，做不到部分选择，所以走 UIKit。）
struct SelectableTextSheet: View {
    let text: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            SelectableTextView(text: text)
                .padding(.horizontal, 8)
                .navigationTitle("选择文本")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("完成") { dismiss() }
                    }
                }
        }
        .presentationDetents([.medium, .large])
        .tint(Color.chatInk)   // 选区手柄/「完成」跟聊天页功能色走黑白，不吃全局棕 tint
    }
}

private struct SelectableTextView: UIViewRepresentable {
    let text: String

    func makeUIView(context: Context) -> UITextView {
        let tv = UITextView()
        tv.isEditable = false
        tv.isSelectable = true
        tv.backgroundColor = .clear
        tv.font = .preferredFont(forTextStyle: .body)
        tv.adjustsFontForContentSizeCategory = true
        tv.textContainerInset = UIEdgeInsets(top: 12, left: 8, bottom: 24, right: 8)
        tv.text = text
        // 等 sheet 弹完再全选，太早 becomeFirstResponder 不出手柄
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            tv.becomeFirstResponder()
            tv.selectedRange = NSRange(location: 0, length: (text as NSString).length)
        }
        return tv
    }

    func updateUIView(_ tv: UITextView, context: Context) {
        if tv.text != text { tv.text = text }
    }
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
            RoundedRectangle(cornerRadius: 18)
                .fill(Color(.systemGray5))
                .overlay(Image(systemName: "photo").foregroundStyle(.secondary))
        }
    }
}

/// 连发多图的行：头像 + 堆叠照片卡 + 时间戳（取最后一张的时间）。
private struct ImageStackRow: View {
    let messages: [ChatMessage]                       // ≥2，同 sender 的连续图片
    let charID: String                                // 头像认这个，不认「当前是谁」
    var showAvatar: Bool = true                       // 同一人连发的后几条不挂头像
    var onTapStack: ([URL], Int) -> Void = { _, _ in }
    var onTapAvatar: () -> Void = { }

    private var isMe: Bool { messages[0].sender == .me }
    private var urls: [URL] {
        messages.compactMap { m in
            if case .image(let u) = m.kind { return u }
            return nil
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if !isMe {
                if showAvatar {
                    Button { onTapAvatar() } label: { AvatarView(sender: .other, charID: charID) }
                        .buttonStyle(.plain)
                } else {
                    Color.clear.frame(width: 34, height: 1)
                }
            }
            PhotoStackCard(urls: urls) { i in onTapStack(urls, i) }
                .frame(maxWidth: .infinity, alignment: isMe ? .trailing : .leading)
        }
    }
}

/// 微信风堆叠照片卡（PhotoStack 交互的原生独立实现，手感对齐 mianmian 版）：
/// 拖动时顶图跟手位移+顺向微旋（~8°封顶），下一张从底下由暗到亮、由小到大浮起；
/// 松手超半程或快甩 → 顶图飞出、下张升顶（~0.22s 缓出），不够 → 弹簧回位。
/// 点卡=全屏查看器从当前图开始翻页。
struct PhotoStackCard: View {
    let urls: [URL]
    var onTap: (Int) -> Void = { _ in }
    @State private var index = 0
    @State private var dragX: CGFloat = 0

    private let cardW: CGFloat = 160
    private var current: Int { min(index, max(urls.count - 1, 0)) }
    /// 本次拖动方向下即将升顶的那张（左滑→下一张，右滑→上一张；未拖动时预览下一张）。
    private var incoming: Int {
        guard urls.count > 1 else { return current }
        return dragX > 0 ? (current - 1 + urls.count) % urls.count
                         : (current + 1) % urls.count
    }
    /// 拖动进度 0~1。
    private var progress: CGFloat { min(abs(dragX) / cardW, 1) }

    var body: some View {
        ZStack {
            // 最底叠影：静态底片，示意"还有更多"
            if urls.count > 2 {
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(Color(.systemGray4))
                    .frame(width: 152, height: 152)
                    .rotationEffect(.degrees(4))
                    .offset(x: 7, y: 5)
            }
            // 升顶中的下层卡：随拖动进度 由小变大、由暗变亮
            photo(incoming)
                .frame(width: cardW, height: cardW)
                .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                .scaleEffect(0.9 + 0.1 * progress)
                .opacity(urls.count > 1 ? 0.55 + 0.45 * Double(progress) : 0)
                .rotationEffect(.degrees(Double(1 - progress) * -3))
            // 顶层卡：跟手位移 + 顺向旋转（~8° 封顶）
            photo(current)
                .frame(width: cardW, height: cardW)
                .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                .overlay(alignment: .topTrailing) {
                    Text("\(current + 1)/\(urls.count)")
                        .font(.caption.bold())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(.black.opacity(0.55)))
                        .padding(6)
                }
                .offset(x: dragX)
                .rotationEffect(.degrees(Double(max(-8, min(8, dragX / 9)))))
        }
        .padding(.horizontal, 8)   // 给叠影留探头空间
        .contentShape(Rectangle())
        .onTapGesture { onTap(current) }
        // coordinateSpace 用 .global：行套在翻转布局里（rotation+scale），局部坐标方向会被
        // 变换搅乱，全局空间拿到的是手指在屏幕上的真实位移。横向占优才认，纵向让给列表滚动。
        .gesture(
            DragGesture(minimumDistance: 15, coordinateSpace: .global)
                .onChanged { v in
                    guard urls.count > 1,
                          abs(v.translation.width) > abs(v.translation.height) else { return }
                    dragX = v.translation.width
                }
                .onEnded { v in
                    guard urls.count > 1, dragX != 0 else { return }
                    // 判定：过半程，或快甩（预测终点远超实际=有速度）
                    let fling = abs(v.predictedEndTranslation.width) > cardW * 0.55
                        && abs(v.translation.width) > 12
                    if progress > 0.5 || fling {
                        let target = incoming
                        let out = dragX < 0 ? -cardW * 1.4 : cardW * 1.4
                        withAnimation(.easeOut(duration: 0.22)) {
                            dragX = out
                        } completion: {
                            index = target
                            dragX = 0          // 新顶卡就位，瞬时复位（视觉上是下层升顶完成）
                        }
                    } else {
                        withAnimation(.spring(duration: 0.3)) { dragX = 0 }
                    }
                }
        )
    }

    @ViewBuilder private func photo(_ i: Int) -> some View {
        if urls.indices.contains(i), let img = AppFiles.loadImage(urls[i]) {
            Image(uiImage: img).resizable().scaledToFill()
        } else {
            Color(.systemGray5)
                .overlay(Image(systemName: "photo").foregroundStyle(.secondary))
        }
    }
}

/// 圆形头像。有真实头像就显示图片，否则显示占位。大小可调。
struct AvatarView: View {
    @EnvironmentObject private var profile: ProfileStore
    let sender: MessageSender
    /// 对方是**谁**（sender == .me 时用不上）。必传：缺身份要在编译期暴露，
    /// 别给它编一个默认值，静默兜底会把「缺身份」变成「错身份」。
    let charID: String
    var size: CGFloat = 34

    var body: some View {
        Group {
            if let img = profile.avatar(for: sender, char: charID) {
                Image(uiImage: img)
                    .resizable()
                    .scaledToFill()
            } else {
                // 我方默认头像用主题深色（别用 accentColor——它在自定义视图里会回落成系统蓝）
                (sender == .me ? Color.userAccent : Color(.systemGray3))
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
