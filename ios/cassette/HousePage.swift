import SwiftUI

/// 小屋（房子视图，PLAN_cohabit C3）：三层平面、谁在哪、出门/回家开关。
/// 点房间 → 三选一：去这里（真实移动，过门禁）/ 偷看一眼（上帝视角）/ 取消。
/// 配色走 Color.house（Theme.swift，一键换肤），不随系统深浅色走——小屋是独立的暖色世界。
struct HousePage: View {
    /// 根模式（「小屋当首页」开着时由 ContentView 传入）：左上角出抽屉钮、
    /// 右下角浮一颗手机钮（聊天降格为随时可唤出的悬浮层）。缺省全 nil = 抽屉里点进来的普通页。
    var asRoot = false
    var unreadCount = 0
    var onOpenDrawer: (() -> Void)? = nil
    var onOpenPhone: (() -> Void)? = nil

    @EnvironmentObject private var profileStore: ProfileStore
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dismiss) private var dismiss

    @State private var world: WorldSnapshot?
    @State private var floor = 2
    @State private var entryTarget: WorldRoom?    // 「去这里/回家」的进场输入弹窗
    @State private var entryText = ""             // 进场的样子（可空）
    @State private var entryPushes = true         // 进完场要不要推房间页（回家=不推）
    @State private var nav: RoomNav?
    @State private var lockedText: String?        // 门锁着的提示
    @State private var loadError = false
    @State private var nudgeOpen = false          // 环境动静（导演口）弹层

    private let service = ChatService()
    private let floors: [(Int, String)] = [(0, "地下室"), (1, "1楼"), (2, "2楼")]

    var body: some View {
        ZStack {
            Color.house.bg.ignoresSafeArea()
            VStack(spacing: 0) {
                header
                if let err = world?.queue_error { queueErrorBanner(err) }
                floorTabs
                ScrollView {
                    VStack(spacing: 14) {
                        carryOfferBanner
                        floorContent
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 14)
                }
            }
            if asRoot { phoneButton }
        }
        .toolbar(.hidden, for: .navigationBar)
        .navigationDestination(item: $nav) { n in
            RoomPage(roomID: n.roomID, title: n.title, peek: n.peek)
        }
        // 进场自带动作：可写可空（「打着哈欠下楼」），落成进门后的第一条动作事件
        .alert("去「\(entryTarget?.name ?? "")」", isPresented: Binding(
            get: { entryTarget != nil }, set: { if !$0 { entryTarget = nil } })) {
            TextField("进场的样子（可空）", text: $entryText)
            Button("走") { if let r = entryTarget { goTo(r, entry: entryText, push: entryPushes) } }
            Button("算了", role: .cancel) {}
        }
        .sheet(isPresented: $nudgeOpen) {
            NudgeSheet(world: world, service: service)
                .presentationDetents([.medium])
        }
        // 「没成」：门锁着 / 没走成 / 邀约已失效，都从这儿说
        .alert("没成", isPresented: Binding(
            get: { lockedText != nil }, set: { if !$0 { lockedText = nil } })) {
            Button("好吧", role: .cancel) {}
        } message: { Text(lockedText ?? "") }
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

    private var userLocation: String { world?.entities["user"]?.location ?? "" }

    private func refresh() async {
        do {
            world = try await service.getWorld()
            loadError = false
        } catch { loadError = world == nil }
    }

    private func goTo(_ room: WorldRoom, entry: String = "", push: Bool = true) {
        Task {
            do {
                let mv = try await service.worldMove(to: room.id)
                if mv.ok {
                    // 进场的样子：进门后的第一条动作。没写星号就整句当动作包起来。
                    let t = entry.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !t.isEmpty {
                        try? await service.roomAct(room.id, text: t.contains("*") ? t : "*\(t)*")
                    }
                    await refresh()
                    if push { nav = RoomNav(roomID: room.id, title: room.name, peek: false) }
                } else {
                    lockedText = mv.text ?? "门锁着，没进去"
                }
            } catch { lockedText = "没走成：\(error.localizedDescription)" }
        }
    }

    /// 醒来撞上模型过载（服务端已隔 30s 重试过一次）→ 队列被按停，原因摆在这儿。
    /// 「开始」= 知道了：恢复队列，攒着的那一轮照常兑现（服务端顺手清掉这条）。
    private func queueErrorBanner(_ text: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill").font(.caption2)
            Text("\(text)——队列已暂停")
                .font(.caption2).multilineTextAlignment(.leading)
            Spacer(minLength: 0)
            Button("开始") {
                Task { try? await service.worldPause(false); await refresh() }
            }
            .font(.caption2.bold())
        }
        .foregroundStyle(.red)
        .padding(.horizontal, 16).padding(.vertical, 6)
        .frame(maxWidth: .infinity)
        .background(Color.red.opacity(0.12))
    }

    // 抱人邀约：人不一定停在房间页，房子视图也得看得见（回应窗口约 2 分钟）。
    @ViewBuilder
    private var carryOfferBanner: some View {
        if let off = world?.carry_offer {
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
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 14).fill(Color.house.surface))
            .overlay(RoundedRectangle(cornerRadius: 14)
                .stroke(Color.house.accent.opacity(0.5), lineWidth: 1))
        }
    }

    private func respondOffer(_ off: CarryOffer, accept: Bool) {
        Task {
            do { try await service.respondCarryOffer(id: off.id, accept: accept) }
            catch { lockedText = error.localizedDescription }   // 409 = 邀约已经不在了
            await refresh()
        }
    }

    private func toggleAway() {
        if userLocation == "away" {
            // 回家 = 默认落客厅，也给一次进场输入（可空）；回家不自动推房间页
            if let lr = world?.rooms.first(where: { $0.id == "living_room" }) {
                entryText = ""; entryPushes = false; entryTarget = lr
            }
        } else {
            Task {   // 出门：任意房间直接出，不用进场语
                _ = try? await service.worldMove(to: "away")
                await refresh()
            }
        }
    }

    // MARK: - 头部 / 楼层

    /// 悬浮手机钮（根模式）：聊天在这个世界里是「手机」，随时掏出来。
    private var phoneButton: some View {
        VStack {
            Spacer()
            HStack {
                Spacer()
                Button { onOpenPhone?() } label: {
                    ZStack(alignment: .topTrailing) {
                        Image(systemName: "message.fill")
                            .font(.system(size: 22))
                            .foregroundStyle(Color.house.onAccent)
                            .frame(width: 56, height: 56)
                            .background(Circle().fill(Color.house.accent))
                            .shadow(color: .black.opacity(0.18), radius: 8, y: 3)
                        if unreadCount > 0 {
                            Text("\(unreadCount)")
                                .font(.caption2.bold())
                                .foregroundStyle(.white)
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(Capsule().fill(.red))
                                .offset(x: 6, y: -4)
                        }
                    }
                }
                .padding(.trailing, 20).padding(.bottom, 24)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            if asRoot {
                Button { onOpenDrawer?() } label: {
                    Image(systemName: "pawprint")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(Color.house.accent)
                        .frame(width: 38, height: 38)
                        .background(Circle().fill(Color.house.surface))
                }
            } else {
                // 普通推入页（抽屉进来的）：系统导航栏被自定义顶栏顶掉了，返回键必须
                // 自己画——漏了它整页就没有出口（实锤：只能划掉 app 重开）。
                Button { dismiss() } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(Color.house.textPrimary)
                        .frame(width: 38, height: 38)
                        .background(Circle().fill(Color.house.surface))
                }
            }
            Image(systemName: "house.fill")
                .font(.system(size: 17))
                .foregroundStyle(Color.house.accent)
                .frame(width: 38, height: 38)
                .background(Circle().fill(Color.house.surface))
            VStack(alignment: .leading, spacing: 2) {
                Text("小屋").font(.title3.bold()).foregroundStyle(Color.house.textPrimary)
                Text(subtitle).font(.caption).foregroundStyle(Color.house.textSecondary)
            }
            Spacer()
            // 环境动静（导演口）：旁白一段声响/气味，点名让谁醒
            Button { nudgeOpen = true } label: {
                Image(systemName: "bell")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.house.accent)
                    .frame(width: 38, height: 38)
                    .background(Circle().fill(Color.house.surface))
            }
            Button(action: toggleAway) {
                Text(userLocation == "away" ? "回家" : "出门")
                    .font(.footnote.bold())
                    .foregroundStyle(Color.house.accent)
                    .padding(.horizontal, 14).padding(.vertical, 7)
                    .background(Capsule().fill(Color.house.surface))
            }
        }
        .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 10)
    }

    private var subtitle: String {
        guard let w = world else { return loadError ? "连不上小屋" : "加载中…" }
        let total = w.entities.count
        let home = w.entities.values.filter { $0.location != "away" }.count
        var s = "共 \(total) 位住户 · \(home) 人在家"
        let away = w.ids(at: "away").compactMap { w.entities[$0]?.name }
        if !away.isEmpty { s += " · \(away.joined(separator: "、"))出门了" }
        return s
    }

    private var floorTabs: some View {
        HStack(spacing: 6) {
            ForEach(floors, id: \.0) { f, name in
                let opened = openedFloors.contains(f)
                Button {
                    if opened { floor = f }
                } label: {
                    HStack(spacing: 4) {
                        if !opened { Image(systemName: "lock").font(.caption2) }
                        Text(name).font(.footnote.bold())
                    }
                    .foregroundStyle(floor == f ? Color.house.onAccent
                                     : opened ? Color.house.textPrimary : Color.house.textSecondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(Capsule().fill(floor == f ? Color.house.accent : Color.house.surfaceHi))
                }
            }
        }
        .padding(.horizontal, 16).padding(.bottom, 4)
    }

    private var openedFloors: Set<Int> { Set((world?.rooms ?? []).map(\.floor)) }

    // MARK: - 房间卡片

    @ViewBuilder
    private var floorContent: some View {
        let rooms = (world?.rooms ?? []).filter { $0.floor == floor }
        if rooms.isEmpty {
            VStack(spacing: 10) {
                Image(systemName: "lock.fill")
                    .font(.system(size: 30)).foregroundStyle(Color.house.textSecondary)
                Text("这层还没开放").font(.footnote).foregroundStyle(Color.house.textSecondary)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 60)
        } else {
            ForEach(rooms) { room in
                // 点卡片：已在房间 → 直接进（不弹进场弹窗）；不在 → 弹进场输入再走。
                // 偷看走卡片右上角的眼睛（在 roomCard 里，自己消费点击）。
                roomCard(room)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        if room.occupants.contains("user") {
                            nav = RoomNav(roomID: room.id, title: room.name, peek: false)
                        } else {
                            entryText = ""; entryPushes = true; entryTarget = room
                        }
                    }
            }
        }
    }

    private func roomCard(_ room: WorldRoom) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(room.name).font(.headline).foregroundStyle(Color.house.textPrimary)
                if room.lock == 1 {
                    Image(systemName: "lock.fill")
                        .font(.caption).foregroundStyle(Color.house.textSecondary)
                }
                Spacer()
                if room.occupants.contains("user") {
                    Text("\(HouseUserName.value)在这里").font(.caption2.bold())
                        .foregroundStyle(Color.house.onAccent)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill(Color.house.accent))
                }
                // 右上角眼睛 = 偷看一眼（上帝视角，不产生事件）
                Button {
                    nav = RoomNav(roomID: room.id, title: room.name, peek: true)
                } label: {
                    Image(systemName: "eye")
                        .font(.system(size: 13))
                        .foregroundStyle(Color.house.textSecondary)
                        .frame(width: 28, height: 28)
                        .background(Circle().fill(Color.house.surfaceHi))
                }
                .buttonStyle(.plain)
            }
            Spacer(minLength: 34)
            if room.occupants.isEmpty {
                Text("空房").font(.caption).foregroundStyle(Color.house.textSecondary)
            } else {
                HStack(spacing: -8) {
                    ForEach(room.occupants, id: \.self) { eid in
                        HouseAvatarChip(entityID: eid, entity: world?.entities[eid], size: 40)
                    }
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, minHeight: 128, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 20).fill(Color.house.surface))
        .overlay(RoundedRectangle(cornerRadius: 20).stroke(Color.house.line, lineWidth: 1))
    }

}

/// 环境动静（导演口）：机主旁白一段声响/气味/光线，点名让谁被它「吵醒」。
/// 只入队醒因、不落房间事件——没被点名的在场者不会看见这段旁白。
private struct NudgeSheet: View {
    let world: WorldSnapshot?
    let service: ChatService
    @Environment(\.dismiss) private var dismiss

    @State private var text = ""
    @State private var picked: Set<String> = []
    @State private var sending = false
    @State private var errorText: String? = nil

    /// 可点名的角色（user 之外的全部实体），按 id 稳定排序。
    private var chars: [(id: String, name: String)] {
        (world?.entities ?? [:])
            .filter { $0.key != "user" }
            .map { (id: $0.key, name: $0.value.name) }
            .sorted { $0.id < $1.id }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("比如：浴室传来水声", text: $text, axis: .vertical)
                        .lineLimit(2...4)
                } header: {
                    Text("发生了什么")
                } footer: {
                    Text("这段话会原样成为 TA 的醒来原因。没被点名的人不会察觉。")
                }
                Section("谁被惊动") {
                    ForEach(chars, id: \.id) { c in
                        Button {
                            if picked.contains(c.id) { picked.remove(c.id) }
                            else { picked.insert(c.id) }
                        } label: {
                            HStack {
                                Text(c.name).foregroundStyle(.primary)
                                Spacer()
                                if picked.contains(c.id) {
                                    Image(systemName: "checkmark")
                                        .font(.footnote.weight(.semibold))
                                        .foregroundStyle(Color.house.accent)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                    }
                }
                if let errorText {
                    Text(errorText).font(.footnote).foregroundStyle(.red)
                }
            }
            .navigationTitle("环境动静")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if sending {
                        ProgressView()
                    } else {
                        Button("触发") { send() }
                            .disabled(picked.isEmpty
                                      || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
            }
        }
    }

    private func send() {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !picked.isEmpty else { return }
        sending = true
        Task {
            defer { sending = false }
            do {
                try await service.worldNudge(text: t, targets: Array(picked))
                dismiss()
            } catch {
                errorText = (error as? ChatServiceError)?.errorDescription
                    ?? error.localizedDescription
            }
        }
    }
}

/// 房间视图的跳转参数。
struct RoomNav: Identifiable, Hashable {
    let roomID: String
    let title: String
    let peek: Bool
    var id: String { "\(roomID)-\(peek)" }
}

/// 实体头像小圆片：有头像图用图（user=me.png，角色=char_<id>.png），没有就名字首字。
/// 正在电脑前的角色右下角带个小键盘/手柄角标。
struct HouseAvatarChip: View {
    @EnvironmentObject private var profileStore: ProfileStore
    let entityID: String
    let entity: WorldEntity?
    var size: CGFloat = 40

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            avatarCircle
            if let s = entity?.status {
                Image(systemName: s == "game" ? "gamecontroller.fill" : "keyboard.fill")
                    .font(.system(size: size * 0.28))
                    .foregroundStyle(Color.house.onAccent)
                    .padding(3)
                    .background(Circle().fill(Color.house.accent))
                    .offset(x: 3, y: 3)
            }
        }
    }

    @ViewBuilder
    private var avatarCircle: some View {
        let image = entityID == "user" ? profileStore.meAvatar
                                       : profileStore.avatarImage(forCharacter: entityID)
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Text(String((entity?.name ?? entityID).prefix(1)))
                    .font(.system(size: size * 0.42, weight: .bold))
                    .foregroundStyle(Color.house.textPrimary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color.house.surfaceHi)
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .overlay(Circle().stroke(IdentityColor.color(for: entityID), lineWidth: 2))
    }
}
