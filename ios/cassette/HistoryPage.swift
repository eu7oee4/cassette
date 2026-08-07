import SwiftUI
import WebKit

/// 聊天记录页（抽屉 → push）：搜索 + 倒数序号 + 日期直达 + 代码块检索 + 注入窗口控制。
/// 序号的意义：历史是按"最近 N 条"注入给模型的——在这里定位到"倒数第 N 条"，
/// 就能把窗口临时扩到 N 让 TA 补看/补存，用完再改回去。
/// 行紧凑等高——等高是右缘快速滚动把手稳定的地基。
/// （图片/文件/HTML 检索入口等对应能力落地后再加：PR6/PR7/PR9。）
struct HistoryPage: View {
    let messages: [ChatMessage]
    @ObservedObject var settingsStore: ProactiveSettingsStore
    /// 点行 → 关掉本页、聊天界面跳到那条气泡（跳转由 ContentView/ChatView 完成）。
    var onJump: (UUID) -> Void = { _ in }
    @AppStorage("sendHistoryCap") private var injectCap = ChatService.defaultHistoryCap

    @State private var query = ""
    @State private var selected: Entry? = nil        // 点开看全文的那条
    @State private var showDates = false             // 日期直达 sheet
    @State private var showCodeList = false          // 带代码块的消息
    @State private var showJump = false              // #序号直达输入
    @State private var jumpText = ""
    // 快速滚动把手状态
    @State private var scrubbing = false
    @State private var scrubEntry: Entry? = nil      // 把手当前指到的行（浮动气泡显示）
    /// 关掉 sheet 后待执行的跳转目标（listArea 的 onChange 消费）。
    @State private var pendingScrollTarget: UUID? = nil

    /// 一行 = 一条可注入的消息（memoryNote 灰字、system 提示都是纯 UI、不发后端，排除——
    /// 过滤口径必须和 ChatService.buildChatRequest 一致，序号才和注入窗口对齐）。
    struct Entry: Identifiable {
        let n: Int                // 倒数序号：1 = 最新
        let msg: ChatMessage
        var id: UUID { msg.id }
    }

    /// 全量条目，最新在最上（倒数#1 在顶部）。
    private var entries: [Entry] {
        let sendable = messages.filter { !$0.isMemoryNote && !$0.isSystem }
        return sendable.reversed().enumerated().map { Entry(n: $0.offset + 1, msg: $0.element) }
    }

    private var filtered: [Entry] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return entries }
        return entries.filter { $0.msg.plainText.localizedCaseInsensitiveContains(q) }
    }

    var body: some View {
        VStack(spacing: 0) {
            searchRow
            quickActions
            capBar
            Divider()
            listArea
        }
        // 点空白处收键盘（搜索框+两个窗口输入框）。内层按钮/行/输入框自己的手势优先。
        .contentShape(Rectangle())
        .onTapGesture { dismissKeyboards() }
        .navigationTitle("聊天记录")
        .navigationBarTitleDisplayMode(.inline)
        // 五个检索入口都是整屏 push 页（左上标准返回）；查看全文留小弹层。
        .navigationDestination(isPresented: $showDates) { dateSheet }
        .navigationDestination(isPresented: $showCodeList) { codeListSheet }
        .navigationDestination(isPresented: $showImageGrid) { imageGridSheet }
        .navigationDestination(isPresented: $showFileList) { fileListSheet }
        .navigationDestination(isPresented: $showWebpages) { WebpagesPage() }
        .sheet(item: $selected) { e in EntryDetail(entry: e, injectCap: $injectCap) }
        .alert("跳到倒数第几条？", isPresented: $showJump) {
            TextField("如 137", text: $jumpText)
                .keyboardType(.numberPad)
            Button("跳转") { jumpToNumber() }
            Button("取消", role: .cancel) { }
        }
    }

    /// 搜索行：普通输入框（不用 .searchable——它聚焦时会收起大标题、整页上移，真机反馈晕）。
    private var searchRow: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.footnote)
                .foregroundStyle(.secondary)
            TextField("按聊天记录搜索", text: $query)
                .textFieldStyle(.plain)
                .submitLabel(.search)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(Color(.tertiarySystemFill), in: Capsule())
        .padding(.horizontal, 12)
        .padding(.top, 8)
    }

    // MARK: - 快捷检索行 + 窗口设置行（设计规则：图标非按钮不加，文案直给）

    @State private var showImageGrid = false         // 图片九宫格
    @State private var showFileList = false          // 发过的文件
    @State private var showWebpages = false          // TA 做的 HTML 网页
    @State private var gridViewer: EnlargedImage? = nil

    private var quickActions: some View {
        HStack(spacing: 14) {
            Button("日期检索") { showDates = true }
            Button("倒序号检索") { jumpText = ""; showJump = true }
            Button("HTML文件") { showWebpages = true }
            Button("代码块") { showCodeList = true }
            Button("图片") { showImageGrid = true }
            Button("文件") { showFileList = true }
            Spacer()
        }
        .font(.subheadline)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.top, 6)
    }

    @State private var capText = ""
    @FocusState private var capFocused: Bool
    // wake 窗口：值在后端 settings（wake_window_n），经共享 store 读写
    @State private var wakeCapText = ""
    @FocusState private var wakeCapFocused: Bool

    private var wakeCap: Int { settingsStore.settings.wakeWindowN ?? 50 }

    private var capBar: some View {
        VStack(alignment: .leading, spacing: 3) {
            capRow(label: "chat 注入窗口", text: $capText, focus: $capFocused,
                   over: injectCap > 150, range: "可设 20~1000") { commitCap() }
            capRow(label: "wake 注入窗口", text: $wakeCapText, focus: $wakeCapFocused,
                   over: wakeCap > 100, range: "可设 20~300") { commitWakeCap() }
            Text("点击消息定位到气泡，长按查看全文 / 把窗口扩到此条")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.top, 2)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .onAppear {
            capText = "\(injectCap)"
            wakeCapText = "\(wakeCap)"   // 先用共享 store 里的已知值即时显示，别等网络
        }
        .onChange(of: injectCap) { _, v in
            if !capFocused { capText = "\(v)" }
        }
        .task {
            // 后台对齐服务器值（比如别的设备改过）；正在打字就不覆盖
            await settingsStore.refreshFromServer()
            if !wakeCapFocused { wakeCapText = "\(wakeCap)" }
        }
    }

    private func capRow(label: String, text: Binding<String>,
                        focus: FocusState<Bool>.Binding, over: Bool, range: String,
                        commit: @escaping () -> Void) -> some View {
        HStack(spacing: 6) {
            Text(label)
                .font(.subheadline.weight(.medium))
            TextField("", text: text)
                .keyboardType(.numberPad)
                .focused(focus)
                .multilineTextAlignment(.center)
                .font(.subheadline.monospacedDigit().bold())
                .frame(width: 56)
                .padding(.vertical, 4)
                .background(over ? Color.theme : Color(.systemGray5),
                            in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .foregroundStyle(over ? .white : .primary)
                .onSubmit { commit() }
                .onChange(of: focus.wrappedValue) { _, focused in
                    if !focused { commit() }
                }
            Text("条")
                .font(.subheadline.weight(.medium))
            Text(range)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Spacer()
        }
    }

    /// chat 窗口提交：夹取 20~1000 落 UserDefaults；非法输入回显当前值。
    private func commitCap() {
        if let n = Int(capText.trimmingCharacters(in: .whitespaces)) {
            injectCap = min(max(n, 20), 1000)
        }
        capText = "\(injectCap)"
    }

    /// wake 窗口提交：夹取 20~300，写进共享 settings 并推后端。
    private func commitWakeCap() {
        guard let n = Int(wakeCapText.trimmingCharacters(in: .whitespaces)) else {
            wakeCapText = "\(wakeCap)"
            return
        }
        let clamped = min(max(n, 20), 300)
        if clamped != wakeCap {
            settingsStore.settings.wakeWindowN = clamped
            Task { await settingsStore.pushToServer() }
        }
        wakeCapText = "\(clamped)"
    }

    // MARK: - 列表 + 快速滚动把手

    private func dismissKeyboards() {
        capFocused = false
        wakeCapFocused = false
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }

    private var listArea: some View {
        ScrollViewReader { proxy in
            ZStack(alignment: .trailing) {
                ScrollView {
                    LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
                        ForEach(sections, id: \.day) { sec in
                            Section {
                                ForEach(sec.items) { e in
                                    EntryRow(entry: e)
                                        .id(e.id)
                                        .onTapGesture { onJump(e.id) }   // 点行=跳到聊天里那条气泡
                                        .contextMenu {                    // 长按=全文/扩窗
                                            Button {
                                                selected = e
                                            } label: {
                                                Label("查看全文", systemImage: "doc.text.magnifyingglass")
                                            }
                                            Button {
                                                injectCap = min(max(e.n, 20), 1000)
                                            } label: {
                                                Label("注入窗口扩到这条（#\(e.n)）",
                                                      systemImage: "square.stack.3d.up")
                                            }
                                        }
                                    Divider().padding(.leading, 58)
                                }
                            } header: {
                                Text(sec.day)
                                    .font(.caption.bold())
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.horizontal, 14)
                                    .padding(.vertical, 4)
                                    .background(.bar)
                            }
                        }
                    }
                }
                .scrollDismissesKeyboard(.immediately)   // 一滑列表就收键盘
                scrubberRail(proxy: proxy)
                // 把手浮动气泡：日期 + 倒数序号，两个坐标一起给
                if scrubbing, let e = scrubEntry {
                    HStack(spacing: 6) {
                        Text(Self.dayFmt.string(from: e.msg.timestamp))
                        Text("倒数#\(e.n)").bold()
                    }
                    .font(.footnote)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(Capsule().fill(.ultraThickMaterial))
                    .shadow(radius: 3)
                    .padding(.trailing, 40)
                }
            }
            // 日期直达 / #序号直达 的跳转在这里消费（sheet/alert 关闭后列表回来才滚得动）
            .onChange(of: pendingScrollTarget) { _, target in
                guard let target else { return }
                withAnimation { proxy.scrollTo(target, anchor: .top) }
                pendingScrollTarget = nil
            }
        }
    }

    /// 右缘快速滚动轨道：拖动按比例映射到条目位置，scrollTo(id) 跳行（等高行→比例即位置）。
    private func scrubberRail(proxy: ScrollViewProxy) -> some View {
        GeometryReader { geo in
            HStack {
                Spacer()
                Capsule()
                    .fill(Color(.systemGray4).opacity(scrubbing ? 0.9 : 0.5))
                    .frame(width: scrubbing ? 8 : 4)
                    .padding(.vertical, 20)
                    .padding(.trailing, 4)
                    .contentShape(Rectangle().inset(by: -14))   // 加宽热区好按
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { v in
                                scrubbing = true
                                let list = filtered
                                guard !list.isEmpty else { return }
                                let frac = min(max(v.location.y / max(geo.size.height, 1), 0), 1)
                                let idx = min(Int(frac * CGFloat(list.count)), list.count - 1)
                                let e = list[idx]
                                scrubEntry = e
                                proxy.scrollTo(e.id, anchor: .top)   // 无动画，跟手
                            }
                            .onEnded { _ in
                                scrubbing = false
                                scrubEntry = nil
                            }
                    )
            }
        }
    }

    // MARK: - 日期直达

    private struct DaySection {
        let day: String
        let items: [Entry]
    }

    /// 按天分组（列表顺序：新→旧，组内亦然）。搜索时照样分组，行为一致。
    private var sections: [DaySection] {
        var out: [DaySection] = []
        for e in filtered {
            let d = Self.dayFmt.string(from: e.msg.timestamp)
            if out.last?.day == d {
                out[out.count - 1] = DaySection(day: d, items: out[out.count - 1].items + [e])
            } else {
                out.append(DaySection(day: d, items: [e]))
            }
        }
        return out
    }

    private var dateSheet: some View {
        List(sections, id: \.day) { sec in
                Button {
                    showDates = false
                    // 关 sheet 后跳转要等列表回来一拍
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                        pendingScrollTarget = sec.items.first?.id
                    }
                } label: {
                    HStack {
                        Text(sec.day)
                            .foregroundStyle(.primary)   // 别用 Button 默认的主题色染日期
                        Spacer()
                        Text("\(sec.items.count) 条 · 倒数#\(sec.items.first?.n ?? 0)~#\(sec.items.last?.n ?? 0)")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        .navigationTitle("日期检索")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - 代码块检索

    private var codeEntries: [Entry] {
        entries.filter { e in
            if case .text(let t) = e.msg.kind { return t.contains("```") }
            return false
        }
    }

    private var codeListSheet: some View {
        ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(codeEntries) { e in
                        EntryRow(entry: e, showDate: true)   // 无分组头 → 行内带全日期
                            .onTapGesture { showCodeList = false; onJump(e.id) }
                        Divider().padding(.leading, 58)
                    }
                    if codeEntries.isEmpty {
                        Text("还没有带代码块的消息")
                            .font(.footnote).foregroundStyle(.secondary).padding(.top, 40)
                    }
                }
            }
        .scrollDismissesKeyboard(.immediately)
        .navigationTitle("代码块")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - 图片检索（按月九宫格）

    /// 全部图片（新→旧）+ 全局序号（点开查看器从这张起）。
    private var imageEntries: [(entry: Entry, url: URL)] {
        entries.compactMap { e in
            if case .image(let u) = e.msg.kind { return (e, u) }
            return nil
        }
    }

    private struct MonthGroup {
        let month: String
        let items: [(index: Int, url: URL)]   // index=全局序号
    }

    /// 按月分组（最近的月在最上；组内也是新→旧）。
    private var imageMonths: [MonthGroup] {
        var out: [MonthGroup] = []
        for (i, item) in imageEntries.enumerated() {
            let m = Self.monthFmt.string(from: item.entry.msg.timestamp)
            if out.last?.month == m {
                out[out.count - 1] = MonthGroup(month: m, items: out[out.count - 1].items + [(i, item.url)])
            } else {
                out.append(MonthGroup(month: m, items: [(i, item.url)]))
            }
        }
        return out
    }

    private var imageGridSheet: some View {
        ScrollView {
                let allUrls = imageEntries.map(\.url)
                LazyVStack(alignment: .leading, spacing: 8, pinnedViews: [.sectionHeaders]) {
                    ForEach(imageMonths, id: \.month) { group in
                        Section {
                            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2),
                                                     count: 4), spacing: 2) {
                                ForEach(group.items, id: \.index) { item in
                                    // 方形格标准写法：透明方容器定尺寸，图 overlay 填充后裁切
                                    Color.clear
                                        .aspectRatio(1, contentMode: .fit)
                                        .overlay {
                                            if let img = AppFiles.loadImage(item.url) {
                                                Image(uiImage: img).resizable().scaledToFill()
                                            } else {
                                                Color(.systemGray5)
                                            }
                                        }
                                        .clipped()
                                        .contentShape(Rectangle())
                                        .onTapGesture {
                                            gridViewer = EnlargedImage(urls: allUrls, start: item.index)
                                        }
                                }
                            }
                        } header: {
                            Text(group.month)
                                .font(.subheadline.bold())
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(.bar)
                        }
                    }
                }
                if imageEntries.isEmpty {
                    Text("还没有图片")
                        .font(.footnote).foregroundStyle(.secondary).padding(.top, 40)
                }
            }
        .navigationTitle("图片")
        .navigationBarTitleDisplayMode(.inline)
        .fullScreenCover(item: $gridViewer) { item in
            ImageViewerView(urls: item.urls, start: item.start) { gridViewer = nil }
        }
    }

    static let monthFmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy年M月"
        f.locale = Locale(identifier: "zh_CN")
        return f
    }()

    // MARK: - 文件检索

    private var fileEntries: [Entry] {
        entries.filter { e in
            if case .file = e.msg.kind { return true }
            return false
        }
    }

    private var fileListSheet: some View {
        ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(fileEntries) { e in
                        EntryRow(entry: e, showDate: true)
                            .onTapGesture { showFileList = false; onJump(e.id) }
                        Divider().padding(.leading, 58)
                    }
                    if fileEntries.isEmpty {
                        Text("还没有发过文件")
                            .font(.footnote).foregroundStyle(.secondary).padding(.top, 40)
                    }
                }
            }
        .scrollDismissesKeyboard(.immediately)
        .navigationTitle("文件")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - 序号直达

    private func jumpToNumber() {
        guard let n = Int(jumpText), n >= 1 else { return }
        // 序号在全量 entries 里找（搜索过滤不影响序号定义）
        if let e = entries.first(where: { $0.n == n }) {
            query = ""                       // 清搜索，保证目标行在列表里
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                pendingScrollTarget = e.id
            }
        }
    }

    static let dayFmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy年M月d日 EEE"   // 带年份，跨年不迷路
        f.locale = Locale(identifier: "zh_CN")
        return f
    }()

    static let timeFmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    static let fullFmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm"
        return f
    }()
}

// MARK: - 行 & 详情

/// 紧凑等高行：#序号 · 时间 · 发送者色点 · 单行摘要。showDate=行内带全日期（无分组头的列表用）。
private struct EntryRow: View {
    let entry: HistoryPage.Entry
    var showDate = false

    var body: some View {
        HStack(spacing: 8) {
            Text("#\(entry.n)")
                .font(.caption.monospacedDigit().bold())
                .foregroundStyle(.secondary)
                .frame(width: 50, alignment: .trailing)
            Circle()
                .fill(entry.msg.sender == .me ? Color.theme : Color(.systemGray3))
                .frame(width: 7, height: 7)
            Text((showDate ? HistoryPage.fullFmt : HistoryPage.timeFmt)
                    .string(from: entry.msg.timestamp))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            Text(entry.msg.plainText.replacingOccurrences(of: "\n", with: " "))
                .font(.subheadline)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .frame(height: 44)                    // 等高：快速跳转的地基
        .contentShape(Rectangle())
    }
}

/// 点开一行：全文 + 「窗口扩到这条」。
private struct EntryDetail: View {
    let entry: HistoryPage.Entry
    @Binding var injectCap: Int
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("倒数#\(entry.n)").font(.headline)
                        Spacer()
                        Text(HistoryPage.dayFmt.string(from: entry.msg.timestamp) + " " +
                             HistoryPage.timeFmt.string(from: entry.msg.timestamp))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text(entry.msg.plainText)
                        .font(.body)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding()
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                }
            }
            .safeAreaInset(edge: .bottom) {
                VStack(spacing: 6) {
                    Button {
                        injectCap = min(max(entry.n, 20), 1000)
                        dismiss()
                    } label: {
                        Text("注入窗口扩到这条（含）→ \(entry.n) 条")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.theme)
                    Text("窗口按你的聊天频率和 token 额度自由定，100 只是默认值")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                .padding()
                .background(.bar)
            }
        }
        .presentationDetents([.medium, .large])
    }
}
