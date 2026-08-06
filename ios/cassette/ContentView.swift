import SwiftUI
import PhotosUI

struct ContentView: View {
    @StateObject private var chatStore = ChatStore()       // 聊天记录的主人（本地持久化）
    @StateObject private var profileStore = ProfileStore() // 头像 + 顶栏标题
    @StateObject private var proactiveStore = ProactiveSettingsStore() // 主动消息设置
    @StateObject private var stickerStore = StickerStore() // 表情包库

    @Environment(\.scenePhase) private var scenePhase

    @State private var draft: String = ""
    @State private var drawerOpen = false            // 抽屉（猫爪/左缘右滑开，阴影点击/左滑关）
    @State private var navPath: [DrawerPage] = []    // 抽屉 push 的页面栈
    @AppStorage("hasOnboarded") private var hasOnboarded = false   // 首启起名引导

    // 点头像 → 底部弹按钮（改昵称 / 换头像）
    @State private var avatarActionTarget: MessageSender? = nil   // 弹选项的对象
    @State private var renameTarget: MessageSender? = nil         // 改名弹窗的对象
    @State private var nameDraft = ""
    @State private var avatarPickerTarget: MessageSender? = nil   // 换头像的对象
    @State private var avatarPickerItem: PhotosPickerItem? = nil
    @State private var showAvatarPicker = false
    @State private var showStickers = false          // 表情包面板
    @State private var pendingSticker: Sticker? = nil // 暂存待发的表情（可配文字一起发）

    // 后端联动状态
    @State private var sessionId: String? = nil   // 后端返回的会话 id（无状态后端仅用于记账）
    @State private var isWaiting: Bool = false    // 等待对方回复中（"正在输入"指示；首个正文 chunk 到即熄）
    @State private var isGenerating = false       // 整轮生成中（从发出到流结束）：禁用发送——
                                                  // 只看 isWaiting 会在正文开始流出后放开按钮，
                                                  // 再发一条就是两个 SSE 流并发互踩（气泡交错/记账混乱）
    @State private var errorText: String? = nil   // 发送失败提示

    /// 一条等待补投的轮：断流时的半截气泡 ids + 登记时间 + 是否已放弃等待（放弃后仍留着兜迟到补投）。
    struct RescueWait {
        var ids: [UUID]
        var since: Date
        var givenUp: Bool = false
    }

    // 断连补投等待表：req_id → RescueWait。断流**无条件登记**（没冒正文也登记，三个点靠它维持）；
    // 补投带 req_id 回来 → 撤半截、上完整回复；对账发现后端没在跑 → givenUp（熄点+提示重发）。
    @State private var rescueWaiting: [String: RescueWait] = [:]

    /// 有没有还在等的补投（驱动"正在输入"三个点；发送禁用仍只看 isWaiting）。
    private var rescueActive: Bool { rescueWaiting.values.contains { !$0.givenUp } }

    // 编辑消息弹窗状态
    @State private var editingMessage: ChatMessage? = nil
    @State private var editingText: String = ""
    @State private var editRefreshTick = 0   // 亲手编辑/删除的信号：ChatView 收到就手术式合并进冻结快照
    @State private var deleteTarget: ChatMessage? = nil   // 长按气泡 → 删除确认

    private let chatService = ChatService()

    var body: some View {
        ZStack {
            NavigationStack(path: $navPath) {
                VStack(spacing: 0) {
                    header
                    chatBody
                }
                .toolbar(.hidden, for: .navigationBar)   // 聊天页用自定义顶栏
                .navigationDestination(for: DrawerPage.self) { destination(for: $0) }
                // 左缘右滑开抽屉（iOS 标准返回同方向，但只在栈空的聊天页生效，不打架）。
                // simultaneousGesture：别抢聊天区的点击/滚动。
                .simultaneousGesture(
                    DragGesture(minimumDistance: 15, coordinateSpace: .global)
                        .onEnded { v in
                            if navPath.isEmpty, !drawerOpen,
                               v.startLocation.x < 28, v.translation.width > 45,
                               abs(v.translation.height) < 70 {
                                dismissKeyboard()
                                drawerOpen = true
                            }
                        })
            }
            drawerLayer
        }
        .environmentObject(profileStore)
        // 页面返回（栈清空）→ 回到抽屉打开态：层级是 聊天 → 抽屉 → 页面。
        .onChange(of: navPath) { old, new in
            if !old.isEmpty && new.isEmpty { drawerOpen = true }
        }
        // 待送达同步：前台时拉一次，并每 15s 轮询（断连补投的回复靠这条通道回来）。
        // .task(id: scenePhase)：进 active 启动、离开 active 自动取消循环，省电。
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            await syncPending()
            await reconcileRescues()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                await syncPending()
                await reconcileRescues()
            }
        }
    }

    /// 抽屉项 → 页面（占位页随 PR2-5/PR8 逐个换真）。
    @ViewBuilder
    private func destination(for page: DrawerPage) -> some View {
        switch page {
        case .memory:
            MemoryPage()
        case .mind:
            MindPage()
        case .settings:
            ProactiveSettingsView(store: proactiveStore)
        default:
            DrawerPlaceholderPage(page: page)
        }
    }

    /// 抽屉层：阴影 + 面板。点阴影/阴影区左滑关；选中项 → 关抽屉 + push。
    @ViewBuilder
    private var drawerLayer: some View {
        ZStack(alignment: .leading) {
            if drawerOpen {
                Color.black.opacity(0.35)
                    .ignoresSafeArea()
                    .contentShape(Rectangle())
                    .onTapGesture { drawerOpen = false }
                    .gesture(DragGesture(minimumDistance: 20)
                        .onEnded { v in if v.translation.width < -30 { drawerOpen = false } })
                    .transition(.opacity)
                DrawerPanel(agentName: topTitle) { page in
                    drawerOpen = false
                    navPath.append(page)
                }
                .transition(.move(edge: .leading))
            }
        }
        .animation(.easeInOut(duration: 0.22), value: drawerOpen)
    }

    // MARK: - 界面

    private var chatBody: some View {
        ChatView(messages: chatStore.messages,
                 isWaiting: isWaiting || rescueActive,   // 后台生成期间点点不灭，补投到达才熄
                 onEdit: startEdit,
                 onDelete: { msg in deleteTarget = msg },
                 onTapChatArea: { if showStickers { showStickers = false } },
                 onTapAvatar: { sender in dismissKeyboard(); avatarActionTarget = sender },
                 editRefreshTick: editRefreshTick)
            .background(Color(.systemGroupedBackground))
            .safeAreaInset(edge: .bottom, spacing: 0) {
                VStack(spacing: 0) {
                    if showStickers {
                        Divider()
                        StickerPanel(store: stickerStore, onPick: stageSticker)
                            .frame(height: 300)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                    if let st = pendingSticker {
                        Divider()
                        PendingStickerBar(imageURL: stickerStore.imageURL(for: st)) {
                            pendingSticker = nil
                        }
                    }
                    InputBar(text: $draft,
                             stickersActive: showStickers,
                             sending: isGenerating || isWaiting,
                             hasAttachments: pendingSticker != nil,
                             onStickers: toggleStickers,
                             onSend: send)
                }
                .animation(.easeInOut(duration: 0.2), value: showStickers)
                .animation(.easeInOut(duration: 0.2), value: pendingSticker)
            }
            .alert("发送失败", isPresented: Binding(
                get: { errorText != nil },
                set: { if !$0 { errorText = nil } }
            )) {
                Button("好", role: .cancel) { }
            } message: {
                Text(errorText ?? "")
            }
            .sheet(item: $editingMessage) { message in
                EditMessageSheet(
                    text: $editingText,
                    canRegenerate: message.sender == .me,   // 只有自己的消息给「重新生成」
                    onEdit: { saveEdit(message: message, regenerate: false) },
                    onRegenerate: { saveEdit(message: message, regenerate: true) },
                    onCancel: { editingMessage = nil }
                )
            }
            // 长按气泡 → 删除确认：从本地历史移除，之后发给后端的历史自然不再包含它。
            .confirmationDialog("删除这条消息？", isPresented: Binding(
                get: { deleteTarget != nil },
                set: { if !$0 { deleteTarget = nil } }
            ), titleVisibility: .visible) {
                Button("删除", role: .destructive) {
                    if let msg = deleteTarget {
                        chatStore.remove(id: msg.id)
                        editRefreshTick += 1   // 离底冻结快照就地撤行，不用滑回底部
                    }
                }
                Button("取消", role: .cancel) { }
            }
            .fullScreenCover(isPresented: Binding(
                get: { !hasOnboarded },
                set: { if !$0 { hasOnboarded = true } }
            )) {
                OnboardingView(store: proactiveStore, profile: profileStore) { hasOnboarded = true }
            }
            // 点头像的选项弹层：改昵称 / 换头像。
            .confirmationDialog("", isPresented: Binding(
                get: { avatarActionTarget != nil },
                set: { if !$0 { avatarActionTarget = nil } }
            ), titleVisibility: .hidden) {
                Button("修改昵称") {
                    if let t = avatarActionTarget {
                        nameDraft = t == .me ? proactiveStore.settings.userName
                                             : proactiveStore.settings.agentName
                        renameTarget = t
                    }
                }
                Button("修改头像") {
                    if let t = avatarActionTarget {
                        avatarPickerTarget = t
                        showAvatarPicker = true
                    }
                }
                Button("取消", role: .cancel) { }
            }
            .alert("修改昵称", isPresented: Binding(
                get: { renameTarget != nil },
                set: { if !$0 { renameTarget = nil } }
            )) {
                TextField("名字", text: $nameDraft)
                Button("取消", role: .cancel) { }
                Button("保存") {
                    let t = renameTarget
                    let name = nameDraft.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard let t, !name.isEmpty else { return }
                    if t == .me { proactiveStore.settings.userName = String(name.prefix(20)) }
                    else { proactiveStore.settings.agentName = String(name.prefix(20)) }
                    Task { await proactiveStore.pushToServer() }
                }
            }
            .photosPicker(isPresented: $showAvatarPicker,
                          selection: $avatarPickerItem, matching: .images)
            .onChange(of: avatarPickerItem) { _, item in
                guard let item, let target = avatarPickerTarget else { return }
                Task {
                    if let data = try? await item.loadTransferable(type: Data.self),
                       let img = UIImage(data: data) {
                        profileStore.setAvatar(target, image: img)
                    }
                    avatarPickerItem = nil
                    avatarPickerTarget = nil
                }
            }
    }

    /// 顶栏标题 = AI 的名字（首启起的，设置页可改）。抽屉顶部也用它。
    private var topTitle: String {
        proactiveStore.settings.agentName.isEmpty ? "cassette" : proactiveStore.settings.agentName
    }

    // 顶部导航栏：左猫爪开抽屉，居中标题；右侧空占位配平保持标题居中
    // （原右上角设置齿轮已搬进抽屉）。
    private var header: some View {
        HStack {
            Button {
                dismissKeyboard()
                drawerOpen = true
            } label: {
                Image(systemName: "pawprint")
                    .font(.system(size: 18, weight: .medium))
                    .foregroundStyle(Color.theme)
                    .frame(width: 40, height: 40)
            }
            Spacer()
            Text(topTitle)
                .font(.headline)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer()
            Color.clear.frame(width: 40, height: 40)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
    }

    // MARK: - 发送 / 流式接收

    /// 点表情：暂存起来（浮在输入框上方），可再打字，按发送一起发。
    private func stageSticker(_ sticker: Sticker) {
        pendingSticker = sticker
        showStickers = false
    }

    private func toggleStickers() {
        dismissKeyboard()
        showStickers.toggle()
    }

    private func send() {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !isGenerating, !trimmed.isEmpty || pendingSticker != nil else { return }
        isGenerating = true   // 同步占位：立刻禁用发送、挡住极快的重复触发
        isWaiting = true

        if !trimmed.isEmpty {
            chatStore.append(ChatMessage(sender: .me, kind: .text(trimmed), timestamp: Date()))
        }
        // 暂存的表情：作为表情消息上屏（历史里以 [表情包：描述] 发给后端，模型看描述）。
        if let st = pendingSticker {
            chatStore.append(ChatMessage(sender: .me,
                                         kind: .sticker(stickerStore.imageURL(for: st), st.description),
                                         timestamp: Date()))
            pendingSticker = nil
        }

        // 清空输入框（同步 + 下一轮再清一次，盖过中文输入法候选字写回）。
        draft = ""
        DispatchQueue.main.async { draft = "" }

        Task { await generateReply() }
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil
        )
    }

    /// 把当前完整历史发给后端（流式），回复逐字上屏；失败则弹提示。
    /// 约定调用前 chatStore.messages 已以用户的新消息结尾。
    @MainActor
    private func generateReply() async {
        isGenerating = true
        isWaiting = true
        defer { isWaiting = false; isGenerating = false }
        var streamingId: UUID? = nil     // 当前正在流式增长的气泡 id（nil=还没冒正文）
        var streamedText = ""            // 当前气泡已流出的文字（break 后清零）
        var sawBreak = false             // 这轮是否发生过工具切段（多气泡 → done 时不用 reply 覆盖）
        var sawDone = false              // 收到过 done——收到就删登记（防僵尸记录挂住三个点）
        var turnIds: [UUID] = []         // 本轮所有正文气泡 id（边产生边同步进登记）
        let reqId = UUID().uuidString    // 断连补投的关联 id（后端 rescue 条目带回）
        // **轮一开始就登记**：半开连接下补投可能先于流报错到达（后端跑完投 pending、
        // 这边流还在 600s 空闲超时里干等）——登记晚了 syncPending 关联扑空，
        // 半截+完整双份并存。提前登记后补投任何时刻到都能撤半截。
        rescueWaiting[reqId] = RescueWait(ids: [], since: Date())
        do {
            let stream = chatService.sendStream(history: chatStore.messages,
                                                sessionId: sessionId,
                                                stickers: stickerStore.stickers, reqId: reqId)
            for try await ev in stream {
                switch ev {
                case .text(let chunk):
                    isWaiting = false     // 正文开始冒 → 收起"正在输入"
                    if let id = streamingId {
                        streamedText += chunk
                        chatStore.updateTextNoSave(id: id, newText: streamedText)
                    } else {
                        let msg = ChatMessage(sender: .other, kind: .text(chunk),
                                              timestamp: Date(), isStreaming: true)
                        streamingId = msg.id
                        streamedText = chunk
                        turnIds.append(msg.id)
                        rescueWaiting[reqId]?.ids = turnIds   // 气泡 id 边产生边同步进登记
                        chatStore.appendNoSave(msg)
                    }
                case .textBreak:
                    // 工具调用切段：这一段是正经说过的话 → 定稿保留（落盘），下一段另起气泡
                    if let id = streamingId {
                        if streamedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            chatStore.remove(id: id)
                        } else {
                            chatStore.editText(id: id, newText: streamedText)
                        }
                    }
                    streamingId = nil
                    streamedText = ""
                    sawBreak = true
                    // 对方去用工具了，下一段还没来 → 重新亮"正在输入"，
                    // 分清"说完了"和"还在忙"（下一段 .text 一到会自动收起）
                    isWaiting = true
                case .memory(let tool, _):
                    // 中途记忆操作 → 就地内联灰字
                    chatStore.appendMemoryNote(memoryNoteText(tool: tool))
                case .error(let msg):
                    errorText = msg
                case .done(let resp):
                    sawDone = true
                    finalizeStreamedReply(resp, streamingId: streamingId,
                                          streamedTail: sawBreak ? streamedText : nil)
                    streamingId = nil
                }
            }
            // 流断了没收到 done（切出 app/锁屏/网络抖/后端重启）：半截先保留、**无条件登记**等待补投
            // （没冒正文也登记——三个点靠它维持，不然切后台回来动画就丢了）。
            // 后端活着 → 补投带 req_id 回来，syncPending 撤半截换完整版；
            // 后端没在跑这轮 → reconcileRescues 对账后熄点提示重发。
            if let id = streamingId {
                if streamedText.isEmpty { chatStore.remove(id: id); turnIds.removeAll { $0 == id } }
                else { chatStore.editText(id: id, newText: streamedText) }
            }
            if sawDone { rescueWaiting.removeValue(forKey: reqId) }
            else { rescueWaiting[reqId]?.ids = turnIds }   // 流断没 done：登记留着等补投
        } catch {
            if let id = streamingId {
                if streamedText.isEmpty { chatStore.remove(id: id); turnIds.removeAll { $0 == id } }
                else { chatStore.editText(id: id, newText: streamedText) }
            }
            // 中途断连（切后台/锁屏被掐流）不弹错：后端照跑，补投机制会把完整回复送回来，
            // 弹窗纯属误报。其余错误（连不上/4xx…）是明确失败：弹提示 + 删登记，不再空等。
            if case ChatServiceError.connectionLost = error {
                rescueWaiting[reqId]?.ids = turnIds   // 静默：半截保留，登记等补投
            } else {
                rescueWaiting.removeValue(forKey: reqId)
                errorText = (error as? ChatServiceError)?.errorDescription ?? error.localizedDescription
            }
        }
    }

    /// 流结束：把流式气泡对齐到权威 reply（标记剥净 / 空白规整）。
    /// resp=nil（空/错）时只清半截气泡。streamedTail ≠ nil ＝这轮被工具切成了多段（sawBreak）：
    /// 前面的气泡已各自定稿，resp.reply 是全段拼接（覆盖最后气泡会把前面的话重复一遍）
    /// → 最后气泡用流出的原文定稿。
    private func finalizeStreamedReply(_ resp: ChatResponse?, streamingId: UUID?,
                                       streamedTail: String? = nil) {
        isWaiting = false
        guard let resp = resp else {
            if let id = streamingId { chatStore.remove(id: id) }
            return
        }
        sessionId = resp.session_id
        if let tail = streamedTail {
            // 多段：只定稿最后一段（空就删掉半截气泡），不动前面已定稿的。
            if let id = streamingId {
                if tail.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    chatStore.remove(id: id)
                } else {
                    chatStore.editText(id: id, newText: tail)
                }
            }
        } else {
            // 单段：流出的是"临时"的，done 带权威 reply → 替换气泡内容 + 落盘。
            let clean = resp.reply.trimmingCharacters(in: .whitespacesAndNewlines)
            if let id = streamingId {
                if clean.isEmpty { chatStore.remove(id: id) }
                else { chatStore.editText(id: id, newText: resp.reply) }
            } else if !clean.isEmpty {
                chatStore.append(ChatMessage(sender: .other, kind: .text(resp.reply), timestamp: Date()))
            }
        }
        // 他挑着发来的表情 → 作为对方表情消息上屏。
        for id in resp.sticker_sends ?? [] {
            if let st = stickerStore.sticker(id: id) {
                chatStore.append(ChatMessage(sender: .other,
                                             kind: .sticker(stickerStore.imageURL(for: st), st.description),
                                             timestamp: Date()))
            }
        }
        // 他改了某些表情的描述 → 应用 + 灰字提示。
        if let updates = resp.desc_updates, !updates.isEmpty {
            for u in updates { stickerStore.updateDescription(id: u.id, u.description) }
            chatStore.appendMemoryNote(updates.count == 1
                ? "更新了一个表情的描述" : "更新了 \(updates.count) 个表情的描述")
        }
        // 他这轮顺手定了下次醒来 → 灰字提示（后端已拼好文案：原话 + 绝对时间点）。
        if let hint = resp.next_wake_hint, !hint.isEmpty {
            chatStore.appendMemoryNote(hint)
        }
    }

    /// 一次记忆操作的灰字文案（内容后续在记忆页看，这里只标动作）。
    private func memoryNoteText(tool: String) -> String {
        tool == "trace" ? "调整了一条记忆" : "记住了一件事"
    }

    // MARK: - 待送达同步（断连补投）

    /// 拉取后端积压的消息，并进本地聊天记录，再 ack（先存后 ack，防丢）。
    @MainActor
    private func syncPending() async {
        do {
            let pending = try await chatService.getPending()
            guard !pending.isEmpty else { return }
            for p in pending {
                let ts = Date(timeIntervalSince1970: TimeInterval(p.ts))
                // error 标记条目＝那轮没产出（claude 挂了）：清等待熄点+提示重发；
                // 半截气泡**不撤**——那是他真说过的话。
                // 提示只在真清掉了等待条目时追加一次：ack 失败重拉时条目已不在，不再重复灰字。
                if p.error == true {
                    if let rid = p.req_id, !rid.isEmpty,
                       rescueWaiting.removeValue(forKey: rid) != nil {
                        chatStore.appendSystemMessage("刚才那条没生成出来，重发试试")
                    }
                    continue
                }
                // 断连补投条目：先撤当时留下的半截气泡，再上完整回复（req_id 关联）。
                if let rid = p.req_id, !rid.isEmpty, let wait = rescueWaiting[rid] {
                    for id in wait.ids { chatStore.remove(id: id) }
                    rescueWaiting.removeValue(forKey: rid)
                }
                if !p.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    chatStore.insertProactive(text: p.text, timestamp: ts)
                }
                // 他醒来时配的表情：按 id 取本地图，单独当表情消息上屏。
                for sid in p.sticker_ids ?? [] {
                    if let st = stickerStore.sticker(id: sid) {
                        chatStore.insertProactiveSticker(url: stickerStore.imageURL(for: st),
                                                         description: st.description, timestamp: ts)
                    }
                }
            }
            try await chatService.ackPending(ids: pending.map { $0.id })
        } catch {
            // 静默失败：下次轮询再拉，消息在后端 outbox 不会丢。
        }
    }

    /// 补投对账：问后端哪些轮还在跑。不在跑且过了 60s 宽限 → 放弃等待（熄点+提示重发，
    /// 条目留着兜迟到补投）；超过绝对上限（后端超时+守护余量）整条删。
    /// 修的场景：请求根本没到后端（connectionLost 被静默吞）→ 以前点点没了也没任何提示。
    @MainActor
    private func reconcileRescues() async {
        guard !rescueWaiting.isEmpty else { return }
        guard let active = try? await chatService.chatActive() else { return }   // 连不上后端时不误判
        let now = Date()
        for (rid, wait) in rescueWaiting {
            if now.timeIntervalSince(wait.since) > 1920 {   // 后端 CLAUDE_TIMEOUT+守护60s+余量
                rescueWaiting.removeValue(forKey: rid)
                continue
            }
            if !wait.givenUp, !active.contains(rid), now.timeIntervalSince(wait.since) > 60 {
                rescueWaiting[rid]?.givenUp = true
                chatStore.appendSystemMessage("刚才那条可能没送到后端，重发试试")
            }
        }
    }

    // MARK: - 编辑 / 重新生成

    /// gobackward：打开编辑弹窗（弹窗里 Edit / Regenerate 按钮按发送者不同）。
    private func startEdit(_ message: ChatMessage) {
        editingText = message.plainText
        editingMessage = message
    }

    /// 保存编辑。regenerate=true（仅自己的消息）时还会把这条之后的旧对话清掉、让后端重答。
    private func saveEdit(message: ChatMessage, regenerate: Bool) {
        let newText = editingText.trimmingCharacters(in: .whitespacesAndNewlines)
        editingMessage = nil
        guard !newText.isEmpty else { return }
        // 「编辑并重新回复」＝相当于重发，刷新时间；「仅修改」保留原时间。
        chatStore.editText(id: message.id, newText: newText, updateTimestamp: regenerate)
        editRefreshTick += 1   // 亲手编辑立即上屏（离底冻结快照做手术式合并）
        if regenerate, !isGenerating {
            chatStore.truncateAfter(id: message.id)   // 删掉这条之后的旧对话
            Task { await generateReply() }
        }
    }
}

#Preview {
    ContentView()
}
