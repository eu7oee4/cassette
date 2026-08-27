import SwiftUI

/// 房间视图（PLAN_cohabit C3）。两种打开方式：
/// - 去这里（peek=false）：真实在场——事件流 = 本次在场区间（scope=visible），
///   底部输入区与 AI 的 act 对称（*动作* + 说话，可一空）；返回只收页面、人留原地。
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
    @State private var offerBusy = false               // 邀约应答进行中（防连点）
    @State private var petCare: PetCareTarget?         // 照顾面板（在场宠物）
    @State private var swipedGroup: String?            // 左滑露出删除键的那一轮（一次只开一个）
    @State private var deleteTarget: TurnGroup?        // 点了删除 → 确认弹窗（删了不可撤）
    @State private var noticeText: String?             // 删完的实话（有一轮撤不回时说一声）
    @State private var landedAtBottom = false          // 首批事件到了没：进门要落在最新一条
    @FocusState private var inputFocused: Bool         // 输入区聚焦（收键盘/自动触底用）

    struct PetCareTarget: Identifiable { let id: String }

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

    /// 小屋休眠中（总开关关着）：房间只读——能看，不能说话/改状态/摸猫
    /// （服务端 409 是真闸，这里把入口一并收掉，别让人按了才碰壁）。
    private var frozen: Bool { detail?.enabled == false }

    var body: some View {
        ZStack {
            Color.house.bg.ignoresSafeArea()
            VStack(spacing: 0) {
                header
                if peek { peekBanner }
                else if detail != nil && !present { goneBanner }
                if frozen { frozenBanner }
                if let err = detail?.queue_error { queueErrorBanner(err) }
                presenceBar
                stateSection
                carryOfferCard
                eventList
                if !peek && present && !frozen { inputBar }
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        .alert("出了点状况", isPresented: Binding(
            get: { errorText != nil }, set: { if !$0 { errorText = nil } })) {
            Button("好", role: .cancel) {}
        } message: { Text(errorText ?? "") }
        .alert("删了，有一条撤不回", isPresented: Binding(
            get: { noticeText != nil }, set: { if !$0 { noticeText = nil } })) {
            Button("知道了", role: .cancel) {}
        } message: { Text(noticeText ?? "") }
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
        // 左滑删除的确认（删了不可撤：房间事件流和角色经历流一起没，AI 下次注入就看不见了）
        .confirmationDialog("删掉这一轮的 \(deleteTarget?.deletable.count ?? 0) 条记录？",
                            isPresented: Binding(
            get: { deleteTarget != nil }, set: { if !$0 { deleteTarget = nil } }),
            titleVisibility: .visible) {
            if let g = deleteTarget {
                Button("删除", role: .destructive) { deleteGroup(g) }
                Button("取消", role: .cancel) { swipedGroup = nil }
            }
        } message: {
            Text("大家的记忆里也会一起消失，下次醒来就看不见了。进出场那条会留着。")
        }
        .sheet(item: $stateSheet) { mode in stateEditor(mode) }
        .sheet(item: $petCare) { target in
            PetCareSheet(petID: target.id,
                         canScoop: detail?.owner == target.id)   // 猫砂盆只在猫房
                .presentationDetents([.medium, .large])
        }
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
            // 休眠中不给：总开关冻的是全部，暂停键在玻璃罩里没有意义。
            if !frozen {
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

    /// 离场回看（2026-08-17）：人已经不在这个房间——下面的事件流是服务端给的
    /// 「你最后一段在场区间」，只读；你走之后发生的事不在里面（那才要偷看）。
    private var goneBanner: some View {
        Text("你已不在这个房间——这是你在场时看到的")
            .font(.caption2).foregroundStyle(Color.house.textSecondary)
            .frame(maxWidth: .infinity).padding(.vertical, 5)
            .background(Color.house.surfaceHi)
    }

    /// 小屋休眠（总开关关着）：冻结的最后一帧，只能看。开关在小屋页的铃铛里。
    private var frozenBanner: some View {
        Label("小屋休眠中——一切都静止着，只能看", systemImage: "moon.zzz.fill")
            .font(.caption2).foregroundStyle(Color.house.textSecondary)
            .frame(maxWidth: .infinity).padding(.vertical, 5)
            .background(Color.house.surfaceHi)
    }

    /// 队列被按停的原因（模型过载，服务端已重试过一次）。按「开始」= 知道了，服务端清掉它。
    private func queueErrorBanner(_ text: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill").font(.caption2)
            Text("\(text)——队列已暂停，按「开始」重试")
                .font(.caption2)
                .multilineTextAlignment(.leading)
            Spacer(minLength: 0)
        }
        .foregroundStyle(.red)
        .padding(.horizontal, 16).padding(.vertical, 6)
        .frame(maxWidth: .infinity)
        .background(Color.red.opacity(0.12))
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
                    // 照顾入口：宠物在场且我也在场才摸得着（偷看是上帝视角，摸不着猫；
                    // 休眠中猫在冬眠，同样摸不着）
                    if !peek, present, !frozen, let pet = d.pets?.first {
                        Button { petCare = PetCareTarget(id: pet) } label: {
                            Image(systemName: "pawprint.fill")
                                .font(.system(size: 15))
                                .foregroundStyle(Color.house.accent)
                                .frame(width: 30, height: 30)
                                .background(Circle().fill(Color.house.surface))
                        }
                    }
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
            if !peek && present && !frozen { editingEntry = entry }
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
                    if !peek && present && !frozen {
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

    // MARK: - 抱人邀约（答应了才会一起移动；服务端只在发生的房间带回这张卡）

    @ViewBuilder
    private var carryOfferCard: some View {
        if !peek, let off = detail?.carry_offer {
            HStack(spacing: 10) {
                Image(systemName: "figure.2.arms.open")
                    .foregroundStyle(Color.house.accent)
                Text("\(off.actor_name) 想抱你去「\(off.to_name)」")
                    .font(.footnote).foregroundStyle(Color.house.textPrimary)
                Spacer()
                Button("不要") { respondOffer(off, accept: false) }
                    .font(.footnote.bold())
                    .foregroundStyle(Color.house.textSecondary)
                Button("好呀") { respondOffer(off, accept: true) }
                    .font(.footnote.bold())
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(Capsule().fill(Color.house.accent))
                    .foregroundStyle(Color.house.onAccent)
            }
            .disabled(offerBusy)
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 14).fill(Color.house.surface))
            .overlay(RoundedRectangle(cornerRadius: 14)
                .stroke(Color.house.accent.opacity(0.5), lineWidth: 1))
            .padding(.horizontal, 16).padding(.bottom, 8)
        }
    }

    private func respondOffer(_ off: CarryOffer, accept: Bool) {
        offerBusy = true
        Task {
            defer { offerBusy = false }
            do {
                try await service.respondCarryOffer(id: off.id, accept: accept)
                await refresh()
                if accept { dismiss() }   // 被抱走了：回房子视图看自己到了哪
            } catch {
                errorText = error.localizedDescription   // 409 = 邀约已经不在了
                await refresh()
            }
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
                    let groups = turnGroups
                    ForEach(Array(groups.enumerated()), id: \.element.id) { i, g in
                        VStack(spacing: 10) {
                            if i > 0 && g.turn != groups[i - 1].turn { turnDivider }
                            SwipeToDeleteRow(
                                enabled: !g.deletable.isEmpty,
                                isOpen: swipedGroup == g.id,
                                onOpen: { swipedGroup = g.id },
                                onClose: { if swipedGroup == g.id { swipedGroup = nil } },
                                onDelete: { deleteTarget = g }
                            ) {
                                VStack(spacing: 10) {
                                    ForEach(g.events) { ev in eventRow(ev).id(ev.id) }
                                }
                            }
                        }
                    }
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
                // 点事件区空白 = 收键盘 + 收起左滑露出来的删除键
                .contentShape(Rectangle())
                .onTapGesture { inputFocused = false; swipedGroup = nil }
            }
            .scrollDismissesKeyboard(.interactively)
            // 进门就在最新那条（房间是「现在」，不是从头读的档案）。光靠 onChange 不够：
            // 首帧 LazyVStack 还没把内容排完，那一脚滚了个寂寞——所以 defaultScrollAnchor
            // 先把起点定在底，第一批数据到了再不带动画补一脚（带动画会看见它从头飞下来）。
            .defaultScrollAnchor(.bottom)
            .onChange(of: events) { _, evs in
                guard let last = evs.last else { return }
                if landedAtBottom {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                } else {
                    landedAtBottom = true
                    proxy.scrollTo(last.id, anchor: .bottom)
                    // 懒加载排完版可能晚半拍，补一脚兜底
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
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

    /// 一轮 = 分割线之间的一组（一次醒来 / 一次发送落下的全部事件）。左滑删除按组走，
    /// 服务端也按 turn 收整组——两边同一个口径。
    /// **没有 turn 的老历史各自成组**：服务端对无 turn 的事件只删它自己，UI 要是把连着
    /// 的几条圈成一组，滑一下只会掉一条，看着像删漏了。分轮横线仍按 turn 比（nil != nil
    /// 是 false，老历史之间照旧不画线）。
    private struct TurnGroup: Identifiable {
        let id: String            // 组内第一条的 id（删除时拿它去服务端认这一轮）
        let turn: String?
        let events: [RoomEvent]
        /// 真能删的：进出场是可见区间的骨架，服务端留着不删（组里只剩它就别给删除键）
        var deletable: [RoomEvent] {
            events.filter { $0.kind != "enter" && $0.kind != "leave" }
        }
    }

    private var turnGroups: [TurnGroup] {
        var out: [TurnGroup] = []
        for ev in events {
            if let t = ev.turn, let last = out.last, last.turn == t {
                out[out.count - 1] = TurnGroup(id: last.id, turn: t, events: last.events + [ev])
            } else {
                out.append(TurnGroup(id: ev.id, turn: ev.turn, events: [ev]))
            }
        }
        return out
    }

    private func deleteGroup(_ g: TurnGroup) {
        Task {
            do {
                let res = try await service.roomDeleteTurn(roomID, eventID: g.id)
                swipedGroup = nil
                // 正在生成的那一轮撤不回：注入在它开始时就组好了，删得再快也已经读进去
                if let who = res.replying {
                    noticeText = "删了，但 \(actorName(who)) 这一轮已经开始生成了——"
                        + "它读到的是删之前的样子，这次的回应里可能还带着。"
                        + "回头想清掉 TA 的那段内心，去心流日志删。"
                }
                await refresh()
            } catch {
                errorText = error.localizedDescription
                await refresh()   // 可能已经被别处删过了：以服务端为准
            }
        }
    }

    private var turnDivider: some View {
        Rectangle()
            .fill(Color.house.textSecondary.opacity(0.22))
            .frame(height: 1)
            .padding(.vertical, 2)
    }

    @ViewBuilder
    private func eventRow(_ ev: RoomEvent) -> some View {
        let mine = ev.actor == "user"
        let idc = IdentityColor.color(for: ev.actor)
        switch ev.type {
        case "system":
            if ev.kind == "state" {
                // 状态改动通知：谁改的靠颜色认（名字剥掉——服务端文本是「名字「一段话」」），
                // 字号与斜体动作一致；靠边规则也跟动作一致（我在右、别人在左）
                Text(Self.stripLeadingName(ev.text))
                    .font(.footnote).bold().foregroundStyle(idc)
                    .multilineTextAlignment(mine ? .trailing : .leading)
                    .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
            } else {
                // 进出场等系统叙述：名字是句子的主语，剥不得，保持原样式
                Text("—— \(ev.text) ——")
                    .font(.caption2).foregroundStyle(Color.house.textSecondary)
                    .frame(maxWidth: .infinity)
            }
        case "action":
            // 识别色名字 + 灰字动作（试过整句上识别色，2026-08-17 改回：名字留着更好认）
            (Text("\(actorName(ev.actor)) ").bold().foregroundStyle(idc)
             + Text("*\(ev.text)*").italic().bold().foregroundStyle(Color.house.textSecondary))
                .font(.footnote)
                // frame 只管整块靠哪边；折行后行内对齐要单独说，不然第二行起全回左边
                .multilineTextAlignment(mine ? .trailing : .leading)
                .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        default:   // speech：气泡底 = 角色识别色（低透明度），谁说的靠颜色认；时间贴气泡内侧
            HStack(alignment: .bottom, spacing: 6) {
                if mine {
                    Spacer(minLength: 40)
                    timeLabel(ev.ts)
                }
                Text(Self.stripQuotes(ev.text))
                    .font(.body)
                    .foregroundStyle(Color.house.textPrimary)
                    // 长句该折行，不该截成一行加 …（机主 2026-08-19 截图：最新那组的
                    // 两句都被截了，上一组好好的）。原因是气泡在 HStack 里跟 Spacer
                    // 抢宽度，赶上自动触底那一脚排版时会按「一行的理想宽度」量完截尾。
                    // fixedSize 纵向放开＝要多高给多高；layoutPriority 让它先拿够宽度。
                    .fixedSize(horizontal: false, vertical: true)
                    .layoutPriority(1)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 16).fill(idc.opacity(0.26)))
                    .overlay(RoundedRectangle(cornerRadius: 16)
                        .stroke(idc.opacity(0.35), lineWidth: 1))
                if !mine {
                    timeLabel(ev.ts)
                    Spacer(minLength: 40)
                }
            }
            .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        }
    }

    private func timeLabel(_ ts: Int) -> some View {
        Text(Self.hhmm(ts))
            .font(.caption2)
            .foregroundStyle(Color.house.textSecondary)
    }

    /// 状态通知的服务端文本剥掉打头的名字：常规是 `名字「一段话」`（从第一个「起保留）；
    /// remove 无叙事的兜底是 `名字 清掉了「原文」`（「之前有空格 → 从空格后保留，
    /// 「清掉了」不能丢，丢了读起来像新增）。都对不上就原样返回，别把内容剥没了。
    /// 引号本身只是服务端的分隔符，剥完名字后一并去掉（机主 2026-08-17：显示上不要「」）。
    private static func stripLeadingName(_ text: String) -> String {
        var body = text
        if let q = text.firstIndex(of: "「") {
            if let sp = text.firstIndex(of: " "), sp < q {
                body = String(text[text.index(after: sp)...])
            } else {
                body = String(text[q...])
            }
        }
        return body.filter { $0 != "「" && $0 != "」" }
    }

    /// 气泡里不显示裹在整句外面的引号——气泡本身就是「这是说的话」。
    /// 服务端从 2026-08-19 起写入时就剥了，这里管的是**存量**：之前模型自己写进去的
    /// 那层（甚至三层，见小卡 8-19 那条）照样躺在事件流里。句中的引号不动。
    private static func stripQuotes(_ text: String) -> String {
        var s = text.trimmingCharacters(in: .whitespaces)
        let pairs: [(Character, Character)] = [("「", "」"), ("『", "』"), ("“", "”"), ("\"", "\"")]
        var peeled = true
        while peeled, s.count >= 2 {
            peeled = false
            for (lo, hi) in pairs where s.first == lo && s.last == hi {
                guard wrapped(s, lo, hi) else { continue }
                s = String(s.dropFirst().dropLast()).trimmingCharacters(in: .whitespaces)
                peeled = true
                break
            }
        }
        return s
    }

    /// 首引号是不是正好在末尾闭合（「甲」和「乙」不算裹住，剥了会吃掉中间的字）。
    private static func wrapped(_ s: String, _ lo: Character, _ hi: Character) -> Bool {
        if lo == hi { return s.filter { $0 == lo }.count == 2 }
        var depth = 0
        for (i, ch) in s.enumerated() {
            if ch == lo { depth += 1 }
            else if ch == hi {
                depth -= 1
                if depth == 0 && i != s.count - 1 { return false }
            }
        }
        return depth == 0
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

/// 左滑露出删除键（房间事件流按「一轮」删，2026-08-19）。
/// 事件流是 LazyVStack 不是 List，没有 .swipeActions 可用——气泡样式不肯为一个删除键
/// 让位，所以自己做一个：**横向位移大于纵向才认**，simultaneousGesture 挂着不抢
/// ScrollView 的滚动；开合状态由父视图持有，保证一次只开一个。
private struct SwipeToDeleteRow<Content: View>: View {
    let enabled: Bool
    let isOpen: Bool
    let onOpen: () -> Void
    let onClose: () -> Void
    let onDelete: () -> Void
    @ViewBuilder let content: () -> Content

    private let revealed: CGFloat = 76
    /// 起手死区：手指先走够这些点，格子才开始跟着动。滑动列表时手指本来就会带点横向
    /// 分量，死区小了就一路蹭出删除键来（机主 2026-08-19：太容易误触）。
    private let deadZone: CGFloat = 30
    /// 松手时超过这个距离才算「我要删」，否则弹回去。
    private var openAt: CGFloat { revealed * 0.8 }
    @GestureState private var drag: CGFloat = 0

    private func _horizontal(_ t: CGSize) -> Bool {
        abs(t.width) > abs(t.height) * 1.5
    }

    /// 手指位移 → 格子位移：死区内不动，出了死区从 0 开始接着走（不跳一下）。
    private func damped(_ dx: CGFloat) -> CGFloat {
        if dx < -deadZone { return dx + deadZone }
        if dx > deadZone { return dx - deadZone }
        return 0
    }

    var body: some View {
        let offset = enabled
            ? min(0, max(-revealed - 16, (isOpen ? -revealed : 0) + drag))
            : 0
        ZStack(alignment: .trailing) {
            Button(role: .destructive, action: onDelete) {
                Text("删除")
                    .font(.footnote.bold())
                    .foregroundStyle(.white)
                    .frame(width: revealed - 8)
                    .padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Color.red))
            }
            .opacity(offset < -12 ? 1 : 0)      // 没滑开就别让它抢点击
            .allowsHitTesting(offset < -12)

            content()
                .frame(maxWidth: .infinity)
                .background(Color.house.bg)     // 盖住底下的删除键（不然滑之前就透出来）
                .offset(x: offset)
        }
        .animation(.snappy(duration: 0.2), value: isOpen)
        // 横向位移要**明显压过**纵向（1.5 倍）才认：滚动时的手抖不该滑出删除键。
        .simultaneousGesture(
            DragGesture(minimumDistance: deadZone)
                .updating($drag) { v, state, _ in
                    guard enabled, _horizontal(v.translation) else { return }
                    state = damped(v.translation.width)
                }
                .onEnded { v in
                    guard enabled, _horizontal(v.translation) else { return }
                    let end = (isOpen ? -revealed : 0) + damped(v.translation.width)
                    if end < -openAt { onOpen() } else { onClose() }
                })
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

/// 照顾面板（PLAN_pet P3）：状态四条 + 砂盆 + 需求 ｜ 喂三选一 ｜ 自由互动 ｜ 铲屎。
/// 与 pet MCP 打同一套 /pets/* 端点；猫的动作/喵声照常落事件流，这里只展示同轮反应。
struct PetCareSheet: View {
    let petID: String
    let canScoop: Bool        // 只有猫房（owner==pet）才有猫砂盆

    @Environment(\.dismiss) private var dismiss
    @State private var info: PetInfo?
    @State private var reply = ""          // 猫的最近一次反应（同轮返回）
    @State private var interactText = ""
    @State private var busy = false
    @State private var errorText: String?

    private let service = ChatService()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let i = info {
                        statHeader(i)
                        statBars(i.state)
                        if !i.needs.isEmpty { needsBlock(i.needs) }
                        if !reply.isEmpty { replyBubble }
                        feedRow
                        interactRow
                        if canScoop { scoopRow(i.state) }
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                    }
                }
                .padding(16)
            }
            .background(Color.house.bg)
            .navigationTitle(info?.name ?? "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("收起") { dismiss() }
                }
            }
            .alert("没成", isPresented: Binding(
                get: { errorText != nil }, set: { if !$0 { errorText = nil } })) {
                Button("好", role: .cancel) {}
            } message: { Text(errorText ?? "") }
            .task { info = try? await service.petInfo(petID) }
        }
    }

    private func refresh() async { info = try? await service.petInfo(petID) }

    private func run(_ op: @escaping () async throws -> Void) {
        busy = true
        Task {
            defer { busy = false }
            do { try await op(); await refresh() }
            catch { errorText = error.localizedDescription; await refresh() }
        }
    }

    // MARK: 展示

    private func statHeader(_ i: PetInfo) -> some View {
        HStack(spacing: 8) {
            Text("第 \(i.state.day) 天")
                .font(.caption).foregroundStyle(Color.house.textSecondary)
            if i.state.asleep {
                Text("睡着了 💤").font(.caption).foregroundStyle(Color.house.textSecondary)
            }
            Spacer()
            Text("猫砂盆：\(i.state.litterLabel)")
                .font(.caption)
                .foregroundStyle(i.state.litter >= 3 ? Color.red : Color.house.textSecondary)
        }
    }

    private func statBars(_ s: PetStats) -> some View {
        VStack(spacing: 8) {
            statBar("饱腹", s.satiety)
            statBar("水分", s.hydration)
            statBar("精力", s.energy)
            statBar("心情", s.mood)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color.house.surface))
    }

    private func statBar(_ label: String, _ v: Int) -> some View {
        HStack(spacing: 10) {
            Text(label).font(.caption).foregroundStyle(Color.house.textSecondary)
                .frame(width: 34, alignment: .leading)
            ProgressView(value: Double(v), total: 100)
                .tint(v < 35 ? Color.red.opacity(0.7) : Color.house.accent)
            Text("\(v)").font(.caption2.monospacedDigit())
                .foregroundStyle(Color.house.textSecondary)
                .frame(width: 28, alignment: .trailing)
        }
    }

    private func needsBlock(_ needs: [PetNeed]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(needs) { n in
                Text("· \(n.text)")
                    .font(.caption).foregroundStyle(Color.house.textPrimary)
            }
        }
    }

    private var replyBubble: some View {
        Text(reply)
            .font(.footnote.italic())
            .foregroundStyle(Color.house.textPrimary)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12)
                .fill(Color.house.accent.opacity(0.15)))
    }

    // MARK: 动作

    private var feedRow: some View {
        HStack(spacing: 10) {
            ForEach(["罐罐", "猫条", "冻干"], id: \.self) { food in
                Button(food) {
                    run { reply = try await service.petInteract(petID, feed: food).reply }
                }
                .font(.footnote.bold())
                .padding(.horizontal, 14).padding(.vertical, 8)
                .background(Capsule().fill(Color.house.surface))
                .foregroundStyle(Color.house.textPrimary)
            }
            Spacer()
        }
        .disabled(busy)
    }

    private var interactRow: some View {
        HStack(spacing: 8) {
            TextField("摸摸它 / 逗逗它，一句话…", text: $interactText)
                .textFieldStyle(.plain)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 12).fill(Color.house.surfaceHi))
            Button {
                let t = interactText.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else { return }
                run {
                    reply = try await service.petInteract(petID, text: t).reply
                    interactText = ""
                }
            } label: {
                Image(systemName: "hand.wave.fill")
                    .foregroundStyle(busy ? Color.house.textSecondary : Color.house.accent)
            }
            .disabled(busy)
        }
    }

    private func scoopRow(_ s: PetStats) -> some View {
        Button {
            run { try await service.petScoop(petID) }
        } label: {
            Label(s.litter > 1 ? "铲屎（该铲了）" : "铲屎",
                  systemImage: "trash.fill")
                .font(.footnote.bold())
                .frame(maxWidth: .infinity).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 12).fill(Color.house.surface))
                .foregroundStyle(s.litter > 1 ? Color.red : Color.house.textSecondary)
        }
        .disabled(busy)
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
