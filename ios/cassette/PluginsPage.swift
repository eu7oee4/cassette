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
    var id: String { name }
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
        .alert("操作失败", isPresented: Binding(
            get: { errorText != nil }, set: { if !$0 { errorText = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(errorText ?? "") }
        .confirmationDialog("卸载「\(uninstallTarget?.display_name ?? "")」？插件目录会被删除。",
                            isPresented: Binding(get: { uninstallTarget != nil },
                                                 set: { if !$0 { uninstallTarget = nil } }),
                            titleVisibility: .visible) {
            Button("卸载", role: .destructive) {
                if let p = uninstallTarget { Task { await run(p.name) { try await service.uninstallPlugin(name: p.name) } } }
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
                    Task { await run(p.name) { try await service.installPlugin(name: p.name) } }
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
                        Task { await run(p.name) { try await service.updatePlugin(name: p.name) } }
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

    /// 跑一个插件操作：行内转圈 → 完了重拉列表对齐状态。
    private func run(_ name: String, _ op: @escaping () async throws -> Void) async {
        busyName = name
        do { try await op() }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "操作失败" }
        busyName = nil
        await load()
    }
}
