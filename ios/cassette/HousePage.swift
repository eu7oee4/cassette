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
    @State private var pendingRoom: WorldRoom?    // 点了哪个房间（弹三选一）
    @State private var nav: RoomNav?
    @State private var lockedText: String?        // 门锁着的提示
    @State private var loadError = false

    private let service = ChatService()
    private let floors: [(Int, String)] = [(2, "2楼"), (1, "1楼"), (0, "地下室")]

    var body: some View {
        ZStack {
            Color.house.bg.ignoresSafeArea()
            VStack(spacing: 0) {
                header
                floorTabs
                ScrollView {
                    VStack(spacing: 14) {
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
        .confirmationDialog(pendingRoom?.name ?? "", isPresented: Binding(
            get: { pendingRoom != nil }, set: { if !$0 { pendingRoom = nil } }),
            titleVisibility: .visible) {
            if let room = pendingRoom {
                Button(userLocation == room.id ? "进去（你在这里）" : "去这里") { goTo(room) }
                Button("偷看一眼") { nav = RoomNav(roomID: room.id, title: room.name, peek: true) }
                Button("取消", role: .cancel) {}
            }
        }
        .alert("门锁着", isPresented: Binding(
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

    private func goTo(_ room: WorldRoom) {
        Task {
            do {
                let mv = try await service.worldMove(to: room.id)
                if mv.ok {
                    await refresh()
                    nav = RoomNav(roomID: room.id, title: room.name, peek: false)
                } else {
                    lockedText = mv.text ?? "门锁着，没进去"
                }
            } catch { lockedText = "没走成：\(error.localizedDescription)" }
        }
    }

    private func toggleAway() {
        Task {
            // 出门 = away（任意房间可直接出）；回家 = 默认落客厅。
            _ = try? await service.worldMove(to: userLocation == "away" ? "living_room" : "away")
            await refresh()
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
                Button { pendingRoom = room } label: { roomCard(room) }
                    .buttonStyle(.plain)
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
                    Text("你在这里").font(.caption2.bold())
                        .foregroundStyle(Color.house.onAccent)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill(Color.house.accent))
                }
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
        .overlay(Circle().stroke(Color.house.accentLight, lineWidth: 2))
    }
}
