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
    var id: String { name }

    /// version 是作者手写的、靠自觉（两个不同 commit 可以都自称 0.1.0），sha 才是真身份。
    /// 装着的那个跟 registry 钉的对不上 = 这份没跟上，左滑「更新」能对齐。
    var isStale: Bool {
        guard let a = installed_commit, !a.isEmpty, let b = commit, !b.isEmpty else { return false }
        return a != b
    }
}

extension ChatService {
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

    @ViewBuilder
    private func row(_ p: PluginItem) -> some View {
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
                }
                Text(p.description).font(.footnote).foregroundStyle(.secondary)
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
                .disabled(p.valid == false)
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
