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
    @State private var actionText = ""
    @State private var speechText = ""
    @State private var sending = false
    @State private var errorText: String?
    @State private var editingEntry: RoomStateEntry?   // 点了哪条地点状态（弹编辑/清掉）
    @State private var stateDraft = ""                 // add/edit 的草稿
    @State private var stateSheet: StateSheetMode?

    private let service = ChatService()

    private enum StateSheetMode: Identifiable {
        case add, edit(RoomStateEntry)
        var id: String { if case .edit(let e) = self { return e.id } else { return "add" } }
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
                Button("清掉", role: .destructive) { stateChange("remove", entry: entry, text: nil) }
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
        let action = actionText.trimmingCharacters(in: .whitespacesAndNewlines)
        let speech = speechText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !action.isEmpty || !speech.isEmpty else { return }
        sending = true
        Task {
            defer { sending = false }
            do {
                try await service.roomAct(roomID, action: action, speech: speech)
                actionText = ""; speechText = ""
                await refresh()
            } catch { errorText = error.localizedDescription }
        }
    }

    private func leave() {
        Task {
            _ = try? await service.worldMove(to: "hallway")
            dismiss()
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
            if !peek && present {
                Button(action: leave) {
                    Label("离开", systemImage: "door.left.hand.open")
                        .font(.footnote.bold())
                        .foregroundStyle(Color.house.accent)
                        .padding(.horizontal, 12).padding(.vertical, 7)
                        .background(Capsule().fill(Color.house.surface))
                }
            }
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
    private var stateSection: some View {
        if let d = detail {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("这里现在有什么")
                        .font(.caption.bold()).foregroundStyle(Color.house.textSecondary)
                    Spacer()
                    if !peek && present {
                        Button { stateDraft = ""; stateSheet = .add } label: {
                            Image(systemName: "plus.circle.fill")
                                .foregroundStyle(Color.house.accent)
                        }
                    }
                }
                if d.state.isEmpty {
                    Text("（没什么特别的）")
                        .font(.caption).foregroundStyle(Color.house.textSecondary.opacity(0.7))
                } else {
                    ForEach(d.state) { entry in
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
                }
                .padding(.horizontal, 16).padding(.vertical, 10)
            }
            .onChange(of: events) { _, evs in
                if let last = evs.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
    }

    @ViewBuilder
    private func eventRow(_ ev: RoomEvent) -> some View {
        let mine = ev.actor == "user"
        switch ev.type {
        case "system":
            Text("—— \(ev.text) ——")
                .font(.caption2).foregroundStyle(Color.house.textSecondary)
                .frame(maxWidth: .infinity)
        case "action":
            (Text("\(actorName(ev.actor)) ").bold() + Text("*\(ev.text)*").italic())
                .font(.footnote).foregroundStyle(Color.house.textSecondary)
                .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        default:   // speech
            VStack(alignment: mine ? .trailing : .leading, spacing: 3) {
                Text("\(actorName(ev.actor)) · \(Self.hhmm(ev.ts))")
                    .font(.caption2).foregroundStyle(Color.house.textSecondary)
                Text(ev.text)
                    .font(.body)
                    .foregroundStyle(mine ? Color.house.onAccent : Color.house.textPrimary)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 16)
                        .fill(mine ? Color.house.accent : Color.house.surfaceHi))
            }
            .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        }
    }

    private func actorName(_ eid: String) -> String {
        eid == "user" ? "你" : (CharacterNameCache.shared.name(eid) ?? eid)
    }

    // MARK: - 输入区（与 AI 的 act 对称：动作 + 说话，可一空）

    private var inputBar: some View {
        VStack(spacing: 6) {
            TextField("*动作*（可空，如：把外套搭在椅背上）", text: $actionText, axis: .vertical)
                .font(.footnote.italic())
                .textFieldStyle(.plain)
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(RoundedRectangle(cornerRadius: 12).fill(Color.house.surfaceHi))
                .foregroundStyle(Color.house.textPrimary)
            HStack(spacing: 8) {
                TextField("说点什么…", text: $speechText, axis: .vertical)
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
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(Color.house.surface.ignoresSafeArea(edges: .bottom))
    }

    private var canSend: Bool {
        !actionText.trimmingCharacters(in: .whitespaces).isEmpty ||
        !speechText.trimmingCharacters(in: .whitespaces).isEmpty
    }

    // MARK: - 地点状态编辑

    @ViewBuilder
    private func stateEditor(_ mode: StateSheetMode) -> some View {
        NavigationStack {
            VStack(spacing: 14) {
                TextField("比如：桌上剩了半杯牛奶", text: $stateDraft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .padding(.horizontal, 16)
                Text("这是「现在这里有什么」的快照——收拾掉一样东西就清掉那条，别写成日志。")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.horizontal, 16)
                Spacer()
            }
            .padding(.top, 20)
            .navigationTitle(modeIsAdd(mode) ? "添加一条" : "改一改")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { stateSheet = nil }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("好了") {
                        let text = stateDraft.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !text.isEmpty else { return }
                        if case .edit(let entry) = mode {
                            stateChange("edit", entry: entry, text: text)
                        } else {
                            stateChange("add", entry: nil, text: text)
                        }
                        stateSheet = nil
                    }
                }
            }
        }
        .presentationDetents([.height(220)])
    }

    private func modeIsAdd(_ mode: StateSheetMode) -> Bool {
        if case .add = mode { return true }
        return false
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
