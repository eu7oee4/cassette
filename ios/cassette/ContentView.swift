import SwiftUI
import PhotosUI
import QuickLook
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject private var chatStore = ChatStore()       // 聊天记录的主人（本地持久化，按会话分仓）
    @StateObject private var profileStore = ProfileStore() // 头像 + 顶栏标题
    @StateObject private var proactiveStore = ProactiveSettingsStore() // 主动消息设置（当前角色的）
    @StateObject private var stickerStore = StickerStore() // 表情包库
    @StateObject private var charListStore = CharacterListStore() // 角色清单（会话列表数据源）

    // 当前会话角色。ChatService 拼请求时读同一个 key（CurrentCharacter），天然对齐。
    @AppStorage(CurrentCharacter.key) private var currentCharID = "default"

    @Environment(\.scenePhase) private var scenePhase

    @State private var draft: String = ""
    @State private var drawerOpen = false            // 抽屉（猫爪/左缘右滑开，阴影点击/左滑关）
    @State private var navPath: [DrawerPage] = []
    @State private var draftCount = 0    // 草稿信箱待寄数（抽屉角标；挂在前台轮询里刷新）    // 抽屉 push 的页面栈
    @State private var chatScrollTarget: UUID? = nil // 聊天记录页点行 → 聊天跳到那条气泡
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
    @State private var pendingImages: [Data] = []    // 暂存待发的照片（jpeg，可配文字一起发）
    @State private var pendingFiles: [OutgoingFile] = []   // 暂存待发的文件
    @State private var showAttachMenu = false        // ➕ 的选项弹层（照片/文件）
    @State private var showAttachPicker = false      // 照片选择器
    @State private var showFileImporter = false      // 文件选择器
    @State private var attachPickerItems: [PhotosPickerItem] = []
    @State private var enlargedImage: EnlargedImage? = nil   // 点图片气泡 → 全屏查看
    @State private var quickLookURL: URL? = nil      // 点文件卡片 → QuickLook 预览

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
        /// 这轮的流还开着（app 在场收正文）：不驱动三个点、不参与对账判死。
        /// 登记提前到了"轮一开始"（防补投先于流报错到达），少了这个字段整个流式期间
        /// rescueActive 恒为 true → 三个点气泡和正在逐字长出来的气泡同时挂着。
        var inFlight: Bool = false
    }

    // 断连补投等待表：req_id → RescueWait。断流**无条件登记**（没冒正文也登记，三个点靠它维持）；
    // 补投带 req_id 回来 → 撤半截、上完整回复；对账发现后端没在跑 → givenUp（熄点+提示重发）。
    @State private var rescueWaiting: [String: RescueWait] = [:]

    /// 有没有还在等的补投（驱动"正在输入"三个点；发送禁用仍只看 isWaiting）。
    private var rescueActive: Bool { rescueWaiting.values.contains { !$0.givenUp && !$0.inFlight } }

    // Code 模式：消息改道 tmux 交互会话（那边手上有整台电脑），回复走待送达盒子回来。
    // codeMode 的真相在后端（会话活着 = 模式开着），@AppStorage 只是冷启动前的乐观值，
    // 每次回前台都用 /code/status 对齐一次。
    @AppStorage("codeMode") private var codeMode = false
    @State private var codeAvailable = false          // 后端开了 Code 模式吗（没开就完全不露入口）
    @State private var codeSwitching = false          // 正在切换中：按钮转圈、挡住连点
    @State private var codeOwner = ""                 // 「电脑上的会话」归哪个角色（空=旧后端，不设限）
    @State private var codeOwnerName = ""             // 同上，显示用的名字
    @State private var terminalExpanded = false       // 内联终端面板展开着吗
    @State private var confirmStopBusy = false        // 退出时那边正干着活 → 先问一句
    // 游戏（game_bridge）：剧情会话复用 code 那套终端面板和消息改道；急停/引擎状态给顶栏 ⏸。
    @State private var gameSessionActive = false      // 剧情会话活着（/code/status 的 profile=game）
    @State private var gamePaused = false             // 急停锁状态（真相在后端 /game）
    @State private var gameEngineRunning = false      // 任务引擎正在跑
    @State private var gamePauseSwitching = false     // ⏸ 切换中：转圈 + 挡连点
    /// 顶栏要不要露 ⏸：引擎在跑 / 剧情会话开着 / 已经急停着（得能解除）任一为真。
    private var gamePauseVisible: Bool { gameEngineRunning || gameSessionActive || gamePaused }
    /// 消息该改道 tmux 会话吗（code 和游戏剧情共用同一条管道）。
    private var sessionMode: Bool { codeMode || gameSessionActive }
    /// 气泡区此刻有多高。终端面板是**盖在**气泡区上的 overlay，高度以它为唯一上限——
    /// 所以面板绝不可能越过顶栏，键盘/附件条/输入框长高也都不用单独算：那些一动，
    /// 气泡区就变矮，这个值自己跟上。
    /// 量它不会成环，正因为面板是 overlay：面板高度影响不了气泡区的 frame。
    /// （压缩式布局那版量过一次，是正反馈震荡——终端变高 → 输入栏被压 → 量出更小的值
    /// → 终端算出自己还能更高 → 静止时黑条都会自己上下跳。overlay 把那条链断了。）
    @State private var chatAreaHeight: CGFloat = 0
    /// 终端面板此刻画出来多高（面板自己报上来）→ 转给 ChatView 当气泡的内容内边距，
    /// 最新气泡就正好停在黑条上边、不被盖住。
    @State private var terminalHeight: CGFloat = 0
    /// 终端当前档位。只用来判断「这次高度变化要不要让气泡跟着做动画」——
    /// 人主动换档才动画（气泡和面板一起走，不然气泡先跳、面板后滑，看着像自己滚了一下）；
    /// 弹窗选项进出导致的高度变化保持瞬时，别把整个气泡列表拖进 220 毫秒的重排。
    @State private var terminalRatio: CGFloat = 0

    // 编辑消息弹窗状态
    @State private var editingMessage: ChatMessage? = nil
    @State private var editingText: String = ""
    @State private var editRefreshTick = 0   // 亲手编辑/删除的信号：ChatView 收到就手术式合并进冻结快照
    @State private var backToNowTick = 0     // 「编辑并重新回复」的信号：ChatView 收到就解冻回底
    @State private var windowSyncTask: Task<Void, Never>? = nil   // 删/编辑后同步后端窗口（防抖）
    @State private var deleteCandidates: [ChatMessage] = []   // 长按气泡/堆叠卡 → 删除确认（组删多条）
    @State private var viewingWebpage: WebpageItem? = nil      // 点网页卡片 → 查看

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
        // 页面返回（栈清空）不重开抽屉——直接回聊天界面。
        // （原先按"聊天 → 抽屉 → 页面"逐层退设计，实际用起来是：从记忆页返回还得再关
        // 一次抽屉才看得到聊天。返回想去的地方就是聊天。）
        // 待送达同步：前台时拉一次，并每 15s 轮询（断连补投的回复靠这条通道回来）。
        // .task(id: scenePhase)：进 active 启动、离开 active 自动取消循环，省电。
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            await syncCodeMode()   // 他可能在断流/后台期间自己切进了 Code 模式 → 回前台对齐
            await syncPending()
            await reconcileRescues()
            await refreshDraftCount()
            await refreshGameStatus()
            while !Task.isCancelled {
                // 会话模式（code/游戏）下他说的每句话都靠这条通道回来 → 提到 3s；平时 15s 省电。
                try? await Task.sleep(for: .seconds(sessionMode ? 3 : 15))
                await syncPending()
                await reconcileRescues()
                await refreshDraftCount()
                await refreshGameStatus()
            }
        }
    }

    /// 抽屉项 → 页面（占位页随 PR2-5/PR8 逐个换真）。
    @ViewBuilder
    private func destination(for page: DrawerPage) -> some View {
        switch page {
        case .conversations:
            ConversationsPage(charStore: charListStore, chatStore: chatStore,
                              currentID: currentCharID,
                              switchDisabled: isGenerating) { c in
                switchCharacter(to: c.id)
                navPath.removeAll()
            }
        case .memory:
            MemoryPage()
        case .mind:
            MindPage()
        case .history:
            HistoryPage(messages: chatStore.messages, settingsStore: proactiveStore) { id in
                navPath.removeAll()
                // 等 pop 动画完、聊天列表回到台前再跳，不然滚动请求打在看不见的列表上
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                    chatScrollTarget = id
                }
            }
        case .drafts:
            DraftsPage()
        case .game:
            GamePage()
        case .plugins:
            PluginsPage()
        case .settings:
            ProactiveSettingsView(store: proactiveStore)
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
                DrawerPanel(agentName: topTitle, draftCount: draftCount) { page in
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
                 onDelete: { msg in deleteCandidates = [msg] },
                 onTapChatArea: { if showStickers { showStickers = false } },
                 onTapAvatar: { sender in dismissKeyboard(); avatarActionTarget = sender },
                 onTapImage: { url in dismissKeyboard(); enlargedImage = EnlargedImage(url: url) },
                 onTapImageStack: { urls, i in
                     dismissKeyboard(); enlargedImage = EnlargedImage(urls: urls, start: i)
                 },
                 onTapFile: { url in dismissKeyboard(); quickLookURL = AppFiles.reanchored(url) },
                 onTapWebpage: { pid, title in
                     dismissKeyboard(); viewingWebpage = WebpageItem(id: pid, title: title, ts: 0)
                 },
                 onDeleteStack: { msgs in deleteCandidates = msgs },
                 editRefreshTick: editRefreshTick,
                 backToNowTick: backToNowTick,
                 // 会话模式（code/游戏）：终端盖在气泡区上，气泡得留出这么高才不会被压在底下
                 bottomOverlayHeight: sessionMode ? terminalHeight : 0,
                 scrollTarget: chatScrollTarget,
                 onScrollTargetHandled: { chatScrollTarget = nil })
            .background(Color(.systemGroupedBackground))
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { chatAreaHeight = $0 }
            // Code 模式的终端：**盖在**气泡区上，不压缩它。压缩那版的代价见
            // CodeTerminalPanel 的开头注释——一句话：气泡区布局全程不动，面板才有
            // 一个诚实的高度上限，也才不会每次改高度都把整个气泡列表重排一遍。
            // ⚠️ 顺序要紧：overlay 挂在 safeAreaInset **之前**，它才对齐到「输入栏顶」
            // 而不是屏幕底。
            .overlay(alignment: .bottom) {
                // 游戏剧情会话共用这块终端（后端 /code/* 打的是「当前活着的会话」）
                if sessionMode {
                    CodeTerminalPanel(service: chatService, expanded: $terminalExpanded,
                                      available: chatAreaHeight)
                }
            }
            .onPreferenceChange(TerminalHeightKey.self) { terminalHeight = $0 }
            .onPreferenceChange(TerminalRatioKey.self) { terminalRatio = $0 }
            // 换档时气泡跟着面板一起动（同一条曲线、同一段时长）——不加这句气泡是瞬时
            // 跳到位、面板还在滑，看着就是「上面的气泡自己滚了一下」。
            // 挂在 ratio 上而不是 height 上：弹窗选项进出只改 height，那一路保持瞬时。
            .animation(.easeOut(duration: 0.22), value: terminalRatio)
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
                    if !pendingImages.isEmpty {
                        Divider()
                        PendingImagesBar(images: pendingImages) { idx in
                            pendingImages.remove(at: idx)
                        }
                    }
                    if !pendingFiles.isEmpty {
                        Divider()
                        PendingFilesBar(files: pendingFiles) { idx in
                            pendingFiles.remove(at: idx)
                        }
                    }
                    InputBar(text: $draft,
                             stickersActive: showStickers,
                             sending: isGenerating || isWaiting,
                             hasAttachments: pendingSticker != nil || !pendingImages.isEmpty
                                             || !pendingFiles.isEmpty,
                             onAttach: { dismissKeyboard(); showAttachMenu = true },
                             onStickers: toggleStickers,
                             onSend: send)
                }
                .animation(.easeInOut(duration: 0.2), value: showStickers)
                .animation(.easeInOut(duration: 0.2), value: pendingSticker)
                .animation(.easeInOut(duration: 0.2), value: pendingImages.count)
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
            // 长按气泡/堆叠卡 → 删除确认（堆叠卡删整组，弹窗写明张数）。
            .confirmationDialog(deleteCandidates.count > 1
                                    ? "删除这组 \(deleteCandidates.count) 张照片？"
                                    : "删除这条消息？",
                                isPresented: Binding(
                get: { !deleteCandidates.isEmpty },
                set: { if !$0 { deleteCandidates = [] } }
            ), titleVisibility: .visible) {
                Button("删除", role: .destructive) {
                    for msg in deleteCandidates { chatStore.remove(id: msg.id) }
                    editRefreshTick += 1   // 离底冻结快照就地撤行，不用滑回底部
                    scheduleWindowSync()   // 后端窗口跟上，别让 TA 醒来还看着删掉的那条
                }
                Button("取消", role: .cancel) { }
            }
            // 关会话（code/游戏共用）时那边正在干活：说清楚代价再让人按。
            .confirmationDialog(gameSessionActive ? "TA 正在游戏会话里" : "TA 正在电脑上干活",
                                isPresented: $confirmStopBusy, titleVisibility: .visible) {
                Button(gameSessionActive ? "收摊并关掉" : "停掉并退出", role: .destructive) {
                    Task { @MainActor in await exitSession() }
                }
                Button("先不关", role: .cancel) { }
            } message: {
                Text(gameSessionActive
                     ? "关掉会结束 TA 的游戏会话（模拟器不会自动关，TA 玩到一半的进度以游戏自己的存档为准）。"
                     : "退出会停掉那边正在跑的活，做到一半的东西不会有结果。")
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
            // ➕ 的选项弹层：照片 / 文件。
            .confirmationDialog("", isPresented: $showAttachMenu, titleVisibility: .hidden) {
                Button("照片") { showAttachPicker = true }
                Button("文件") { showFileImporter = true }
                Button("取消", role: .cancel) { }
            }
            // 选文件：PDF / 文本类（md/代码/txt）/ json / docx。读出数据暂存，≤10MB。
            .fileImporter(isPresented: $showFileImporter,
                          allowedContentTypes: Self.allowedFileTypes,
                          allowsMultipleSelection: true) { result in
                guard case .success(let urls) = result else { return }
                for url in urls {
                    let scoped = url.startAccessingSecurityScopedResource()
                    defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                    guard let data = try? Data(contentsOf: url) else { continue }
                    guard data.count <= 10 * 1024 * 1024 else {
                        errorText = "「\(url.lastPathComponent)」超过 10MB，先精简一下再发"
                        continue
                    }
                    pendingFiles.append(OutgoingFile(data: data, name: url.lastPathComponent,
                                                     mime: Self.mime(for: url)))
                }
            }
            // ➕ 选照片：多选，压到适合模型看的尺寸再暂存（原图几 MB 的 base64 没必要）。
            .photosPicker(isPresented: $showAttachPicker, selection: $attachPickerItems,
                          maxSelectionCount: 9, matching: .images)
            .onChange(of: attachPickerItems) { _, items in
                guard !items.isEmpty else { return }
                Task {
                    for item in items {
                        if let data = try? await item.loadTransferable(type: Data.self),
                           let img = UIImage(data: data),
                           let jpeg = Self.jpegForUpload(img) {
                            pendingImages.append(jpeg)
                        }
                    }
                    attachPickerItems = []
                }
            }
            .quickLookPreview($quickLookURL)
            // 点网页卡片 → 查看 TA 做的页面
            .sheet(item: $viewingWebpage) { p in WebpageViewer(id: p.id, title: p.title, asSheet: true) }
            // 全屏查看聊天图片；长按可删（删的是那条消息，本地历史移除）。
            .fullScreenCover(item: $enlargedImage) { item in
                ImageViewerView(urls: item.urls, start: item.start, onDeleteURL: { url in
                    if let msg = chatStore.messages.first(where: {
                        if case .image(let u) = $0.kind { return u == url }
                        return false
                    }) {
                        chatStore.remove(id: msg.id)
                        editRefreshTick += 1
                        scheduleWindowSync()
                    }
                }, onClose: { enlargedImage = nil })
            }
    }

    /// 顶栏标题 = 当前会话角色的名字（settings 是当前角色的；空时用角色清单兜底）。
    private var topTitle: String {
        if !proactiveStore.settings.agentName.isEmpty { return proactiveStore.settings.agentName }
        return charListStore.name(for: currentCharID) ?? "cassette"
    }

    /// 切到另一个角色的会话。流式生成中不切（NoSave 气泡没落盘）；调用方按钮已禁用，这里双保险。
    private func switchCharacter(to id: String) {
        guard id != currentCharID, !isGenerating else { return }
        chatStore.switchConversation(id)
        currentCharID = id
        profileStore.switchCharacter(id)
        sessionId = nil
        Task { await proactiveStore.reloadForCurrentCharacter() }
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
            // 左侧配平位：右边多出 ⏸ 时补一个等宽空位，标题才不偏
            if gamePauseVisible {
                Color.clear.frame(width: 40, height: 40)
            }
            Spacer()
            // 标题可点：进会话列表（多角色切换）。别的会话有未读时名字旁亮一个小圆点。
            Button {
                dismissKeyboard()
                navPath.append(DrawerPage.conversations)
            } label: {
                HStack(spacing: 5) {
                    Text(topTitle)
                        .font(.headline)
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if chatStore.otherUnreadTotal > 0 {
                        Circle().fill(Color.theme).frame(width: 7, height: 7)
                    }
                    Image(systemName: "chevron.down")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            // 右侧：游戏急停（引擎在跑/剧情会话开着/急停中才露）+ 会话开关
            // （code/游戏共用一颗：游戏会话活着时它是手柄样式，点了收摊——mianmian 同款）
            if gamePauseVisible {
                gamePauseToggle
            }
            if codeAvailable || gameSessionActive {
                codeToggle
            } else {
                Color.clear.frame(width: 40, height: 40)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
    }

    /// 游戏急停 ⏸：开着 = 实心底 + 白图标（和 codeToggle 同一套视觉语言）。
    /// 按下去是「叫停」不是「杀进程」：任务引擎跑完当前任务收手，剧情会话的操作工具被拒。
    private var gamePauseToggle: some View {
        Button(action: toggleGamePause) {
            Group {
                if gamePauseSwitching {
                    ProgressView().tint(gamePaused ? .white : Color.theme)
                } else {
                    Image(systemName: gamePaused ? "pause.circle.fill" : "pause.circle")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(gamePaused ? Color.white : Color.theme)
                }
            }
            .frame(width: 40, height: 30)
            .background(Capsule().fill(gamePaused ? Color.theme : Color.clear))
        }
        .disabled(gamePauseSwitching)
        .accessibilityLabel(gamePaused ? "游戏急停：开（点这里解除）" : "游戏急停")
    }

    private func toggleGamePause() {
        gamePauseSwitching = true
        Task { @MainActor in
            defer { gamePauseSwitching = false }
            do {
                try await chatService.setGamePaused(!gamePaused)
                await refreshGameStatus()
                chatStore.appendSystemMessage(gamePaused ? "游戏急停：已叫停" : "游戏急停：已解除")
            } catch {
                errorText = "急停没设上：\((error as? ChatServiceError)?.errorDescription ?? error.localizedDescription)"
            }
        }
    }

    /// 和后端对齐游戏状态（急停/引擎跑动）。失败静默——顶栏 ⏸ 靠下一拍补上。
    @MainActor
    private func refreshGameStatus() async {
        guard let st = try? await chatService.getGameStatus() else { return }
        gamePaused = st.paused
        gameEngineRunning = st.running
    }

    /// 会话开关（code/游戏共用）：切入=起 code 会话；亮着时点击=关掉当前会话。
    /// 游戏会话是 TA 自己切进去的（game_start），这颗按钮对它只有「关」这半边。
    /// 「电脑上的会话」这样东西全机只有一个，此刻归不归我这边的角色。
    /// 后端没下发归属（旧版）→ 空串 → 不设限。已经开着的会话不受这个管：
    /// 会话是我的时候得能点它退出，哪怕归属在会话开着之后被转走了。
    private var codeOwnedByMe: Bool {
        codeOwner.isEmpty || codeOwner == currentCharID || codeMode || gameSessionActive
    }

    private var codeToggle: some View {
        Button(action: toggleCodeMode) {
            Group {
                if codeSwitching {
                    ProgressView().tint(sessionMode ? .white : Color.theme)
                } else {
                    Image(systemName: gameSessionActive ? "gamecontroller.fill"
                          : "chevron.left.forwardslash.chevron.right")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(sessionMode ? Color.white : Color.theme)
                }
            }
            .frame(width: 40, height: 30)
            .background(Capsule().fill(sessionMode ? Color.theme : Color.clear))
            .opacity(codeOwnedByMe ? 1 : 0.35)
        }
        .disabled(codeSwitching || !codeOwnedByMe)
        .accessibilityLabel(gameSessionActive ? "游戏会话：开（点这里收摊）"
                            : codeMode ? "Code 模式：开（点这里退出）"
                            : codeOwnedByMe ? "切进 Code 模式"
                            : "电脑上的会话归\(codeOwnerName)，这边用不了")
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

    /// 切 Code 模式：入=带最近历史起会话；出=停会话。
    /// 失败时**不翻** codeMode——开关必须和后端会话的真实状态一致，翻了就会往一个不存在的
    /// 会话里发消息。
    private func toggleCodeMode() {
        dismissKeyboard()
        // 游戏会话活着：这一下是「收摊」。正玩着先问一句（和退 code 同款确认）。
        if gameSessionActive {
            codeSwitching = true
            Task { @MainActor in
                defer { codeSwitching = false }
                if let st = try? await chatService.codeStatus(probeBusy: true), st.busy == true {
                    confirmStopBusy = true
                    return
                }
                await exitSession()
            }
            return
        }
        codeSwitching = true
        Task { @MainActor in
            defer { codeSwitching = false }
            if codeMode {
                // 退出 = 杀掉 Mac 上那个会话。它要是正干着活，先问一句——不问的话
                // 一个跑了十分钟的活就这么没了。
                if let st = try? await chatService.codeStatus(probeBusy: true), st.busy == true {
                    confirmStopBusy = true
                    return
                }
                await exitSession()
            } else {
                do {
                    try await chatService.codeStart(history: chatStore.messages)
                    codeMode = true
                    chatStore.appendSystemMessage("已切进 Code 模式")
                } catch {
                    errorText = "切 Code 模式失败：\((error as? ChatServiceError)?.errorDescription ?? error.localizedDescription)"
                }
            }
        }
    }

    /// 真的关掉当前会话（code/游戏共用）：停掉 Mac 上那个 tmux 会话。
    @MainActor
    private func exitSession() async {
        let wasGame = gameSessionActive
        do {
            try await chatService.codeStop()
            codeMode = false
            gameSessionActive = false
            terminalExpanded = false
            chatStore.appendSystemMessage(wasGame ? "游戏会话关掉了" : "已退出 Code 模式")
        } catch {
            // 停不掉就别翻开关：翻了下一次回前台 syncCodeMode 又会按"会话还活着"
            // 给翻回来，还补一条系统灰字，人看着莫名其妙。
            errorText = "没能停掉会话：\((error as? ChatServiceError)?.errorDescription ?? error.localizedDescription)"
        }
    }

    /// 和后端对齐 Code 模式状态（会话活着 = 模式开着，这是唯一不变量）。
    /// 回前台时校准：他自己切进 Code 模式的那个 done 事件若在断流/后台期间丢了，靠这个补上。
    @MainActor
    private func syncCodeMode() async {
        guard let st = try? await chatService.codeStatus() else { return }
        codeAvailable = st.enabled && st.tmux
        // 会话是独占资源（全机就一个）：记下归谁，按钮据此禁用 + 说明为什么。
        // 旧后端不下发这俩字段 → 空串 = 不设限，老行为。
        codeOwner = st.owner ?? ""
        codeOwnerName = st.owner_name ?? ""
        // 活着的会话可能是游戏档案（TA 自己 game_start 切的）——那不是 Code 模式，
        // 别翻 codeMode、也别报「已切进 Code 模式」。终端面板走 gameSessionActive 亮起。
        let isGame = (st.profile ?? "code") == "game"
        // 别人的会话不该在我这边的聊天里翻开关：会话全机唯一，但它属于起它的那个角色，
        // 在别人的会话里亮起「已切进 Code 模式」等于把对方的活当成自己的。
        // 后端没给 session_char（旧版/手动开的会话）就不设限，保持老行为。
        let sc = st.session_char ?? ""
        let sessionMine = sc.isEmpty || sc == currentCharID
        let gameAlive = st.alive && isGame && sessionMine
        if gameAlive != gameSessionActive {
            gameSessionActive = gameAlive
            if gameAlive { chatStore.appendSystemMessage("去玩游戏了") }
            else { terminalExpanded = false }
        }
        let codeAlive = st.alive && !isGame && sessionMine
        guard st.enabled else {
            if codeMode { codeMode = false; terminalExpanded = false }
            return
        }
        guard codeAlive != codeMode else { return }
        codeMode = codeAlive
        if !codeAlive { terminalExpanded = false }
        if codeAlive { chatStore.appendSystemMessage("已切进 Code 模式") }
    }

    /// Code 模式的发送：文字 + 图片进会话，回复走待送达盒子回来。
    /// 不占 isWaiting/isGenerating——那边干一件活可能几分钟，气泡由轮询上屏，
    /// 期间输入框不该锁着（他在干活时人还想再补一句是常事）。
    private func sendToCode(_ trimmed: String) {
        var imagesData: [Data] = []
        var bubbleIds: [UUID] = []
        for data in pendingImages {
            if let url = AppFiles.saveChatImage(data) {
                let m = ChatMessage(sender: .me, kind: .image(url), timestamp: Date())
                chatStore.append(m)
                bubbleIds.append(m.id)
            }
            imagesData.append(data)
        }
        pendingImages = []
        // 文件：和聊天一样落盘上屏成卡片，数据随消息带过去（后端存进会话够得着的目录、给路径）
        let filesToSend = pendingFiles
        for f in pendingFiles {
            if let url = AppFiles.saveChatFile(f.data, name: f.name) {
                let m = ChatMessage(sender: .me, kind: .file(url, f.name), timestamp: Date())
                chatStore.append(m)
                bubbleIds.append(m.id)
            }
        }
        pendingFiles = []
        if !trimmed.isEmpty {
            let m = ChatMessage(sender: .me, kind: .text(trimmed), timestamp: Date())
            chatStore.append(m)
            bubbleIds.append(m.id)
        }
        draft = ""
        DispatchQueue.main.async { draft = "" }
        Task { @MainActor in
            do {
                try await chatService.codeSend(text: trimmed, imagesData: imagesData,
                                               filesData: filesToSend)
            } catch {
                // 没送进去（弹窗护栏挡下 / 会话没了）：撤掉这批气泡，别让它看着像发出去了；
                // 文字放回输入框，按掉弹窗直接重发（图得重选）。
                for id in bubbleIds { chatStore.remove(id: id) }
                editRefreshTick += 1
                draft = trimmed
                errorText = (error as? ChatServiceError)?.errorDescription ?? error.localizedDescription
            }
        }
    }

    private func send() {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !isGenerating,
              !trimmed.isEmpty || pendingSticker != nil
                || !pendingImages.isEmpty || !pendingFiles.isEmpty else { return }

        // 会话模式（code/游戏）：走 tmux 会话那条管道。表情没接——在那种会话里没什么
        // 意义，挡住说清楚就行。
        if sessionMode {
            if pendingSticker != nil {
                errorText = "会话模式发不了表情，等 TA 回聊天再发。"
                return
            }
            sendToCode(trimmed)
            return
        }

        isGenerating = true   // 同步占位：立刻禁用发送、挡住极快的重复触发
        isWaiting = true

        // 暂存的照片：先落盘成 .image 消息上屏；base64 数据随本轮请求带给后端（多模态）。
        let imagesToSend = pendingImages
        for data in pendingImages {
            if let url = AppFiles.saveChatImage(data) {
                chatStore.append(ChatMessage(sender: .me, kind: .image(url), timestamp: Date()))
            }
        }
        pendingImages = []

        // 暂存的文件：同款——落盘成 .file 消息上屏，数据随请求带给后端（document block）。
        let filesToSend = pendingFiles
        for f in pendingFiles {
            if let url = AppFiles.saveChatFile(f.data, name: f.name) {
                chatStore.append(ChatMessage(sender: .me, kind: .file(url, f.name), timestamp: Date()))
            }
        }
        pendingFiles = []

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

        Task { await generateReply(imagesData: imagesToSend, filesData: filesToSend) }
    }

    /// 文件选择器放行的类型 + 各自的 MIME（后端按这个决定 PDF 直喂 / 文本读 / docx 抽正文）。
    private static let allowedFileTypes: [UTType] = {
        var types: [UTType] = [.pdf, .text, .json]
        if let docx = UTType("org.openxmlformats.wordprocessingml.document") { types.append(docx) }
        return types
    }()

    private static func mime(for url: URL) -> String {
        switch url.pathExtension.lowercased() {
        case "pdf":  return "application/pdf"
        case "docx": return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        case "json": return "application/json"
        default:     return "text/plain"   // md/txt/代码：后端按文本原文读
        }
    }

    /// 相册原图压到适合模型看的尺寸（长边 ≤1568，jpeg 0.8）——多模态按 token 算钱，
    /// 原图几 MB 的 base64 纯浪费；本地气泡渲染用的也是这份，清晰度够。
    private static func jpegForUpload(_ img: UIImage, maxDim: CGFloat = 1568) -> Data? {
        let longer = max(img.size.width, img.size.height)
        guard longer > maxDim else { return img.jpegData(compressionQuality: 0.8) }
        let scale = maxDim / longer
        let newSize = CGSize(width: img.size.width * scale, height: img.size.height * scale)
        let fmt = UIGraphicsImageRendererFormat.default()
        fmt.scale = 1
        let resized = UIGraphicsImageRenderer(size: newSize, format: fmt).image { _ in
            img.draw(in: CGRect(origin: .zero, size: newSize))
        }
        return resized.jpegData(compressionQuality: 0.8)
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil
        )
    }

    /// 把当前完整历史发给后端（流式），回复逐字上屏；失败则弹提示。
    /// 约定调用前 chatStore.messages 已以用户的新消息结尾。
    @MainActor
    private func generateReply(imagesData: [Data] = [], filesData: [OutgoingFile] = []) async {
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
        // inFlight=流还开着：这条登记先不亮三个点（那是 isWaiting 的活）、也不参与对账判死。
        rescueWaiting[reqId] = RescueWait(ids: [], since: Date(), inFlight: true)
        do {
            let stream = chatService.sendStream(history: chatStore.messages,
                                                sessionId: sessionId,
                                                stickers: stickerStore.stickers, reqId: reqId,
                                                imagesData: imagesData, filesData: filesData)
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
                case .memory(let tool, let text, let ok, let reason):
                    // 中途工具操作 → 就地内联灰字（成功的网页除外：finalize 会补一张可点的卡片）
                    if !ok {
                        chatStore.appendMemoryNote(memoryFailNoteText(tool: tool, reason: reason))
                    } else if tool == "gametask" {
                        // 派引擎跑日常：灰字带上任务清单（text），机主一眼能核对派了什么
                        chatStore.appendMemoryNote("派引擎去跑日常：\(text)")
                    } else if tool != "webpage" {
                        chatStore.appendMemoryNote(memoryNoteText(tool: tool))
                    }
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
            if sawDone {
                rescueWaiting.removeValue(forKey: reqId)
            } else {
                // 流断没 done：登记留着等补投。流关了 → inFlight 落下，从这一刻起
                // 三个点由它维持、对账也开始管它。
                rescueWaiting[reqId]?.ids = turnIds
                rescueWaiting[reqId]?.inFlight = false
            }
        } catch {
            if let id = streamingId {
                if streamedText.isEmpty { chatStore.remove(id: id); turnIds.removeAll { $0 == id } }
                else { chatStore.editText(id: id, newText: streamedText) }
            }
            // 中途断连（切后台/锁屏被掐流）不弹错：后端照跑，补投机制会把完整回复送回来，
            // 弹窗纯属误报。其余错误（连不上/4xx…）是明确失败：弹提示 + 删登记，不再空等。
            if case ChatServiceError.connectionLost = error {
                // 静默：半截保留，登记等补投（哪怕一段正文都没冒——不然三个点一灭就死寂）。
                rescueWaiting[reqId]?.ids = turnIds
                rescueWaiting[reqId]?.inFlight = false
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
        // 他这轮自己切进了 Code 模式（调了 code_start 工具）→ 翻开关，后续消息改道会话。
        if resp.code_started == true, !codeMode {
            codeMode = true
            codeAvailable = true
            chatStore.appendSystemMessage("已切进 Code 模式")
        }
        // 他这轮自己切去玩游戏了（调了 game_start）→ 终端面板亮起，后续消息改道会话。
        if resp.game_started == true, !gameSessionActive {
            gameSessionActive = true
            chatStore.appendSystemMessage("去玩游戏了")
        }
        // 他这轮做/改的网页 → 网页卡片消息（stored 只有标题，从后端反查 id）。
        // 只认真做成了的（ok=false 的那次页面根本没生成，反查 id 只会挂错一张卡片）。
        appendDoneOnlyStored(resp.stored)
    }

    /// done 独有的 stored 产物上屏：网页卡片 + 浏览灰字（后端已聚合成一条，text=网址列表）。
    /// 流式收尾和断连补投两条路共用——这俩只随 done 走，断连轮 app 没见过 done，得从补投
    /// 条目里补（实锤：08-10 二轮测试浏览灰字蒸发）。记忆灰字不在此列：流式中途就地发过。
    /// 同秒+同内容去重：ack 失败重拉 pending 时别插重复（finalize 路 timestamp=now，不会撞）。
    @MainActor
    private func appendDoneOnlyStored(_ stored: [StoredMemory]?, timestamp: Date = Date()) {
        let ts = Int(timestamp.timeIntervalSince1970)
        let pages = (stored ?? []).filter { $0.tool == "webpage" && $0.ok != false }
        if !pages.isEmpty {
            Task { @MainActor in
                guard let list = try? await chatService.getWebpages() else { return }
                for p in pages {
                    if let item = list.first(where: { $0.title == p.text }) ?? list.first {
                        let dup = chatStore.messages.contains { m in
                            guard Int(m.timestamp.timeIntervalSince1970) == ts,
                                  case .webpage(let pid, _) = m.kind else { return false }
                            return pid == item.id
                        }
                        if !dup {
                            chatStore.append(ChatMessage(sender: .other,
                                                         kind: .webpage(item.id, item.title),
                                                         timestamp: timestamp))
                        }
                    }
                }
            }
        }
        for b in (stored ?? []) where b.tool == "browse" && b.ok != false {
            let urls = b.text.split(separator: "\n").map(String.init)
                .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            guard !urls.isEmpty else { continue }
            let dup = chatStore.messages.contains { m in
                guard Int(m.timestamp.timeIntervalSince1970) == ts,
                      case .browseNote(let u) = m.kind else { return false }
                return u == urls
            }
            if !dup {
                chatStore.append(ChatMessage(sender: .other, kind: .browseNote(urls),
                                             timestamp: timestamp))
            }
        }
    }

    /// 一次工具产物的灰字文案（内容去记忆页/聊天记录页看，这里只标动作）。
    private func memoryNoteText(tool: String) -> String {
        switch tool {
        case "feel":    return "记下了一份心情"
        case "trace":   return "调整了一条记忆"
        case "i":       return "记下了一个关于自己的念头"
        case "webpage": return "做了一个网页"
        case "mail":    return "寄出了一封邮件"
        case "mail_draft": return "写了封信放进草稿信箱，等你过目"
        case "gametask": return "派引擎去跑游戏日常了"
        default:        return "记住了一件事"
        }
    }

    /// 想做但没做成的那次（工具报错/被婉拒）。以前这种也显示成「记住了一件事」——
    /// 灰字在骗人，记忆其实没落盘。带上原因，TA 下次自己就知道该补什么。
    private func memoryFailNoteText(tool: String, reason: String) -> String {
        let what: String
        switch tool {
        case "feel":    what = "想记下一份心情"
        case "trace":   what = "想调整一条记忆"
        case "i":       what = "想记下一个关于自己的念头"
        case "webpage": what = "想做一个网页"
        case "codemode": what = "想切去 Code 模式"
        case "gamemode": what = "想切去玩游戏"
        case "gametask": what = "想派引擎跑游戏日常"
        case "mail", "mail_draft": what = "想寄一封邮件"
        default:        what = "想记住一件事"
        }
        let why = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        return why.isEmpty ? "\(what)，但没成" : "\(what)，但没成：\(why)"
    }

    /// 草稿信箱待寄数（抽屉角标）。失败静默清零——角标是提示不是真相，
    /// 后端不在时不该拿旧数字亮着。
    @MainActor
    private func refreshDraftCount() async {
        let wrap = try? await chatService.getMailDrafts()
        draftCount = (wrap?.plugin_installed ?? false) ? (wrap?.items.count ?? 0) : 0
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
                // 多角色路由：别的角色的消息直接写进对方会话文件 + 未读 +1，不混进当前聊天。
                // 对方的 error/rescue 记账（rescueWaiting）都是当前会话的 UI 概念，不适用——跳过。
                let conv = p.char_id ?? "default"
                if conv != currentCharID {
                    if p.error != true {
                        if !p.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            chatStore.insertProactive(text: p.text, timestamp: ts,
                                                      conversation: conv)
                        }
                        for sid in p.sticker_ids ?? [] {
                            if let st = stickerStore.sticker(id: sid) {
                                chatStore.insertProactiveSticker(
                                    url: stickerStore.imageURL(for: st),
                                    description: st.description,
                                    timestamp: ts, conversation: conv)
                            }
                        }
                    }
                    continue
                }
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
                // 断连补投轮的 done 独有产物（browse 灰字/网页卡片）：从条目里补渲染。
                appendDoneOnlyStored(p.stored, timestamp: ts)
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
            // 绝对清理线：后端 CLAUDE_TIMEOUT+守护60s+余量。流还开着的不算僵尸——
            // 超长的一轮（那边干半小时的活）把登记清了，流真断的时候就没人接补投了。
            if !wait.inFlight, now.timeIntervalSince(wait.since) > 1920 {
                rescueWaiting.removeValue(forKey: rid)
                continue
            }
            // inFlight（流还开着、app 就在收正文）不参与判死：这轮的死活由流自己说了算，
            // 让对账插一脚会把"后端还没登记进 _ACTIVE_REQS 的在途请求"误判成丢了。
            if !wait.inFlight, !wait.givenUp, !active.contains(rid),
               now.timeIntervalSince(wait.since) > 60 {
                rescueWaiting[rid]?.givenUp = true
                chatStore.appendSystemMessage("刚才那条可能没送到后端，重发试试")
            }
        }
    }

    // MARK: - 窗口同步（删/编辑消息后）

    /// 删/编辑消息是纯本地操作，不触发任何后端请求——不同步的话，在下次发消息之前，
    /// TA 每次醒来看到的都还是删改之前的世界，和眠眠的视角对不上。
    /// 1 秒防抖：连删几条（或删整组照片）只推最后那一份。
    /// **不用等它**：推失败也只是窗口略旧，下次发消息 /chat 会整体覆盖，没有需要提示的事。
    private func scheduleWindowSync() {
        windowSyncTask?.cancel()
        windowSyncTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled else { return }
            try? await chatService.syncWindow(history: chatStore.messages)
        }
    }

    // MARK: - 编辑 / 重新生成

    /// gobackward：打开编辑弹窗（弹窗里 Edit / Regenerate 按钮按发送者不同）。
    /// Code 模式下整个禁掉：那边的操作有真实副作用（改了文件、跑过命令），
    /// 重放一遍历史等于再干一次。
    private func startEdit(_ message: ChatMessage) {
        guard !sessionMode else {
            errorText = "会话模式（Code/游戏）下不能编辑/重新生成（那边的操作有真实副作用），等会话结束再用。"
            return
        }
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
            // 附件找回（mianmian 实踩 bug）：图/文件只在原发送轮注入，历史里只剩
            // [图片]/[文件:名] 占位——直接重答模型就看不到了。从这条往前收集紧邻的
            // 同回合附件（发送时图/文件都排在文字前面），从沙盒把数据重建出来随重发带上。
            let (imagesData, filesData) = collectAdjacentAttachments(before: message.id)
            chatStore.truncateAfter(id: message.id)   // 删掉这条之后的旧对话
            backToNowTick += 1                        // 回底，等着看重答的那条
            Task { await generateReply(imagesData: imagesData, filesData: filesData) }
        } else {
            // 「仅修改」没有后续请求，窗口得自己去对齐；「编辑并重新回复」不用管——
            // 后面紧跟的 /chat 会整体覆盖窗口。
            scheduleWindowSync()
        }
    }

    /// 从某条消息往前收集紧邻的自己发的图/文件（同回合附件），重建成可发送的数据。
    private func collectAdjacentAttachments(before id: UUID) -> ([Data], [OutgoingFile]) {
        var imagesData: [Data] = []
        var filesData: [OutgoingFile] = []
        guard let idx = chatStore.messages.firstIndex(where: { $0.id == id }) else {
            return ([], [])
        }
        var i = idx - 1
        walk: while i >= 0 {
            let m = chatStore.messages[i]
            guard m.sender == .me else { break }
            switch m.kind {
            case .image(let url):
                if let data = try? Data(contentsOf: AppFiles.reanchored(url)) {
                    imagesData.insert(data, at: 0)   // 存的就是发送时的 jpeg，原样回带
                }
            case .file(let url, let name):
                if let data = try? Data(contentsOf: AppFiles.reanchored(url)) {
                    filesData.insert(OutgoingFile(data: data, name: name,
                                                  mime: Self.mime(for: URL(fileURLWithPath: name))), at: 0)
                }
            default:
                break walk
            }
            i -= 1
        }
        return (imagesData, filesData)
    }
}

#Preview {
    ContentView()
}
