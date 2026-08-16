import SwiftUI

/// 房间视图（PLAN_cohabit C3）。两种打开方式：
/// - 去这里（peek=false）：真实在场——事件流 = 本次在场区间（scope=visible），
///   底部输入区与 AI 的 act 对称（*动作* + 说话，可一空），显式「离开」按钮（回走廊）。
/// - 偷看一眼（peek=true）：玩家的上帝视角——全部历史（scope=all）、只读、不产生事件。
struct RoomPage: View {
    let roomID: String
    let title: String
    let peek: Bool

    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @EnvironmentObject private var profileStore: ProfileStore

    @State private var detail: RoomDetail?
    @State private var events: [RoomEvent] = []
    @State private var inputText = ""     // 混写：*星号* 是动作、其余是说话，可交错
    @State private var sending = false
    @State private var errorText: String?
    @State private var editingEntry: RoomStateEntry?   // 点了哪条地点状态（弹编辑/清掉）
    @State private var stateDraft = ""                 // add/edit 的草稿
    @State private var stateSheet: StateSheetMode?
    @FocusState private var inputFocused: Bool         // 输入区聚焦（收键盘/自动触底用）

    private let service = ChatService()

    private enum StateSheetMode: Identifiable {
        case add, edit(RoomStateEntry), remove(RoomStateEntry)
        var id: String {
            switch self {
            case .add: return "add"
            case .edit(let e): return "e-\(e.id)"
            case .remove(let e): return "r-\(e.id)"
            }
        }
    }

    /// 「我」还在这个房间吗（在场才有输入区；偷看永远没有）。
    private var present: Bool { detail?.occupants.contains("user") ?? !peek }

    var body: some View {
        ZStack {
            Color.house.bg.ignoresSafeArea()
            VStack(spacing: 0) {
                header
                if peek { peekBanner }
                presenceBar
                stateSection
                eventList
                if !peek && present { inputBar }
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        .alert("出了点状况", isPresented: Binding(
            get: { errorText != nil }, set: { if !$0 { errorText = nil } })) {
            Button("好", role: .cancel) {}
        } message: { Text(errorText ?? "") }
        .confirmationDialog(editingEntry?.text ?? "", isPresented: Binding(
            get: { editingEntry != nil }, set: { if !$0 { editingEntry = nil } }),
            titleVisibility: .visible) {
            if let entry = editingEntry {
                Button("改一改") { stateDraft = entry.text; stateSheet = .edit(entry) }
                // 清掉也可以留一句叙事（进事件流；快照直接删）——面板里写，可空
                Button("清掉", role: .destructive) { stateDraft = ""; stateSheet = .remove(entry) }
                Button("取消", role: .cancel) {}
            }
        }
        .sheet(item: $stateSheet) { mode in stateEditor(mode) }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            await refresh()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                await refresh()
            }
        }
    }

    // MARK: - 数据

    private func refresh() async {
        detail = try? await service.roomDetail(roomID)
        if let evs = try? await service.roomEvents(roomID, scope: peek ? "all" : "visible") {
            events = evs
        }
    }

    private func sendAct() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sending = true
        Task {
            defer { sending = false }
            do {
                try await service.roomAct(roomID, text: text)
                inputText = ""
                await refresh()
            } catch { errorText = error.localizedDescription }
        }
    }

    private func stateChange(_ op: String, entry: RoomStateEntry?, text: String?) {
        Task {
            do {
                try await service.roomStateChange(roomID, op: op, entryID: entry?.id, text: text)
                await refresh()
            } catch { errorText = error.localizedDescription }
        }
    }

    // MARK: - 顶栏 / 在场者 / 地点状态

    private var header: some View {
        HStack(spacing: 12) {
            Button { dismiss() } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(Color.house.textPrimary)
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(Color.house.surface))
            }
            Text(title).font(.headline).foregroundStyle(Color.house.textPrimary)
            if detail?.lock == 1 {
                Image(systemName: "lock.fill").font(.caption)
                    .foregroundStyle(Color.house.textSecondary)
            }
            Spacer()
            // 暂停/开始：按住场面好插嘴——正在生成的说完为止，之后队列攒着，
            // 恢复一口气补（偷看模式也给：旁观他们对话时同样用得上）。
            Button {
                let target = !(detail?.paused ?? false)
                Task { try? await service.worldPause(target); await refresh() }
            } label: {
                let isPaused = detail?.paused ?? false
                Label(isPaused ? "开始" : "暂停",
                      systemImage: isPaused ? "play.fill" : "pause.fill")
                    .font(.footnote.bold())
                    .foregroundStyle(isPaused ? Color.house.onAccent : Color.house.accent)
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(Capsule().fill(isPaused ? Color.house.accent : Color.house.surface))
            }
            // 没有「离开」按钮：返回只是收起页面，人还留在房间——移动只发生在
            // 地图上点「去这里 / 出门 / 回家」（机主 2026-08-16 拍板，走廊概念从用户侧移除）。
        }
        .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 10)
    }

    private var peekBanner: some View {
        Text("偷看中 · 全部历史 · 屋里的人不会察觉")
            .font(.caption2).foregroundStyle(Color.house.onAccent)
            .frame(maxWidth: .infinity).padding(.vertical, 5)
            .background(Color.house.accent.opacity(0.85))
    }

    @ViewBuilder
    private var presenceBar: some View {
        if let d = detail {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    HStack(spacing: -8) {
                        ForEach(d.occupants, id: \.self) { eid in
                            HouseAvatarChip(entityID: eid, entity: nil, size: 30)
                        }
                    }
                    Text(d.occupants.isEmpty ? "现在没人" : "")
                        .font(.caption).foregroundStyle(Color.house.textSecondary)
                    Spacer()
                }
                // 正在电脑前的人：状态一行灰字（数据从 /world 来，轮询便宜起见这里
                // 不重复拉——房卡/会话列表已有；进了屋能感知的是「他就在这、很专注」）。
            }
            .padding(.horizontal, 16).padding(.bottom, 8)
        }
    }

    @ViewBuilder
    private func stateRow(_ entry: RoomStateEntry) -> some View {
        Button {
            if !peek && present { editingEntry = entry }
        } label: {
            HStack(alignment: .top, spacing: 6) {
                Text("·").foregroundStyle(Color.house.accent)
                Text(entry.text)
                    .font(.footnote).foregroundStyle(Color.house.textPrimary)
                    .multilineTextAlignment(.leading)
                Spacer()
                Text(Self.relative(entry.since))
                    .font(.caption2).foregroundStyle(Color.house.textSecondary)
            }
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private var stateSection: some View {
        if let d = detail {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Spacer()
                    if !peek && present {
                        Button { stateDraft = ""; stateSheet = .add } label: {
                            Image(systemName: "plus.circle.fill")
                                .foregroundStyle(Color.house.accent)
                        }
                    }
                }
                // 按最近改动倒序：edit 是原位更新，不排序的话新改的会压在折叠线下面看不见
                let entries = d.state.sorted { $0.since > $1.since }
                if entries.isEmpty {
                    Text("（没什么特别的）")
                        .font(.caption).foregroundStyle(Color.house.textSecondary.opacity(0.7))
                } else if entries.count > 4 {
                    // 条目多了别把事件流挤没：约 4 条的高度内部滚动
                    ScrollView {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(entries) { entry in stateRow(entry) }
                        }
                    }
                    .frame(maxHeight: 112)
                } else {
                    ForEach(entries) { entry in stateRow(entry) }
                }
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 14).fill(Color.house.surface))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.house.line, lineWidth: 1))
            .padding(.horizontal, 16).padding(.bottom, 8)
        }
    }

    // MARK: - 事件流

    private var eventList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 10) {
                    if events.isEmpty {
                        Text(peek ? "这里还没发生过什么" : "你在这儿的这段时间还没发生什么")
                            .font(.caption).foregroundStyle(Color.house.textSecondary)
                            .padding(.top, 30)
                    }
                    ForEach(events) { ev in eventRow(ev).id(ev.id) }
                    // 正在回应：房间里谁的醒来在生成中（轮询带回来的）
                    if let reps = detail?.replying, !reps.isEmpty {
                        ForEach(reps, id: \.self) { cid in
                            HStack(spacing: 8) {
                                HouseAvatarChip(entityID: cid, entity: nil, size: 26)
                                Text("\(actorName(cid)) 正在回应")
                                    .font(.caption)
                                    .foregroundStyle(Color.house.textSecondary)
                                HouseTypingDots()
                                Spacer()
                            }
                            .id("replying-\(cid)")
                        }
                    }
                }
                .padding(.horizontal, 16).padding(.vertical, 10)
                .frame(maxWidth: .infinity, minHeight: 44)
                // 点事件区空白 = 收键盘（simultaneous：别抢气泡将来可能有的点击）
                .contentShape(Rectangle())
                .onTapGesture { inputFocused = false }
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: events) { _, evs in
                if let last = evs.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
            // 键盘弹出 → 底部自动触底（等键盘动画起来再滚，不然滚了个寂寞）
            .onChange(of: inputFocused) { _, focused in
                guard focused, let last = events.last else { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    @ViewBuilder
    private func eventRow(_ ev: RoomEvent) -> some View {
        let mine = ev.actor == "user"
        let idc = IdentityColor.color(for: ev.actor)
        switch ev.type {
        case "system":
            Text("—— \(ev.text) ——")
                .font(.caption2).foregroundStyle(Color.house.textSecondary)
                .frame(maxWidth: .infinity)
        case "action":
            (Text("\(actorName(ev.actor)) ").bold().foregroundStyle(idc)
             + Text("*\(ev.text)*").italic().foregroundStyle(Color.house.textSecondary))
                .font(.footnote)
                .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        default:   // speech：气泡底 = 角色识别色（低透明度），谁说的一眼可辨
            VStack(alignment: mine ? .trailing : .leading, spacing: 3) {
                (Text(actorName(ev.actor)).foregroundStyle(idc).bold()
                 + Text(" · \(Self.hhmm(ev.ts))").foregroundStyle(Color.house.textSecondary))
                    .font(.caption2)
                Text(ev.text)
                    .font(.body)
                    .foregroundStyle(Color.house.textPrimary)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 16).fill(idc.opacity(0.26)))
                    .overlay(RoundedRectangle(cornerRadius: 16)
                        .stroke(idc.opacity(0.35), lineWidth: 1))
            }
            .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        }
    }

    private func actorName(_ eid: String) -> String {
        eid == "user" ? HouseUserName.value : (CharacterNameCache.shared.name(eid) ?? eid)
    }

    // MARK: - 输入区（混写：*星号* 是动作、其余是说话，可交错，与 AI 的 act 对称）

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField("说话，*动作* 用星号包起来，可交错…", text: $inputText, axis: .vertical)
                .focused($inputFocused)
                .textFieldStyle(.plain)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 14).fill(Color.house.surfaceHi))
                .foregroundStyle(Color.house.textPrimary)
            Button(action: sendAct) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 30))
                    .foregroundStyle(canSend ? Color.house.accent : Color.house.textSecondary)
            }
            .disabled(!canSend || sending)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(Color.house.surface.ignoresSafeArea(edges: .bottom))
    }

    private var canSend: Bool {
        !inputText.trimmingCharacters(in: .whitespaces).isEmpty
    }

    // MARK: - 地点状态编辑

    @ViewBuilder
    private func stateEditor(_ mode: StateSheetMode) -> some View {
        let isRemove = { if case .remove = mode { return true } else { return false } }()
        NavigationStack {
            VStack(spacing: 14) {
                TextField(isRemove ? "说一句收拾了什么（可空），如：把空碗收走了"
                                   : "比如：桌上剩了半杯牛奶",
                          text: $stateDraft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .padding(.horizontal, 16)
                Text(isRemove
                     ? "这条状态会直接从快照里删掉；写的话只进事件流（谁「那段文字」）。"
                     : "这是「现在这里有什么」的快照——收拾掉一样东西就清掉那条，别写成日志。")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.horizontal, 16)
                Spacer()
            }
            .padding(.top, 20)
            .navigationTitle(sheetTitle(mode))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { stateSheet = nil }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(isRemove ? "清掉" : "好了") {
                        let text = stateDraft.trimmingCharacters(in: .whitespacesAndNewlines)
                        switch mode {
                        case .add:
                            guard !text.isEmpty else { return }
                            stateChange("add", entry: nil, text: text)
                        case .edit(let entry):
                            guard !text.isEmpty else { return }
                            stateChange("edit", entry: entry, text: text)
                        case .remove(let entry):
                            stateChange("remove", entry: entry,
                                        text: text.isEmpty ? nil : text)
                        }
                        stateSheet = nil
                    }
                }
            }
        }
        .presentationDetents([.height(220)])
    }

    private func sheetTitle(_ mode: StateSheetMode) -> String {
        switch mode {
        case .add: return "添加一条"
        case .edit: return "改一改"
        case .remove: return "收拾掉这条"
        }
    }

    // MARK: - 时间格式

    static func hhmm(_ ts: Int) -> String {
        let f = DateFormatter(); f.dateFormat = "HH:mm"
        return f.string(from: Date(timeIntervalSince1970: TimeInterval(ts)))
    }

    static func relative(_ ts: Int) -> String {
        let s = Int(Date().timeIntervalSince1970) - ts
        if s < 3600 { return "\(max(1, s / 60))分钟前" }
        if s < 86400 { return "\(s / 3600)小时前" }
        return "\(s / 86400)天前"
    }
}

/// 小屋里用户的显示名（机主 2026-08-16：小屋的「你」改成 user 名称）。
/// 数据源 = 设置页同一份（proactive_settings 的 user_name，默认角色那份是全局键）。
enum HouseUserName {
    static var value: String {
        if let data = UserDefaults.standard.data(forKey: "proactive_settings"),
           let s = try? JSONDecoder().decode(ProactiveSettings.self, from: data),
           !s.userName.isEmpty, s.userName != "user" {
            return s.userName
        }
        return "你"   // 没起过昵称就退回「你」
    }
}

/// 「正在回应」的三点呼吸动画（小屋版 typing indicator）。
struct HouseTypingDots: View {
    @State private var on = false

    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(Color.house.textSecondary)
                    .frame(width: 5, height: 5)
                    .opacity(on ? 1 : 0.25)
                    .animation(.easeInOut(duration: 0.6)
                        .repeatForever(autoreverses: true)
                        .delay(Double(i) * 0.2), value: on)
            }
        }
        .onAppear { on = true }
    }
}

/// 角色 id → 显示名的轻量缓存：房间事件流里频繁要名字，别每行都去翻 CharacterListStore。
/// 数据源与会话列表同一份（/characters 的缓存），HousePage 出现时刷一次。
final class CharacterNameCache {
    static let shared = CharacterNameCache()
    private var names: [String: String] = [:]

    init() {
        if let data = UserDefaults.standard.data(forKey: "characters_cache"),
           let cached = try? JSONDecoder().decode([CharacterInfo].self, from: data) {
            for c in cached where !c.display_name.isEmpty { names[c.id] = c.display_name }
        }
    }

    func name(_ id: String) -> String? { names[id] }
}
