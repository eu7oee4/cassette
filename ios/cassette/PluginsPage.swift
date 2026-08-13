import SwiftUI

// MARK: - 数据模型（对齐后端 /plugins）

struct PluginItem: Decodable, Identifiable {
    let name: String
    let display_name: String
    let description: String
    let version: String?
    let in_registry: Bool?
    let state: String        // not_installed / disabled / enabled
    let valid: Bool?
    let commit: String?              // registry 钉的短 sha（该装哪个）
    let installed_commit: String?    // 实际装着的短 sha；空 = 手放的开发副本，来源不可考
    let wake_toggleable: Bool?       // 有「醒来能用」开关（宿主 WAKE_TOGGLEABLE 里的才有）
    let wake_enabled: Bool?          // 开关当前状态（默认关）
    // 开关文案由后端下发：开关管的是整插件还是只某几个工具（如 mail 只管发信），
    // 语义在宿主那边定，app 不猜。旧后端没这俩字段 → 回退通用文案。
    let wake_toggle_title: String?
    let wake_toggle_desc: String?
    // 独占资源归属（后端 plugins.EXCLUSIVE）：这插件背后是一份只有一个的东西时，
    // 只有归属角色能真正挂上它——**别的角色即使把开关拨开了也不挂**。
    // owner 空串 = 它吃的几样资源分属不同人，那就谁都用不了（后端如实回空，别猜一个名字）。
    let exclusive: Bool?
    let owned: Bool?
    let owner: String?
    let owner_name: String?
    let resources: [String]?
    var id: String { name }

    /// 这行要不要显示"不归你"的说明。旧后端没这几个字段 → nil，一律当归你（老行为）。
    var blockedByOwner: Bool { exclusive == true && owned == false }

    /// version 是作者手写的、靠自觉（两个不同 commit 可以都自称 0.1.0），sha 才是真身份。
    /// 装着的那个跟 registry 钉的对不上 = 这份没跟上，左滑「更新」能对齐。
    var isStale: Bool {
        guard let a = installed_commit, !a.isEmpty, let b = commit, !b.isEmpty else { return false }
        return a != b
    }
}

/// 一样独占资源 + 现在归谁 + 谁在吃它（后端 /plugins/resources）。
struct ResourceItem: Decodable, Identifiable {
    let resource: String
    let label: String
    let owner: String
    let owner_name: String
    let plugins: [String]
    var id: String { resource }
}

struct ResourcesResponse: Decodable {
    let items: [ResourceItem]
    let characters: [CharacterInfo]
}

extension ChatService {
    func getResources() async throws -> ResourcesResponse {
        let data = try await perform(authedRequest("GET", "/plugins/resources"))
        guard let r = try? JSONDecoder().decode(ResourcesResponse.self, from: data)
        else { throw ChatServiceError.badResponse }
        return r
    }

    func setResourceOwner(resource: String, charID: String) async throws {
        struct Body: Encodable { let resource: String; let char_id: String }
        let body = try JSONEncoder().encode(Body(resource: resource, char_id: charID))
        _ = try await perform(authedRequest("POST", "/plugins/owner", jsonBody: body))
    }

    func getPlugins() async throws -> [PluginItem] {
        let data = try await perform(authedRequest("GET", "/plugins"))
        struct Wrap: Decodable { let items: [PluginItem] }
        do { return try JSONDecoder().decode(Wrap.self, from: data).items }
        catch { throw ChatServiceError.badResponse }
    }

    private struct PluginBody: Encodable {
        let name: String
        var enabled: Bool? = nil
    }

    func installPlugin(name: String) async throws {
        let body = try JSONEncoder().encode(PluginBody(name: name))
        // clone 要下载整个仓，超时放宽
        _ = try await perform(authedRequest("POST", "/plugins/install", jsonBody: body, timeout: 200))
    }

    func togglePlugin(name: String, enabled: Bool) async throws {
        let body = try JSONEncoder().encode(PluginBody(name: name, enabled: enabled))
        _ = try await perform(authedRequest("POST", "/plugins/toggle", jsonBody: body))
    }

    func wakeTogglePlugin(name: String, enabled: Bool) async throws {
        let body = try JSONEncoder().encode(PluginBody(name: name, enabled: enabled))
        _ = try await perform(authedRequest("POST", "/plugins/wake_toggle", jsonBody: body))
    }

    func updatePlugin(name: String) async throws {
        let body = try JSONEncoder().encode(PluginBody(name: name))
        _ = try await perform(authedRequest("POST", "/plugins/update", jsonBody: body, timeout: 200))
    }

    func uninstallPlugin(name: String) async throws {
        let body = try JSONEncoder().encode(PluginBody(name: name))
        _ = try await perform(authedRequest("POST", "/plugins/uninstall", jsonBody: body))
    }
}

// MARK: - 插件商店页

/// 三态：未安装(下载) / 已装未启用 / 启用中。工具代码全在 Mac 上，这页是遥控器；
/// 下载只认后端写死的白名单仓（安全设计），这里没有任何"输入 URL 安装"的口。
struct PluginsPage: View {
    private let service = ChatService()

    @State private var items: [PluginItem] = []
    @State private var loading = true
    @State private var busyName: String? = nil       // 正在装/切/卸的那个（行内转圈）
    @State private var errorText: String? = nil
    @State private var uninstallTarget: PluginItem? = nil
    @State private var noteText: String? = nil       // 操作成功的短提示（2.5s 自动消失）
    @State private var noteSeq = 0                   // 提示计时的世代号，防前一条掐掉后一条
    @State private var showOwnership = false         // 归属页（右上角）

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if items.isEmpty {
                ContentUnavailableView("货架还空着", systemImage: "puzzlepiece.extension",
                                       description: Text("插件会随版本逐个上架。"))
            } else {
                List {
                    Section {
                        ForEach(items) { p in row(p) }
                    } footer: {
                        Text("插件装在后端（你的 Mac）上，这里是遥控器。只能安装自带白名单里的插件，开/关即时生效。")
                    }
                }
            }
        }
        .navigationTitle("插件商店")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("归属") { showOwnership = true }
            }
        }
        .sheet(isPresented: $showOwnership) {
            // 关掉归属页要重拉插件列表：归属一变，哪几行变成"不归你"就跟着变。
            OwnershipSheet(onClose: { showOwnership = false; Task { await load() } })
        }
        .refreshable { await load() }
        .task { await load() }
        .overlay(alignment: .bottom) {
            if let noteText {
                Text(noteText)
                    .font(.footnote)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(.ultraThinMaterial, in: Capsule())
                    .overlay(Capsule().stroke(.separator))
                    .padding(.bottom, 24)
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: noteText)
        .alert("操作失败", isPresented: Binding(
            get: { errorText != nil }, set: { if !$0 { errorText = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(errorText ?? "") }
        .confirmationDialog("卸载「\(uninstallTarget?.display_name ?? "")」？插件目录会被删除。",
                            isPresented: Binding(get: { uninstallTarget != nil },
                                                 set: { if !$0 { uninstallTarget = nil } }),
                            titleVisibility: .visible) {
            Button("卸载", role: .destructive) {
                if let p = uninstallTarget {
                    Task { await run(p.name, note: "已卸载") { try await service.uninstallPlugin(name: p.name) } }
                }
            }
            Button("取消", role: .cancel) { }
        }
    }

    // 归属文案拆成纯函数：字符串三元 + 插值直接写进 ViewBuilder 里，Swift 的类型检查器
    // 会在这个本来就很长的表达式上卡死（实测报 "unable to type-check in reasonable time"）。
    private static func ownerBadge(_ p: PluginItem) -> String {
        let n = p.owner_name ?? ""
        return n.isEmpty ? "归属分散" : "归「\(n)」"
    }

    private static func ownerHint(_ p: PluginItem) -> String {
        let n = p.owner_name ?? ""
        if n.isEmpty {
            return "它要的几样东西现在分属不同角色，谁都用不了——去右上角「归属」把它们归到同一个人名下。"
        }
        return "这东西只有一份，现在归「\(n)」。在这边开了也不会挂上——要用就去右上角「归属」转过来。"
    }

    @ViewBuilder
    private func row(_ p: PluginItem) -> some View {
        VStack(spacing: 10) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(p.display_name).font(.body.weight(.medium))
                        if let v = p.version, !v.isEmpty {
                            Text("v\(v)").font(.caption2).foregroundStyle(.tertiary)
                        }
                        // 装着的那个 commit：这才是"我装的到底是哪一份"的确定答案
                        if let c = p.installed_commit, !c.isEmpty {
                            Text(c).font(.caption2.monospaced()).foregroundStyle(.tertiary)
                        }
                        if p.isStale {
                            Text("可更新").font(.caption2).foregroundStyle(Color.theme)
                        }
                        if p.valid == false {
                            Text("清单损坏").font(.caption2).foregroundStyle(.red)
                        }
                        // 不归你：必须画出来。没有这条的话，开关拨得开、显示"已启用"，
                        // 工具却根本不挂——一个从界面上完全查不出原因的谜。
                        if p.blockedByOwner {
                            Text(Self.ownerBadge(p))
                                .font(.caption2)
                                .padding(.horizontal, 5).padding(.vertical, 1)
                                .background(Color.secondary.opacity(0.15), in: Capsule())
                                .foregroundStyle(.secondary)
                        }
                    }
                    Text(p.description).font(.footnote).foregroundStyle(.secondary)
                    if p.blockedByOwner {
                        Text(Self.ownerHint(p)).font(.caption2).foregroundStyle(.orange)
                    }
                }
                Spacer()
                if busyName == p.name {
                    ProgressView()
                } else if p.state == "not_installed" {
                    Button("下载") {
                        Task { await run(p.name, note: "已安装") { try await service.installPlugin(name: p.name) } }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.theme)
                    .font(.footnote.weight(.medium))
                } else {
                    Toggle("", isOn: Binding(
                        get: { p.state == "enabled" },
                        set: { on in Task { await run(p.name) { try await service.togglePlugin(name: p.name, enabled: on) } } }
                    ))
                    .labelsHidden()
                    .tint(Color.theme)
                    // 不归你就别让拨——拨得动却不生效，比拨不动更难懂。
                    .disabled(p.valid == false || p.blockedByOwner)
                }
            }
            // 「醒来能用」：只有宿主标了 wake_toggleable 的插件（browser）才有，且要装了并
            // 开着才画——关掉插件时开关跟着藏（醒来那条路本来就要求 enabled，藏了不骗人）。
            // 默认关：要不要让一次没人看着的自发醒来摸到这个插件，是机主自己的决定。
            if p.state == "enabled", p.wake_toggleable == true, !p.blockedByOwner {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.wake_toggle_title ?? "醒来能用").font(.footnote)
                        Text(p.wake_toggle_desc ?? "打开后 TA 自发醒来时也能用它（默认关）")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(
                        get: { p.wake_enabled == true },
                        set: { on in Task { await run(p.name) { try await service.wakeTogglePlugin(name: p.name, enabled: on) } } }
                    ))
                    .labelsHidden()
                    .tint(Color.theme)
                }
                .padding(.leading, 12)
            }
        }
        .padding(.vertical, 4)
        .swipeActions(edge: .trailing) {
            if p.state != "not_installed" {
                Button(role: .destructive) { uninstallTarget = p } label: {
                    Label("卸载", systemImage: "trash")
                }
                if p.in_registry == true {
                    Button {
                        Task { await update(p) }
                    } label: { Label("更新", systemImage: "arrow.triangle.2.circlepath") }
                    .tint(Color.theme)
                }
            }
        }
    }

    private func load() async {
        errorText = nil
        do { items = try await service.getPlugins() }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端" }
        loading = false
    }

    /// 跑一个插件操作：行内转圈 → 完了重拉列表对齐状态。返回值＝成功没成功。
    /// note＝成功后的短提示。以前成功是**完全静默**的：更新一个已经对齐的插件时，转圈之后
    /// 状态/版本号一模一样，界面上和"什么都没发生"分辨不出来（实锤困惑过）。
    /// 开关不用 note——拨到哪边本身就是反馈。
    @discardableResult
    private func run(_ name: String, note: String? = nil,
                     _ op: @escaping () async throws -> Void) async -> Bool {
        busyName = name
        var ok = true
        do { try await op() }
        catch {
            ok = false
            errorText = (error as? ChatServiceError)?.errorDescription ?? "操作失败"
        }
        busyName = nil
        await load()
        if ok, let note { showNote(note) }
        return ok
    }

    /// 更新单独走一层：提示里带上 sha 的前后对比。
    /// 「更新」按钮是常显的（条件只有 in_registry，没有版本比较），所以按下去有两种正常结果——
    /// 真的换了版本，和本来就已经是钉住的那版。这两种得让人分得出来。
    private func update(_ p: PluginItem) async {
        let before = p.installed_commit ?? ""
        guard await run(p.name, { try await service.updatePlugin(name: p.name) }) else { return }
        let after = items.first(where: { $0.name == p.name })?.installed_commit ?? ""
        if after.isEmpty {
            showNote("已重装")
        } else if after == before {
            showNote("已经是钉住的那版（\(after)）")
        } else {
            showNote("已更新到 \(after)")
        }
    }

    /// 短提示：2.5 秒后自己消失。新提示来了就把上一条的计时作废（不然前一条会把后一条掐掉）。
    private func showNote(_ text: String) {
        noteSeq += 1
        let mine = noteSeq
        noteText = text
        Task {
            try? await Task.sleep(for: .seconds(2.5))
            if noteSeq == mine { noteText = nil }
        }
    }
}

// MARK: - 归属页（插件商店右上角）

/// 独占资源的归属：**这东西只有一份，所以同一时刻只能给一个人用。**
///
/// 管的是资源不是插件——一个插件可能吃好几样（游戏剧情既要游戏账号又要会话），
/// 几个插件可能吃同一样（两个游戏插件共用一个账号）。所以这页按资源列，转一次
/// 账号归属，吃它的插件一起跟着走，不会出现两个角色同时上手同一个号。
struct OwnershipSheet: View {
    let onClose: () -> Void
    private let service = ChatService()

    @State private var items: [ResourceItem] = []
    @State private var chars: [CharacterInfo] = []
    @State private var loading = true
    @State private var busy: String? = nil
    @State private var errorText: String? = nil

    var body: some View {
        NavigationStack {
            Group {
                if loading {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if items.isEmpty {
                    ContentUnavailableView("没有需要分归属的东西", systemImage: "key")
                } else {
                    List {
                        Section {
                            ForEach(items) { r in row(r) }
                        } footer: {
                            Text("这些东西每样只有一份，同一时刻只能给一个角色用。转走之后，"
                                 + "原来那个角色即使把插件开关拨开也不会挂上。改完下一轮聊天／醒来生效。")
                        }
                    }
                }
            }
            .navigationTitle("归属")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { Button("完成") { onClose() } }
            }
            .task { await load() }
            .alert("转不了", isPresented: Binding(
                get: { errorText != nil }, set: { if !$0 { errorText = nil } }
            )) { Button("好", role: .cancel) { } } message: { Text(errorText ?? "") }
        }
    }

    @ViewBuilder
    private func row(_ r: ResourceItem) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(r.label).font(.body.weight(.medium))
                if !r.plugins.isEmpty {
                    Text("用到它的：" + r.plugins.joined(separator: "、"))
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if busy == r.resource {
                ProgressView()
            } else {
                // Menu 而不是 Picker：角色可能只有一个（那就只有一项，点开一看就明白
                // 现在没得选），也可能以后有好几个，同一套 UI 都撑得住。
                Menu {
                    ForEach(chars) { c in
                        Button {
                            Task { await transfer(r, to: c.id) }
                        } label: {
                            if c.id == r.owner {
                                Label(name(c), systemImage: "checkmark")
                            } else {
                                Text(name(c))
                            }
                        }
                    }
                } label: {
                    HStack(spacing: 3) {
                        Text(r.owner_name.isEmpty ? r.owner : r.owner_name)
                        Image(systemName: "chevron.up.chevron.down").font(.caption2)
                    }
                    .font(.footnote)
                    .foregroundStyle(Color.theme)
                }
            }
        }
        .padding(.vertical, 2)
    }

    private func name(_ c: CharacterInfo) -> String {
        c.display_name.isEmpty ? c.id : c.display_name
    }

    private func load() async {
        do {
            let r = try await service.getResources()
            items = r.items
            chars = r.characters
        } catch {
            errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端"
        }
        loading = false
    }

    /// 转让：同一个人就别白跑一趟后端。失败把后端那句话原样弹出来——
    /// 「会话正开着」这类拒绝是有具体去处的，含糊成"操作失败"就没人知道该干嘛。
    private func transfer(_ r: ResourceItem, to charID: String) async {
        guard charID != r.owner else { return }
        busy = r.resource
        do { try await service.setResourceOwner(resource: r.resource, charID: charID) }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "转不了" }
        busy = nil
        await load()
    }
}
